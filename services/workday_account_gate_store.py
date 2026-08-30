"""Private, transaction-safe Workday account-gate persistence operations."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from inspect import Parameter, signature
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from models.database import (
    JobApplication,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
    WorkdayCooldownNotice,
)
from services.portal_credentials import PortalCredentialError, normalize_portal_scope
from services.workday_transition_contracts import (
    AUTH_SUBMIT_LIMIT_PER_ATTEMPT,
    WorkdayGateState,
)


class WorkdayGateStoreError(RuntimeError):
    """Base error for invalid private gate operations."""


class WorkdayGateOwnershipError(WorkdayGateStoreError):
    """The application does not belong to the trusted server-side user."""


class WorkdayGateBindingError(WorkdayGateStoreError):
    """The application is already bound to a different private gate."""


class WorkdayGateDecision(str, Enum):
    """Worker eligibility returned by one atomic gate acquisition."""

    ALLOW = "allow"
    ONE_PROBE = "one_probe"
    OBSERVE_ONLY = "observe_only"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class WorkdayGateAcquireRequest:
    """Trusted server-side identity used to bind and acquire one gate."""

    user_id: UUID
    application_id: UUID
    account_ref: UUID
    portal_scope: str


@dataclass(frozen=True, slots=True)
class WorkdayGateLease:
    """Opaque mutation authority for one gate generation."""

    gate_id: UUID
    application_id: UUID
    generation: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class WorkdayAuthGateBinding:
    """Trusted owner/account/tenant metadata verified before vault access."""

    user_id: UUID
    account_ref: UUID
    portal_scope: str


@dataclass(frozen=True, slots=True)
class WorkdayGateAcquisition:
    """Atomic gate decision and optional lease authority."""

    decision: WorkdayGateDecision
    gate_id: UUID
    generation: int
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    next_eligible_at: datetime | None = None

    def lease_for(self, application_id: UUID) -> WorkdayGateLease | None:
        if self.lease_token is None:
            return None
        return WorkdayGateLease(
            gate_id=self.gate_id,
            application_id=application_id,
            generation=self.generation,
            lease_token=self.lease_token,
        )


@dataclass(frozen=True, slots=True)
class WorkdayAccountLockEvent:
    """Secret-free event data consumed by the later notification task."""

    gate_id: UUID
    application_id: UUID
    generation: int
    cooldown_until: datetime


@dataclass(frozen=True, slots=True)
class WorkdayGateMutation:
    """Result of a generation/token-guarded mutation."""

    applied: bool
    stale: bool = False
    lock_event: WorkdayAccountLockEvent | None = None


class SQLAlchemyWorkdayAccountGateStore:
    """Serialize private Workday authentication through locked gate rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        lease_ttl: timedelta = timedelta(minutes=10),
        default_lock_cooldown: timedelta = timedelta(hours=6),
        max_backoff: timedelta = timedelta(minutes=30),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_ttl <= timedelta(0):
            raise ValueError("Gate lease TTL must be positive.")
        if default_lock_cooldown <= timedelta(0):
            raise ValueError("Default account-lock cooldown must be positive.")
        if max_backoff <= timedelta(0):
            raise ValueError("Maximum gate backoff must be positive.")
        self._session = session
        self._lease_ttl = lease_ttl
        self._default_lock_cooldown = default_lock_cooldown
        self._max_backoff = max_backoff
        self._clock = clock or (lambda: datetime.now(UTC))

    async def acquire(
        self, request: WorkdayGateAcquireRequest
    ) -> WorkdayGateAcquisition:
        """Bind the owned application and atomically decide worker eligibility."""
        async with self._session.begin():
            return await self.acquire_in_transaction(request)

    async def acquire_in_transaction(
        self, request: WorkdayGateAcquireRequest
    ) -> WorkdayGateAcquisition:
        """Acquire using the caller's transaction for queue-lease atomicity."""
        portal_scope = _canonical_workday_scope(request.portal_scope)
        account_ref = str(request.account_ref)
        now = self._now()

        application = await self._owned_application_for_update(
            application_id=request.application_id,
            user_id=request.user_id,
        )
        await self._ensure_gate(
            user_id=request.user_id,
            account_ref=account_ref,
            portal_scope=portal_scope,
            now=now,
        )
        gate = await self._gate_by_identity_for_update(
            user_id=request.user_id,
            account_ref=account_ref,
            portal_scope=portal_scope,
        )
        if gate is None:
            raise WorkdayGateStoreError("Private Workday gate could not be created.")
        if application.workday_account_gate_id not in (None, gate.id):
            raise WorkdayGateBindingError(
                "Application is already bound to a different Workday account gate."
            )
        application.workday_account_gate_id = gate.id

        active = await self._active_attempt_for_update(gate.id)
        if active is not None and active.lease_expires_at <= now:
            if active.auth_submit_count == 0:
                active.status = "abandoned"
                active = None
            else:
                gate.state = WorkdayGateState.AUTH_OUTCOME_PENDING.value

        state = WorkdayGateState(gate.state)
        if state is WorkdayGateState.AUTH_OUTCOME_PENDING:
            if active is not None and active.application_id == application.id:
                return self._acquisition(
                    gate,
                    WorkdayGateDecision.OBSERVE_ONLY,
                    attempt=active,
                )
            return self._acquisition(gate, WorkdayGateDecision.DEFER)
        if state is WorkdayGateState.REVIEW_REQUIRED:
            return self._acquisition(gate, WorkdayGateDecision.DEFER)
        if active is not None:
            return self._acquisition(gate, WorkdayGateDecision.DEFER)
        if state is WorkdayGateState.PROBE_IN_PROGRESS:
            # A missing/expired unsubmitted probe is safe to reclaim.
            gate.state = WorkdayGateState.COOLING_DOWN.value
            state = WorkdayGateState.COOLING_DOWN
        if state is WorkdayGateState.COOLING_DOWN:
            effective_until = _maximum_time(
                gate.cooldown_until,
                gate.user_min_until,
                gate.next_eligible_at,
            )
            if effective_until is None or effective_until > now:
                return self._acquisition(
                    gate,
                    WorkdayGateDecision.DEFER,
                    next_eligible_at=effective_until,
                )
            return await self._grant(
                gate=gate,
                application_id=application.id,
                decision=WorkdayGateDecision.ONE_PROBE,
                now=now,
            )
        if state is not WorkdayGateState.OPEN:
            return self._acquisition(gate, WorkdayGateDecision.DEFER)
        return await self._grant(
            gate=gate,
            application_id=application.id,
            decision=WorkdayGateDecision.ALLOW,
            now=now,
        )

    async def mark_secret_accessed(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        """Atomically persist secret-access authority before vault access."""
        async with self._session.begin():
            gate, attempt = await self._current_attempt_for_update(lease)
            if (
                gate is None
                or attempt is None
                or attempt.lease_expires_at <= self._now()
                or attempt.auth_submit_count != 0
            ):
                return WorkdayGateMutation(applied=False, stale=True)
            if attempt.secret_accessed:
                return WorkdayGateMutation(applied=False, stale=True)
            attempt.secret_accessed = True
            attempt.heartbeat_at = self._now()
            return WorkdayGateMutation(applied=True)

    async def verify_auth_lease(
        self, lease: WorkdayGateLease, binding: WorkdayAuthGateBinding
    ) -> bool:
        """Verify exact owner/account/tenant and current lease before vault access."""
        portal_scope = _canonical_workday_scope(binding.portal_scope)
        async with self._session.begin():
            gate, attempt = await self._current_attempt_for_update(lease)
            if (
                gate is None
                or attempt is None
                or attempt.lease_expires_at <= self._now()
                or attempt.secret_accessed
                or attempt.auth_submit_count != 0
            ):
                return False
            if (
                gate.user_id != binding.user_id
                or gate.account_ref != str(binding.account_ref)
                or gate.portal_scope != portal_scope
            ):
                return False
            application = await self._owned_application_for_update(
                application_id=lease.application_id,
                user_id=binding.user_id,
            )
            return application.workday_account_gate_id == gate.id

    async def claim_auth_submit(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        """Claim the fixed ``0 -> 1`` authentication submission exactly once."""
        async with self._session.begin():
            gate, attempt = await self._current_attempt_for_update(lease)
            if (
                gate is None
                or attempt is None
                or attempt.lease_expires_at <= self._now()
                or attempt.auth_submit_count != 0
            ):
                return WorkdayGateMutation(applied=False, stale=True)
            attempt.auth_submit_count = AUTH_SUBMIT_LIMIT_PER_ATTEMPT
            attempt.heartbeat_at = self._now()
            gate.state = WorkdayGateState.AUTH_OUTCOME_PENDING.value
            return WorkdayGateMutation(applied=True)

    async def claim_llm_repair(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        """Claim the one pre-auth structural repair permitted per attempt."""
        async with self._session.begin():
            _gate, attempt = await self._current_attempt_for_update(lease)
            if (
                attempt is None
                or attempt.lease_expires_at <= self._now()
                or attempt.secret_accessed
                or attempt.auth_submit_count != 0
                or attempt.llm_repair_count != 0
            ):
                return WorkdayGateMutation(applied=False, stale=True)
            attempt.llm_repair_count = 1
            attempt.heartbeat_at = self._now()
            return WorkdayGateMutation(applied=True)

    async def complete_success(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        """Complete a submitted authentication attempt."""
        async with self._session.begin():
            return await self.complete_success_in_transaction(
                lease, authentication_submitted=True
            )

    async def complete_success_in_transaction(
        self,
        lease: WorkdayGateLease,
        *,
        authentication_submitted: bool,
    ) -> WorkdayGateMutation:
        """Complete one guarded attempt inside its caller's transaction."""
        now = self._now()
        gate, attempt = await self._current_attempt_for_update(lease)
        expected_submit_count = 1 if authentication_submitted else 0
        if (
            gate is None
            or attempt is None
            or attempt.lease_expires_at <= now
            or attempt.auth_submit_count != expected_submit_count
        ):
            return WorkdayGateMutation(applied=False, stale=True)
        attempt.status = "completed"
        gate.generation += 1
        if gate.user_min_until is not None and gate.user_min_until > now:
            gate.state = WorkdayGateState.COOLING_DOWN.value
            gate.cooldown_until = _maximum_time(
                gate.cooldown_until, gate.user_min_until
            )
            gate.next_eligible_at = gate.cooldown_until
        else:
            gate.state = WorkdayGateState.OPEN.value
            gate.cooldown_until = None
            gate.next_eligible_at = None
        return WorkdayGateMutation(applied=True)

    async def start_bounded_backoff(
        self, lease: WorkdayGateLease, *, backoff: timedelta
    ) -> WorkdayGateMutation:
        """Close a pre-submit attempt until one bounded server-time deadline."""
        if backoff <= timedelta(0) or backoff > self._max_backoff:
            raise ValueError(
                "Backoff must be positive and within the configured bound."
            )
        now = self._now()
        async with self._session.begin():
            gate, attempt = await self._current_attempt_for_update(lease)
            if (
                gate is None
                or attempt is None
                or attempt.lease_expires_at <= now
                or attempt.auth_submit_count != 0
            ):
                return WorkdayGateMutation(applied=False, stale=True)
            attempt.status = "abandoned"
            gate.generation += 1
            gate.state = WorkdayGateState.COOLING_DOWN.value
            until = now + backoff
            gate.next_eligible_at = _maximum_time(
                gate.next_eligible_at, gate.user_min_until, until
            )
            return WorkdayGateMutation(applied=True)

    async def mark_review_required(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        """Preserve an unresolved attempt and require explicit resolution."""
        async with self._session.begin():
            return await self.mark_review_required_in_transaction(lease)

    async def mark_review_required_in_transaction(
        self,
        lease: WorkdayGateLease,
        *,
        authentication_submitted: bool | None = None,
    ) -> WorkdayGateMutation:
        """Require review while the caller owns the same gate transaction."""
        gate, attempt = await self._current_attempt_for_update(lease)
        if gate is None or attempt is None:
            return WorkdayGateMutation(applied=False, stale=True)
        if authentication_submitted is not None and attempt.auth_submit_count != int(
            authentication_submitted
        ):
            return WorkdayGateMutation(applied=False, stale=True)
        attempt.status = "review_required"
        gate.generation += 1
        gate.state = WorkdayGateState.REVIEW_REQUIRED.value
        return WorkdayGateMutation(applied=True)

    async def release_unsubmitted(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        """Release startup work only when no authentication submit was claimed."""
        async with self._session.begin():
            return await self.release_unsubmitted_in_transaction(lease)

    async def release_unsubmitted_in_transaction(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        """Release within the caller's transaction with its application lease."""
        now = self._now()
        gate, attempt = await self._current_attempt_for_update(lease)
        if (
            gate is None
            or attempt is None
            or attempt.lease_expires_at <= now
            or attempt.auth_submit_count != 0
        ):
            return WorkdayGateMutation(applied=False, stale=True)
        attempt.status = "abandoned"
        gate.generation += 1
        if gate.user_min_until is not None and gate.user_min_until > now:
            gate.state = WorkdayGateState.COOLING_DOWN.value
            gate.next_eligible_at = _maximum_time(
                gate.next_eligible_at, gate.user_min_until
            )
        else:
            gate.state = WorkdayGateState.OPEN.value
        return WorkdayGateMutation(applied=True)

    async def confirm_account_lock(
        self,
        lease: WorkdayGateLease,
        *,
        trusted_portal_until: datetime | None = None,
    ) -> WorkdayGateMutation:
        """Start/extend a cooldown; stale trusted evidence may only lengthen it."""
        now = self._now()
        portal_until = _trusted_future_time(trusted_portal_until, now)
        default_until = now + self._default_lock_cooldown
        async with self._session.begin():
            gate = await self._gate_by_id_for_update(lease.gate_id)
            if gate is None:
                return WorkdayGateMutation(applied=False, stale=True)
            evidence = await self._attempt_by_authority_for_update(lease)
            if evidence is None:
                return WorkdayGateMutation(applied=False, stale=True)

            current_attempt = await self._active_attempt_for_update(gate.id)
            current = (
                gate.generation == lease.generation
                and current_attempt is not None
                and current_attempt.id == evidence.id
                and evidence.status == "active"
            )
            previous_until = _maximum_time(
                gate.cooldown_until, gate.user_min_until, gate.next_eligible_at
            )
            effective_until = _maximum_time(previous_until, default_until, portal_until)
            assert effective_until is not None

            if (
                not current
                and previous_until is not None
                and effective_until <= previous_until
            ):
                return WorkdayGateMutation(applied=False, stale=True)

            if current:
                evidence.status = "completed"
                gate.generation += 1
                gate.state = WorkdayGateState.COOLING_DOWN.value
            elif gate.state in {
                WorkdayGateState.OPEN.value,
                WorkdayGateState.PROBE_IN_PROGRESS.value,
            }:
                if (
                    current_attempt is not None
                    and current_attempt.auth_submit_count == 0
                ):
                    current_attempt.status = "abandoned"
                if current_attempt is None or current_attempt.auth_submit_count == 0:
                    gate.generation += 1
                    gate.state = WorkdayGateState.COOLING_DOWN.value

            gate.cooldown_until = effective_until
            gate.next_eligible_at = effective_until
            await self._ensure_cooldown_notice(
                gate_id=gate.id,
                application_id=lease.application_id,
                gate_generation=gate.generation,
                now=now,
            )
            event = WorkdayAccountLockEvent(
                gate_id=gate.id,
                application_id=lease.application_id,
                generation=gate.generation,
                cooldown_until=effective_until,
            )
            return WorkdayGateMutation(
                applied=True,
                stale=not current,
                lock_event=event,
            )

    async def _ensure_cooldown_notice(
        self,
        *,
        gate_id: UUID,
        application_id: UUID,
        gate_generation: int,
        now: datetime,
    ) -> None:
        """Create one notice while the gate row serializes this generation."""
        existing = (
            await self._session.execute(
                select(WorkdayCooldownNotice)
                .where(
                    WorkdayCooldownNotice.gate_id == gate_id,
                    WorkdayCooldownNotice.gate_generation == gate_generation,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        self._session.add(
            WorkdayCooldownNotice(
                id=uuid4(),
                gate_id=gate_id,
                application_id=application_id,
                gate_generation=gate_generation,
                status="pending",
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()

    async def _ensure_gate(
        self,
        *,
        user_id: UUID,
        account_ref: str,
        portal_scope: str,
        now: datetime,
    ) -> None:
        statement = (
            postgresql_insert(WorkdayAccountGate)
            .values(
                id=uuid4(),
                user_id=user_id,
                account_ref=account_ref,
                portal_scope=portal_scope,
                state=WorkdayGateState.OPEN.value,
                generation=0,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "account_ref", "portal_scope"]
            )
        )
        await self._session.execute(statement)

    async def _owned_application_for_update(
        self, *, application_id: UUID, user_id: UUID
    ) -> JobApplication:
        result = await self._session.execute(
            select(JobApplication)
            .where(
                JobApplication.id == application_id,
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
            )
            .with_for_update()
        )
        application = result.scalar_one_or_none()
        if application is None:
            raise WorkdayGateOwnershipError("Owned Workday application was not found.")
        return application

    async def _gate_by_identity_for_update(
        self, *, user_id: UUID, account_ref: str, portal_scope: str
    ) -> WorkdayAccountGate | None:
        result = await self._session.execute(
            select(WorkdayAccountGate)
            .where(
                WorkdayAccountGate.user_id == user_id,
                WorkdayAccountGate.account_ref == account_ref,
                WorkdayAccountGate.portal_scope == portal_scope,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _gate_by_id_for_update(self, gate_id: UUID) -> WorkdayAccountGate | None:
        result = await self._session.execute(
            select(WorkdayAccountGate)
            .where(WorkdayAccountGate.id == gate_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _active_attempt_for_update(
        self, gate_id: UUID
    ) -> WorkdayAuthAttempt | None:
        result = await self._session.execute(
            select(WorkdayAuthAttempt)
            .where(
                WorkdayAuthAttempt.gate_id == gate_id,
                WorkdayAuthAttempt.status == "active",
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _attempt_by_authority_for_update(
        self, lease: WorkdayGateLease
    ) -> WorkdayAuthAttempt | None:
        result = await self._session.execute(
            select(WorkdayAuthAttempt)
            .where(
                WorkdayAuthAttempt.gate_id == lease.gate_id,
                WorkdayAuthAttempt.application_id == lease.application_id,
                WorkdayAuthAttempt.generation == lease.generation,
                WorkdayAuthAttempt.lease_token == lease.lease_token,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _current_attempt_for_update(
        self, lease: WorkdayGateLease
    ) -> tuple[WorkdayAccountGate | None, WorkdayAuthAttempt | None]:
        gate = await self._gate_by_id_for_update(lease.gate_id)
        if gate is None or gate.generation != lease.generation:
            return gate, None
        attempt = await self._attempt_by_authority_for_update(lease)
        if attempt is None or attempt.status != "active":
            return gate, None
        return gate, attempt

    async def _grant(
        self,
        *,
        gate: WorkdayAccountGate,
        application_id: UUID,
        decision: WorkdayGateDecision,
        now: datetime,
    ) -> WorkdayGateAcquisition:
        gate.generation += 1
        if decision is WorkdayGateDecision.ONE_PROBE:
            gate.state = WorkdayGateState.PROBE_IN_PROGRESS.value
        token = secrets.token_urlsafe(32)
        attempt = WorkdayAuthAttempt(
            id=uuid4(),
            gate_id=gate.id,
            application_id=application_id,
            generation=gate.generation,
            lease_token=token,
            lease_expires_at=now + self._lease_ttl,
            heartbeat_at=now,
            secret_accessed=False,
            auth_submit_count=0,
            llm_repair_count=0,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._session.add(attempt)
        await self._session.flush()
        return self._acquisition(gate, decision, attempt=attempt)

    def _acquisition(
        self,
        gate: WorkdayAccountGate,
        decision: WorkdayGateDecision,
        *,
        attempt: WorkdayAuthAttempt | None = None,
        next_eligible_at: datetime | None = None,
    ) -> WorkdayGateAcquisition:
        return WorkdayGateAcquisition(
            decision=decision,
            gate_id=gate.id,
            generation=gate.generation,
            lease_token=attempt.lease_token if attempt is not None else None,
            lease_expires_at=(
                attempt.lease_expires_at if attempt is not None else None
            ),
            next_eligible_at=next_eligible_at,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkdayGateStoreError(
                "Gate clock must return a timezone-aware value."
            )
        return value.astimezone(UTC)


def create_workday_account_gate_store(
    session: AsyncSession,
    *,
    lock_cooldown_hours: int | None = None,
    store_factory=None,
) -> SQLAlchemyWorkdayAccountGateStore:
    """Build the production gate store with the configured lock wait."""
    hours = (
        get_settings().workday_account_lock_cooldown_hours
        if lock_cooldown_hours is None
        else lock_cooldown_hours
    )
    if hours <= 0:
        raise ValueError("Configured account-lock cooldown must be positive.")
    constructor = store_factory or SQLAlchemyWorkdayAccountGateStore
    cooldown = timedelta(hours=hours)
    parameters = signature(constructor).parameters
    accepts_keyword = "default_lock_cooldown" in parameters or any(
        item.kind is Parameter.VAR_KEYWORD for item in parameters.values()
    )
    if accepts_keyword:
        return constructor(session, default_lock_cooldown=cooldown)
    # Small protocol fakes can remain constructor-compatible in focused tests;
    # every real production constructor above receives the explicit setting.
    return constructor(session)


def _canonical_workday_scope(value: str) -> str:
    try:
        normalized = normalize_portal_scope(value)
    except PortalCredentialError as exc:
        raise WorkdayGateStoreError(
            "Canonical Workday portal scope is invalid."
        ) from exc
    if not normalized.startswith("workday:") or normalized != value:
        raise WorkdayGateStoreError(
            "Gate acquisition requires a server-canonicalized Workday portal scope."
        )
    return normalized


def _maximum_time(*values: datetime | None) -> datetime | None:
    aware = [_as_utc(value) for value in values if value is not None]
    return max(aware) if aware else None


def _trusted_future_time(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    trusted = _as_utc(value)
    return trusted if trusted > now else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkdayGateStoreError("Gate timestamps must be timezone-aware.")
    return value.astimezone(UTC)
