"""Owner-scoped Workday cooldown notices and atomic user decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    JobApplication,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
    WorkdayCooldownNotice,
)
from services.application_soft_delete import (
    ApplicationSoftDeleteNotFoundError,
    soft_delete_owned_application_in_transaction,
)
from services.workday_transition_contracts import WorkdayGateState


class WorkdayCooldownNoticeError(RuntimeError):
    """Base error for a rejected cooldown-notice operation."""


class WorkdayCooldownNoticeNotFoundError(WorkdayCooldownNoticeError):
    """The notice is absent or outside the authenticated user's ownership."""


class WorkdayCooldownNoticeConflictError(WorkdayCooldownNoticeError):
    """The notice is already decided or its protected state is inconsistent."""


class WorkdayCooldownDecision(str, Enum):
    KEEP = "keep"
    EXTEND = "extend"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class WorkdayCooldownNoticeView:
    id: UUID
    application_id: UUID
    status: str
    safe_next_attempt_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkdayCooldownDecisionResult:
    notice: WorkdayCooldownNoticeView
    invalidated_session_id: str | None = None


class SQLAlchemyWorkdayCooldownNoticeStore:
    """Apply terminal notice decisions without creating a retry path."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list_pending(self, *, user_id: UUID) -> list[WorkdayCooldownNoticeView]:
        """Return only pending notices for active applications owned by the user."""
        rows = (
            await self._session.execute(
                select(WorkdayCooldownNotice, WorkdayAccountGate)
                .join(
                    WorkdayAccountGate,
                    WorkdayAccountGate.id == WorkdayCooldownNotice.gate_id,
                )
                .join(
                    JobApplication,
                    JobApplication.id == WorkdayCooldownNotice.application_id,
                )
                .where(
                    WorkdayAccountGate.user_id == user_id,
                    JobApplication.user_id == user_id,
                    JobApplication.workday_account_gate_id
                    == WorkdayCooldownNotice.gate_id,
                    JobApplication.deleted_at.is_(None),
                    WorkdayCooldownNotice.status == "pending",
                )
                .order_by(WorkdayCooldownNotice.created_at)
            )
        ).all()
        views: list[WorkdayCooldownNoticeView] = []
        for notice, gate in rows:
            safe_until = _effective_until(gate)
            if safe_until is not None:
                views.append(_view(notice, safe_until=safe_until))
        return views

    async def decide(
        self,
        *,
        user_id: UUID,
        notice_id: UUID,
        decision: WorkdayCooldownDecision,
        extend_until: datetime | None = None,
        confirm_delete: bool = False,
    ) -> WorkdayCooldownDecisionResult:
        """Atomically keep, extend, or soft-delete one owned notice target."""
        now = self._now()
        selected_until = _validated_extension(
            decision=decision,
            extend_until=extend_until,
            now=now,
        )
        if decision is WorkdayCooldownDecision.DELETE and not confirm_delete:
            raise ValueError("Delete requires explicit confirmation.")
        if decision is not WorkdayCooldownDecision.DELETE and confirm_delete:
            raise ValueError("Delete confirmation is valid only for delete.")

        async with self._session.begin():
            locator = (
                await self._session.execute(
                    select(WorkdayCooldownNotice).where(
                        WorkdayCooldownNotice.id == notice_id
                    )
                )
            ).scalar_one_or_none()
            if locator is None:
                raise WorkdayCooldownNoticeNotFoundError("Notice not found.")

            application = (
                await self._session.execute(
                    select(JobApplication)
                    .where(
                        JobApplication.id == locator.application_id,
                        JobApplication.user_id == user_id,
                        JobApplication.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            gate = (
                await self._session.execute(
                    select(WorkdayAccountGate)
                    .where(
                        WorkdayAccountGate.id == locator.gate_id,
                        WorkdayAccountGate.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            notice = (
                await self._session.execute(
                    select(WorkdayCooldownNotice)
                    .where(
                        WorkdayCooldownNotice.id == notice_id,
                        WorkdayCooldownNotice.gate_id == locator.gate_id,
                        WorkdayCooldownNotice.application_id == locator.application_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                application is None
                or gate is None
                or notice is None
                or application.workday_account_gate_id != gate.id
            ):
                raise WorkdayCooldownNoticeNotFoundError("Notice not found.")
            if notice.status != "pending":
                raise WorkdayCooldownNoticeConflictError(
                    "Cooldown notice was already decided."
                )

            safe_until = _effective_until(gate)
            if safe_until is None:
                raise WorkdayCooldownNoticeConflictError(
                    "Cooldown notice has no protected wait."
                )
            invalidated_session_id: str | None = None

            if decision is WorkdayCooldownDecision.KEEP:
                notice.status = "kept"
            elif decision is WorkdayCooldownDecision.EXTEND:
                assert selected_until is not None
                safe_until = max(safe_until, selected_until)
                gate.user_min_until = safe_until
                gate.cooldown_until = safe_until
                gate.next_eligible_at = safe_until
                attempt = await self._active_attempt_for_update(gate.id)
                if attempt is not None and attempt.auth_submit_count == 0:
                    attempt.status = "abandoned"
                    gate.generation += 1
                    gate.state = WorkdayGateState.COOLING_DOWN.value
                    application.automation_lease_id = None
                    application.automation_lease_expires_at = None
                notice.status = "extended"
            else:
                try:
                    deletion = await soft_delete_owned_application_in_transaction(
                        self._session,
                        application_id=application.id,
                        user_id=user_id,
                        now=now,
                    )
                except ApplicationSoftDeleteNotFoundError as exc:
                    raise WorkdayCooldownNoticeNotFoundError(
                        "Notice not found."
                    ) from exc
                invalidated_session_id = deletion.session_id
                attempt = await self._active_attempt_for_update(gate.id)
                if attempt is not None and attempt.application_id == application.id:
                    if attempt.auth_submit_count == 0:
                        attempt.status = "abandoned"
                        gate.generation += 1
                        gate.state = WorkdayGateState.COOLING_DOWN.value
                    else:
                        attempt.status = "review_required"
                        gate.generation += 1
                        gate.state = WorkdayGateState.REVIEW_REQUIRED.value
                application.automation_lease_id = None
                application.automation_lease_expires_at = None
                notice.status = "deleted"

            notice.decided_at = now
            notice.updated_at = now
            return WorkdayCooldownDecisionResult(
                notice=_view(notice, safe_until=safe_until),
                invalidated_session_id=invalidated_session_id,
            )

    async def _active_attempt_for_update(
        self, gate_id: UUID
    ) -> WorkdayAuthAttempt | None:
        return (
            await self._session.execute(
                select(WorkdayAuthAttempt)
                .where(
                    WorkdayAuthAttempt.gate_id == gate_id,
                    WorkdayAuthAttempt.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkdayCooldownNoticeError(
                "Cooldown notice clock must return a timezone-aware value."
            )
        return value.astimezone(UTC)


def _validated_extension(
    *,
    decision: WorkdayCooldownDecision,
    extend_until: datetime | None,
    now: datetime,
) -> datetime | None:
    if decision is not WorkdayCooldownDecision.EXTEND:
        if extend_until is not None:
            raise ValueError("An extension time is valid only for extend.")
        return None
    if (
        extend_until is None
        or extend_until.tzinfo is None
        or extend_until.utcoffset() is None
    ):
        raise ValueError("Extend requires a timezone-aware future UTC time.")
    selected = extend_until.astimezone(UTC)
    if selected <= now:
        raise ValueError("Extend requires a future UTC time.")
    return selected


def _effective_until(gate: WorkdayAccountGate) -> datetime | None:
    values = [
        value.astimezone(UTC)
        for value in (
            gate.cooldown_until,
            gate.user_min_until,
            gate.next_eligible_at,
        )
        if value is not None
    ]
    return max(values) if values else None


def _view(
    notice: WorkdayCooldownNotice, *, safe_until: datetime
) -> WorkdayCooldownNoticeView:
    return WorkdayCooldownNoticeView(
        id=notice.id,
        application_id=notice.application_id,
        status=notice.status,
        safe_next_attempt_at=safe_until,
        created_at=notice.created_at,
    )
