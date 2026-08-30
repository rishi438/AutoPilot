"""Private, one-submit Workday authentication boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Awaitable, Callable, Protocol
from uuid import UUID

from services.portal_credentials import WorkerPortalCredential, normalize_portal_scope
from services.workday_account_gate_store import (
    WorkdayAuthGateBinding,
    WorkdayGateLease,
    WorkdayGateMutation,
)
from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureRoute,
    WorkdayGateOperation,
    route_workday_failure,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionState,
)

logger = logging.getLogger(__name__)


class WorkdayAuthBrokerError(RuntimeError):
    """Sanitized failure before authentication could be submitted safely."""

    def __init__(
        self,
        message: str,
        *,
        submitted: bool = False,
        phase: WorkdayAuthExecutionPhase | None = None,
    ) -> None:
        super().__init__(message)
        self.submitted = submitted
        self.phase = phase or (
            WorkdayAuthExecutionPhase.SUBMIT_CLAIMED_PENDING
            if submitted
            else WorkdayAuthExecutionPhase.PRE_SUBMIT
        )


class WorkdayAuthExecutionPhase(str, Enum):
    """Phase reached by the private authentication operation."""

    PRE_SUBMIT = "pre_submit"
    SECRET_ACCESSED_NO_SUBMIT = "secret_accessed_no_submit"
    SUBMIT_CLAIMED_PENDING = "submit_claimed_pending"


@dataclass(frozen=True, slots=True)
class WorkdayAuthBrokerRequest:
    """Private server-issued authority for one existing-account login."""

    user_id: UUID
    account_ref: UUID
    portal_scope: str
    gate_lease: WorkdayGateLease

    def __post_init__(self) -> None:
        scope = normalize_portal_scope(self.portal_scope)
        if scope != self.portal_scope or not scope.startswith("workday:"):
            raise ValueError("A canonical Workday portal scope is required.")


@dataclass(frozen=True, slots=True)
class WorkdayAuthAccountBinding:
    """Private in-process proof that the successful login used this account."""

    account_ref: UUID = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkdayPostSubmitObservation:
    """Typed, credential-free facts from one read-only observation."""

    failure_classes: frozenset[WorkdayFailureClass]
    terminal: bool = True
    trusted_portal_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_classes", frozenset(self.failure_classes))
        allowed = {
            WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
            WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
            WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
            WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
            WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN,
        }
        if not self.failure_classes.issubset(allowed):
            raise ValueError("Post-submit observations must use post-submit classes.")


@dataclass(frozen=True, slots=True)
class WorkdayAuthBrokerResult:
    """Result after the one-submit boundary; success remains provisional."""

    route: WorkdayFailureRoute
    observations: int
    provisional_success: bool = False
    account_binding: WorkdayAuthAccountBinding | None = field(default=None, repr=False)


class WorkdayAuthGateStore(Protocol):
    """Narrow private gate operations used by the broker."""

    async def verify_auth_lease(
        self, lease: WorkdayGateLease, binding: WorkdayAuthGateBinding
    ) -> bool: ...

    async def mark_secret_accessed(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...

    async def claim_auth_submit(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...

    async def complete_success(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...

    async def confirm_account_lock(
        self,
        lease: WorkdayGateLease,
        *,
        trusted_portal_until: datetime | None = None,
    ) -> WorkdayGateMutation: ...

    async def mark_review_required(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...


class WorkdayAuthCredentialReader(Protocol):
    """Exact owned-vault lookup; no list or scope-only lookup is permitted."""

    async def credential_for_auth_broker(
        self, *, user_id: str, account_ref: UUID, portal_scope: str
    ) -> WorkerPortalCredential | None: ...


class WorkdayAuthPageActions(Protocol):
    """Only the broker may receive this login fill/submit page surface."""

    async def verify_unique_auth_controls(self, *, expected_scope: str) -> bool: ...

    async def fill_verified_auth_controls(
        self, credential: WorkerPortalCredential
    ) -> None: ...

    async def click_verified_sign_in(self) -> None: ...


class WorkdaySafeStateObserver(Protocol):
    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState: ...


class WorkdayAuthOutcomeObserver(Protocol):
    """Read-only observer; implementations cannot expose page mutation methods."""

    async def observe_post_submit(self) -> WorkdayPostSubmitObservation: ...


class WorkdayAuthBroker:
    """Own credential access, one durable submit claim, and read-only routing."""

    def __init__(
        self,
        *,
        gate_store: WorkdayAuthGateStore,
        credential_reader: WorkdayAuthCredentialReader,
        page_actions: WorkdayAuthPageActions,
        state_observer: WorkdaySafeStateObserver,
        outcome_observer: WorkdayAuthOutcomeObserver,
        max_observations: int = 60,
        poll_interval_seconds: float = 0.25,
        poll_wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_observations < 1 or max_observations > 120:
            raise ValueError("Observation count must be between 1 and 120.")
        if poll_interval_seconds < 0 or poll_interval_seconds > 5:
            raise ValueError("Observation poll interval is outside its safe bound.")
        self._gate_store = gate_store
        self._credential_reader = credential_reader
        self._page_actions = page_actions
        self._state_observer = state_observer
        self._outcome_observer = outcome_observer
        self._max_observations = max_observations
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_wait = poll_wait

    async def authenticate(
        self, request: WorkdayAuthBrokerRequest
    ) -> WorkdayAuthBrokerResult:
        """Authenticate once; a claimed submit can never be retried by this broker."""
        observed = await self._state_observer.observe()
        if (
            observed.state is not WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
            or observed.portal_family != "workday"
            or observed.tenant_scope != request.portal_scope
        ):
            raise WorkdayAuthBrokerError(
                "Authentication did not start from a verified Workday auth form."
            )
        if not await self._page_actions.verify_unique_auth_controls(
            expected_scope=request.portal_scope
        ):
            raise WorkdayAuthBrokerError(
                "The active Workday dialog does not contain unique login controls."
            )

        binding = WorkdayAuthGateBinding(
            user_id=request.user_id,
            account_ref=request.account_ref,
            portal_scope=request.portal_scope,
        )
        if not await self._gate_store.verify_auth_lease(request.gate_lease, binding):
            raise WorkdayAuthBrokerError("Authentication gate authority is stale.")

        secret_claim = await self._gate_store.mark_secret_accessed(request.gate_lease)
        if not secret_claim.applied:
            raise WorkdayAuthBrokerError(
                "Authentication secret access was already claimed or is stale."
            )
        credential: WorkerPortalCredential | None = None
        vault_read_failed = False
        try:
            credential = await self._credential_reader.credential_for_auth_broker(
                user_id=str(request.user_id),
                account_ref=request.account_ref,
                portal_scope=request.portal_scope,
            )
        except Exception:
            vault_read_failed = True
        if vault_read_failed:
            raise WorkdayAuthBrokerError(
                "The owned Workday credential could not be read.",
                phase=WorkdayAuthExecutionPhase.SECRET_ACCESSED_NO_SUBMIT,
            )
        if credential is None or credential.portal_scope != request.portal_scope:
            raise WorkdayAuthBrokerError(
                "The owned Workday credential was not available.",
                phase=WorkdayAuthExecutionPhase.SECRET_ACCESSED_NO_SUBMIT,
            )
        fill_failed = False
        try:
            await self._page_actions.fill_verified_auth_controls(credential)
        except Exception:
            fill_failed = True
        if fill_failed:
            raise WorkdayAuthBrokerError(
                "The verified Workday login controls could not be filled.",
                phase=WorkdayAuthExecutionPhase.SECRET_ACCESSED_NO_SUBMIT,
            )

        submit_claim = await self._gate_store.claim_auth_submit(request.gate_lease)
        if not submit_claim.applied:
            raise WorkdayAuthBrokerError(
                "Authentication submission was already claimed or is stale.",
                phase=WorkdayAuthExecutionPhase.SECRET_ACCESSED_NO_SUBMIT,
            )

        try:
            await self._page_actions.click_verified_sign_in()
        except Exception:
            return await self._route_and_apply(
                request,
                WorkdayPostSubmitObservation(
                    frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN})
                ),
                observations=0,
                timeout=True,
            )

        latest: WorkdayPostSubmitObservation | None = None
        for index in range(self._max_observations):
            try:
                latest = await self._outcome_observer.observe_post_submit()
            except Exception as exc:
                logger.info(
                    "workday_auth_post_submit_observation_failed "
                    "observation=%s error_type=%s",
                    index + 1,
                    type(exc).__name__,
                )
                latest = WorkdayPostSubmitObservation(
                    frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN})
                )
                return await self._route_and_apply(
                    request,
                    latest,
                    observations=index + 1,
                    network_ambiguity=True,
                )
            if latest.terminal:
                return await self._route_and_apply(
                    request, latest, observations=index + 1
                )
            if index + 1 < self._max_observations:
                await self._poll_wait(self._poll_interval_seconds)

        timeout_observation = latest or WorkdayPostSubmitObservation(frozenset())
        return await self._route_and_apply(
            request,
            timeout_observation,
            observations=self._max_observations,
            timeout=True,
        )

    async def _route_and_apply(
        self,
        request: WorkdayAuthBrokerRequest,
        observation: WorkdayPostSubmitObservation,
        *,
        observations: int,
        timeout: bool = False,
        network_ambiguity: bool = False,
    ) -> WorkdayAuthBrokerResult:
        route = route_workday_failure(
            WorkdayFailureFacts(
                observed_classes=observation.failure_classes,
                auth_submit_count=1,
                timeout_after_submit=timeout,
                network_ambiguity_after_submit=network_ambiguity,
            )
        )
        operation = route.gate_operation
        if operation is WorkdayGateOperation.COMPLETE_SUCCESS:
            # The final Unit 1 checkpoint owns gate completion.  Keeping this
            # attempt pending makes a crash or failed proof recoverable without
            # allowing another physical authentication submit.
            return WorkdayAuthBrokerResult(
                route=route,
                observations=observations,
                provisional_success=True,
                account_binding=WorkdayAuthAccountBinding(request.account_ref),
            )
        elif operation is WorkdayGateOperation.CONFIRM_ACCOUNT_LOCK:
            mutation = await self._gate_store.confirm_account_lock(
                request.gate_lease,
                trusted_portal_until=observation.trusted_portal_until,
            )
        elif operation is WorkdayGateOperation.MARK_REVIEW_REQUIRED:
            mutation = await self._gate_store.mark_review_required(request.gate_lease)
        else:
            raise WorkdayAuthBrokerError(
                "Post-submit routing selected an invalid gate operation."
            )
        if not mutation.applied:
            raise WorkdayAuthBrokerError(
                "Post-submit gate routing was stale.",
                submitted=True,
                phase=WorkdayAuthExecutionPhase.SUBMIT_CLAIMED_PENDING,
            )
        return WorkdayAuthBrokerResult(route=route, observations=observations)
