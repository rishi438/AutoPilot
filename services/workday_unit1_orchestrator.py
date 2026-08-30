"""Bounded orchestration for Workday Unit 1 authentication readiness."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import asyncio
import logging
from time import perf_counter
from typing import Callable, Protocol

from services.portal_account_automation import (
    NativeAccountPageState,
    derive_workday_portal_scope,
)
from services.portal_control_resolver import PortalControlIntent
from services.workday_account_gate_store import WorkdayGateLease
from services.workday_auth_broker import (
    WorkdayAuthAccountBinding,
    WorkdayAuthBrokerError,
    WorkdayAuthBrokerRequest,
    WorkdayAuthBrokerResult,
    WorkdayPostSubmitObservation,
)
from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureOutcome,
    WorkdayFailureRoute,
    WorkdayGateOperation,
    route_workday_failure,
)
from services.workday_state_observer import WorkdayStateSecurityError
from services.workday_page_condition import (
    WorkdayPageConditionError,
    WorkdayUnit1PageCondition,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionKey,
    WorkdayTransitionState,
)
from services.workday_transition_engine import (
    TransitionPreconditionFailed,
    TransitionRepairRequired,
    TransitionReplayError,
    TransitionReplayOutcome,
    TransitionReplayStatus,
)
from services.workday_transition_repair import (
    WorkdayRepairAttemptFacts,
    WorkdayTransitionRepairOutcome,
    WorkdayTransitionRepairRequest,
    WorkdayTransitionRepairStatus,
)
from services.workday_worker_api import LeasedWorkdayApplication

_EXECUTOR_POLICY_VERSION = 1
_MAX_DISPATCH_STEPS = 5
_LOGIN_FORM_HYDRATION_ATTEMPTS = 4
_LOGIN_FORM_HYDRATION_INTERVAL_MS = 250
_CHECKPOINT_DEADLINE_SECONDS = 2.0
_TRANSITIONS = {
    WorkdayTransitionState.JOB_PAGE: (
        "open_apply",
        PortalControlIntent.APPLY,
    ),
    WorkdayTransitionState.APPLY_CHOICES: (
        "select_apply_manually",
        PortalControlIntent.APPLY_MANUALLY,
    ),
    WorkdayTransitionState.ACCOUNT_PAGE: (
        "open_existing_sign_in",
        PortalControlIntent.SIGN_IN,
    ),
}
_REPLAY_SUCCESS = frozenset(
    {
        TransitionReplayStatus.CURRENT_SUCCEEDED,
        TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_UPDATED,
        TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_CONFLICT,
        TransitionReplayStatus.DIRECT_AUTHENTICATED_SESSION,
    }
)
logger = logging.getLogger(__name__)


class WorkdayUnit1Status(str, Enum):
    """Credential-free terminal outcomes for one Unit 1 run."""

    COMPLETE = "complete"
    DEFERRED = "deferred"
    HELD = "held"
    REVIEW_REQUIRED = "review_required"
    SKIPPED = "skipped"
    BACKOFF = "backoff"


class WorkdayExecutionPhase(str, Enum):
    """The irreversible boundary reached by one Unit 1 execution."""

    BEFORE_BROWSER_ACTION = "before_browser_action"
    PRE_SUBMIT = "pre_submit"
    SECRET_ACCESSED_NO_SUBMIT = "secret_accessed_no_submit"
    SUBMIT_CLAIMED_PENDING = "submit_claimed_pending"
    STALE_AUTHORITY = "stale_authority"


class WorkdayUnit1ExecutionError(RuntimeError):
    """Typed execution failure that preserves the reached safety phase."""

    def __init__(self, message: str, *, phase: WorkdayExecutionPhase) -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(frozen=True, slots=True)
class WorkdayUnit1CheckpointFacts:
    """Private checks that must all hold for the final checkpoint."""

    approved_https_origin: bool
    canonical_tenant_verified: bool
    leased_job_context_matches: bool
    leased_application_context_matches: bool
    external_account_matches: bool
    no_login_or_auth_error: bool
    no_captcha_or_otp_or_lock: bool
    basic_information_control_hydrated: bool
    safe_signature: str = ""

    @property
    def satisfied(self) -> bool:
        return all(
            (
                self.approved_https_origin,
                self.canonical_tenant_verified,
                self.leased_job_context_matches,
                self.leased_application_context_matches,
                self.external_account_matches,
                self.no_login_or_auth_error,
                self.no_captcha_or_otp_or_lock,
                self.basic_information_control_hydrated,
            )
        )


@dataclass(frozen=True, slots=True)
class WorkdayUnit1Result:
    """Safe orchestration result; page content and identity are excluded."""

    status: WorkdayUnit1Status
    final_state: WorkdayTransitionState | None = None
    route: WorkdayFailureRoute | None = None
    stable_observations: int = 0


class WorkdayUnit1Page(Protocol):
    async def open_approved_job(
        self, target_url: str
    ) -> WorkdayUnit1PageCondition | None: ...

    async def capture_page_condition(self) -> WorkdayUnit1PageCondition: ...

    async def observe_post_submit(self) -> WorkdayPostSubmitObservation: ...


class WorkdayUnit1Observer(Protocol):
    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState: ...


class WorkdayUnit1Replay(Protocol):
    async def replay(
        self,
        *,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
    ) -> TransitionReplayOutcome: ...


class WorkdayUnit1Repair(Protocol):
    async def repair(
        self, request: WorkdayTransitionRepairRequest
    ) -> WorkdayTransitionRepairOutcome: ...


class WorkdayUnit1AuthBroker(Protocol):
    async def authenticate(
        self, request: WorkdayAuthBrokerRequest
    ) -> WorkdayAuthBrokerResult: ...


class WorkdayUnit1PrivateContext(Protocol):
    async def auth_request(
        self, *, lease: LeasedWorkdayApplication, portal_scope: str
    ) -> WorkdayAuthBrokerRequest: ...

    async def checkpoint_facts(
        self,
        *,
        lease: LeasedWorkdayApplication,
        observation: WorkdayObservedState,
    ) -> WorkdayUnit1CheckpointFacts: ...

    async def accept_auth_binding(self, binding: WorkdayAuthAccountBinding) -> None: ...


class WorkdayUnit1Persistence(Protocol):
    async def apply_route(
        self,
        *,
        lease: LeasedWorkdayApplication,
        route: WorkdayFailureRoute,
        review_gate_mutation_applied: bool = False,
    ) -> None: ...

    async def complete_unit(
        self, *, lease: LeasedWorkdayApplication, authentication_submitted: bool
    ) -> None: ...

    async def review_unit(
        self, *, lease: LeasedWorkdayApplication, authentication_submitted: bool
    ) -> None: ...

    async def recover_observe_only(
        self,
        *,
        lease: LeasedWorkdayApplication,
        route: WorkdayFailureRoute,
        trusted_portal_until=None,
    ) -> None: ...


class WorkdayUnit1Events(Protocol):
    async def emit(self, event: dict[str, object]) -> None: ...


class WorkdayUnit1Orchestrator:
    """Compose Tasks 06-12 without adding a browser, vault, queue, or model."""

    def __init__(
        self,
        *,
        page: WorkdayUnit1Page,
        observer: WorkdayUnit1Observer,
        replay: WorkdayUnit1Replay,
        repair: WorkdayUnit1Repair,
        auth_broker: WorkdayUnit1AuthBroker,
        private_context: WorkdayUnit1PrivateContext,
        persistence: WorkdayUnit1Persistence,
        events: WorkdayUnit1Events,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._page = page
        self._observer = observer
        self._replay = replay
        self._repair = repair
        self._auth_broker = auth_broker
        self._private_context = private_context
        self._persistence = persistence
        self._events = events
        self._clock = clock

    async def run(self, lease: LeasedWorkdayApplication) -> WorkdayUnit1Result:
        """Advance one leased job to a proven, hydrated Basic Information page."""
        if lease.gate_decision == "observe_only":
            return await self._run_observe_only(lease)

        target_url = lease.to_workday_lease().target_url
        portal_scope = derive_workday_portal_scope(target_url)
        gate_lease = WorkdayGateLease(
            gate_id=lease.gate_id,
            application_id=lease.application_id,
            generation=lease.gate_generation,
            lease_token=lease.gate_lease_token,
        )
        try:
            opened_condition = await self._page.open_approved_job(target_url)
            condition = await self._capture_condition(opened_condition)
            failure_class = self._condition_failure(condition)
            if failure_class is not None:
                return await self._apply_failure(lease, failure_class)
            observed = await self._observer.observe(include_candidates=True)
        except WorkdayPageConditionError as exc:
            return await self._apply_failure(lease, exc.failure_class)
        except WorkdayStateSecurityError:
            return await self._apply_failure(
                lease, WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
            )
        except Exception as exc:
            logger.warning(
                "workday_unit1_navigation_or_observation_failed application_id=%s error_type=%s",
                lease.application_id,
                type(exc).__name__,
            )
            return await self._apply_failure(
                lease, WorkdayFailureClass.PRE_SUBMIT_TRANSIENT
            )

        logger.info(
            "workday_unit1_state_ready phase=initial state=%s",
            observed.state.value,
        )

        session_authenticated = bool(
            condition is not None and condition.already_authenticated
        )
        dispatch_steps = 0
        while dispatch_steps < _MAX_DISPATCH_STEPS:
            if session_authenticated and (
                observed.state
                is not WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            ):
                return await self._apply_session_review(lease)

            dispatch_kind, transition = self._dispatch_state(observed.state)
            if dispatch_kind == "terminal":
                break
            if dispatch_kind == "safe_hold":
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
            if dispatch_kind == "hydrate":
                try:
                    hydrated = await self._hydrate_login_form()
                except WorkdayPageConditionError as exc:
                    return await self._apply_failure(lease, exc.failure_class)
                except WorkdayStateSecurityError:
                    return await self._apply_failure(
                        lease, WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
                    )
                if hydrated is None:
                    return await self._apply_failure(
                        lease, WorkdayFailureClass.ANYTHING_ELSE
                    )
                observed = hydrated
                continue

            if transition is None:
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
            dispatch_steps += 1
            task_type, intent = transition
            key = WorkdayTransitionKey(
                portal_family="workday",
                tenant_scope=portal_scope,
                task_type=task_type,
                from_state_signature=observed.safe_signature,
                action_intent=intent,
                signature_version=observed.signature_version,
                executor_policy_version=_EXECUTOR_POLICY_VERSION,
            )
            started = self._clock()
            try:
                replay_observed = getattr(self._replay, "replay_observed", None)
                if callable(replay_observed):
                    replayed = await replay_observed(
                        key=key,
                        expected_from_state=observed.state,
                        observed=observed,
                    )
                else:
                    replayed = await self._replay.replay(
                        key=key, expected_from_state=observed.state
                    )
            except TransitionRepairRequired as exc:
                if exc.ticket is None:
                    return await self._apply_failure(
                        lease, WorkdayFailureClass.ANYTHING_ELSE
                    )
                route = self._route(WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH)
                repair_request = WorkdayTransitionRepairRequest(
                    route=route,
                    ticket=exc.ticket,
                    gate_lease=gate_lease,
                    facts=WorkdayRepairAttemptFacts(
                        approved_https_origin=True,
                        canonical_tenant_verified=True,
                        stable_from_state=True,
                    ),
                )
                repair_observed = getattr(self._repair, "repair_observed", None)
                if callable(repair_observed):
                    repaired = await repair_observed(
                        repair_request,
                        observed=observed,
                    )
                else:
                    repaired = await self._repair.repair(repair_request)
                if repaired.failure_class is not None:
                    return await self._apply_failure(lease, repaired.failure_class)
                await self._emit_transition(
                    key=key,
                    version_id=None,
                    from_state=observed.state,
                    to_state=repaired.observed_state,
                    decision=repaired.status.value,
                    started=started,
                )
                if (
                    repaired.status
                    not in {
                        WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED,
                        WorkdayTransitionRepairStatus.DIRECT_AUTHENTICATED_SESSION,
                    }
                    or repaired.observed_state is None
                ):
                    return await self._apply_failure(
                        lease,
                        WorkdayFailureClass.ANYTHING_ELSE,
                        review_gate_mutation_applied=repaired.hold_applied,
                    )
                try:
                    condition = await self._capture_condition(None)
                except WorkdayPageConditionError as condition_error:
                    return await self._apply_failure(
                        lease, condition_error.failure_class
                    )
                failure_class = self._condition_failure(condition)
                if failure_class is not None:
                    return await self._apply_failure(lease, failure_class)
                try:
                    observed = await self._observer.observe(include_candidates=True)
                except WorkdayPageConditionError as condition_error:
                    return await self._apply_failure(
                        lease, condition_error.failure_class
                    )
                except WorkdayStateSecurityError:
                    return await self._apply_failure(
                        lease, WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
                    )
                except Exception:
                    return await self._apply_failure(
                        lease, WorkdayFailureClass.ANYTHING_ELSE
                    )
                continue
            except WorkdayPageConditionError as exc:
                return await self._apply_failure(lease, exc.failure_class)
            except WorkdayStateSecurityError:
                return await self._apply_failure(
                    lease, WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
                )
            except TransitionPreconditionFailed:
                logger.info("workday_unit1_replay_rejected reason=precondition_changed")
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
            except TransitionReplayError:
                logger.info("workday_unit1_replay_rejected reason=replay_error")
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
            except Exception as exc:
                logger.warning(
                    "workday_unit1_replay_failed application_id=%s error_type=%s",
                    lease.application_id,
                    type(exc).__name__,
                )
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )

            if replayed.failure_class is not None:
                return await self._apply_failure(lease, replayed.failure_class)
            try:
                condition = await self._capture_condition(None)
            except WorkdayPageConditionError as exc:
                return await self._apply_failure(lease, exc.failure_class)
            failure_class = self._condition_failure(condition)
            if failure_class is not None:
                return await self._apply_failure(lease, failure_class)

            await self._emit_transition(
                key=key,
                version_id=str(replayed.version_id),
                from_state=observed.state,
                to_state=replayed.observed_state,
                decision=replayed.status.value,
                started=started,
            )
            if replayed.status not in _REPLAY_SUCCESS:
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
            try:
                observed = await self._observer.observe(include_candidates=True)
            except WorkdayPageConditionError as exc:
                return await self._apply_failure(lease, exc.failure_class)
            except WorkdayStateSecurityError:
                return await self._apply_failure(
                    lease, WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
                )
            except Exception:
                return await self._apply_failure(
                    lease, WorkdayFailureClass.ANYTHING_ELSE
                )
        else:
            return await self._apply_failure(lease, WorkdayFailureClass.ANYTHING_ELSE)

        authentication_submitted = False
        if observed.state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY:
            try:
                auth_request = await self._private_context.auth_request(
                    lease=lease, portal_scope=portal_scope
                )
                auth_result = await self._auth_broker.authenticate(auth_request)
            except WorkdayAuthBrokerError as exc:
                if exc.submitted:
                    route = self._checkpoint_failure_route(
                        authentication_submitted=True
                    )
                    await self._persistence.review_unit(
                        lease=lease, authentication_submitted=True
                    )
                    return self._result(route)
                route = self._route(WorkdayFailureClass.PRE_SUBMIT_TRANSIENT)
                await self._persistence.apply_route(lease=lease, route=route)
                return self._result(route)
            except Exception:
                route = self._route(WorkdayFailureClass.PRE_SUBMIT_TRANSIENT)
                await self._persistence.apply_route(lease=lease, route=route)
                return self._result(route)
            if auth_result.route.outcome is not WorkdayFailureOutcome.COMPLETE_UNIT:
                await self._persistence.apply_route(
                    lease=lease, route=auth_result.route
                )
                return self._result(auth_result.route)
            if (
                not auth_result.provisional_success
                or auth_result.account_binding is None
            ):
                route = self._checkpoint_failure_route(authentication_submitted=True)
                await self._persistence.review_unit(
                    lease=lease, authentication_submitted=True
                )
                return self._result(route)
            accept_binding = getattr(self._private_context, "accept_auth_binding", None)
            if not callable(accept_binding):
                route = self._checkpoint_failure_route(authentication_submitted=True)
                await self._persistence.review_unit(
                    lease=lease, authentication_submitted=True
                )
                return self._result(route)
            await accept_binding(auth_result.account_binding)
            authentication_submitted = True

        proof = await self._prove_checkpoint(lease)
        if proof is None:
            route = self._checkpoint_failure_route(
                authentication_submitted=authentication_submitted
            )
            if authentication_submitted:
                await self._persistence.review_unit(
                    lease=lease, authentication_submitted=True
                )
            else:
                await self._persistence.apply_route(lease=lease, route=route)
            return self._result(route)
        await self._persistence.complete_unit(
            lease=lease, authentication_submitted=authentication_submitted
        )
        return WorkdayUnit1Result(
            WorkdayUnit1Status.COMPLETE,
            final_state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            stable_observations=2,
        )

    async def _run_observe_only(
        self, lease: LeasedWorkdayApplication
    ) -> WorkdayUnit1Result:
        """Recover a claimed submit using only fresh read-only observations."""
        observe = getattr(self._page, "observe_post_submit", None)
        if not callable(observe):
            return await self._review_observe_only(lease)
        try:
            observation = await observe()
        except Exception as exc:
            logger.warning(
                "workday_unit1_observe_only_failed application_id=%s error_type=%s",
                lease.application_id,
                type(exc).__name__,
            )
            return await self._review_observe_only(lease)
        if not isinstance(observation, WorkdayPostSubmitObservation):
            return await self._review_observe_only(lease)
        route = route_workday_failure(
            WorkdayFailureFacts(
                observed_classes=observation.failure_classes,
                auth_submit_count=1,
            )
        )
        if route.failure_class is WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS:
            proof = await self._prove_checkpoint(lease)
            if proof is not None:
                await self._persistence.complete_unit(
                    lease=lease, authentication_submitted=True
                )
                return WorkdayUnit1Result(
                    WorkdayUnit1Status.COMPLETE,
                    final_state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
                    stable_observations=2,
                )
            return await self._review_observe_only(lease)
        recover = getattr(self._persistence, "recover_observe_only", None)
        if callable(recover):
            await recover(
                lease=lease,
                route=route,
                trusted_portal_until=observation.trusted_portal_until,
            )
        else:
            # Compatibility for protocol fakes; production persistence always
            # uses the guarded recovery method above.
            await self._persistence.apply_route(lease=lease, route=route)
        return self._result(route)

    async def _review_observe_only(
        self, lease: LeasedWorkdayApplication
    ) -> WorkdayUnit1Result:
        route = route_workday_failure(
            WorkdayFailureFacts(
                frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN}),
                auth_submit_count=1,
            )
        )
        await self._persistence.review_unit(lease=lease, authentication_submitted=True)
        return self._result(route)

    async def _prove_checkpoint(
        self, lease: LeasedWorkdayApplication
    ) -> WorkdayUnit1CheckpointFacts | None:
        signatures: list[tuple[str, WorkdayUnit1CheckpointFacts]] = []
        latest: WorkdayUnit1CheckpointFacts | None = None
        deadline = self._clock() + _CHECKPOINT_DEADLINE_SECONDS
        for index in range(2):
            try:
                observed = await self._observer.observe()
                latest = await self._private_context.checkpoint_facts(
                    lease=lease, observation=observed
                )
            except Exception as exc:
                logger.info(
                    "workday_unit1_checkpoint_observation_failed "
                    "observation=%s error_type=%s",
                    index + 1,
                    type(exc).__name__,
                )
                return None
            transition_ready = (
                observed.state is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            )
            logger.info(
                "workday_unit1_checkpoint_observed "
                "observation=%s transition_state=%s transition_ready=%s "
                "approved_https_origin=%s canonical_tenant_verified=%s "
                "leased_job_context_matches=%s "
                "leased_application_context_matches=%s "
                "external_account_matches=%s no_login_or_auth_error=%s "
                "no_captcha_or_otp_or_lock=%s "
                "basic_information_control_hydrated=%s "
                "checkpoint_facts_satisfied=%s",
                index + 1,
                observed.state.value,
                transition_ready,
                latest.approved_https_origin,
                latest.canonical_tenant_verified,
                latest.leased_job_context_matches,
                latest.leased_application_context_matches,
                latest.external_account_matches,
                latest.no_login_or_auth_error,
                latest.no_captcha_or_otp_or_lock,
                latest.basic_information_control_hydrated,
                latest.satisfied,
            )
            if not transition_ready or not latest.satisfied:
                if not transition_ready and not latest.satisfied:
                    reason = "transition_state_and_checkpoint_facts"
                elif not transition_ready:
                    reason = "transition_state_not_ready"
                else:
                    reason = "checkpoint_facts_unsatisfied"
                logger.info(
                    "workday_unit1_checkpoint_rejected observation=%s reason=%s",
                    index + 1,
                    reason,
                )
                return None
            signatures.append((observed.safe_signature, latest))
            if index == 0:
                if self._clock() >= deadline:
                    logger.info(
                        "workday_unit1_checkpoint_rejected "
                        "observation=1 reason=deadline_exceeded"
                    )
                    return None
                await self._wait_for_hydration()
        stable = signatures[0] == signatures[1]
        logger.info(
            "workday_unit1_checkpoint_stability_evaluated " "observations=2 stable=%s",
            stable,
        )
        if not stable:
            return None
        logger.info("workday_unit1_checkpoint_verified observations=2")
        return latest

    async def _apply_failure(
        self,
        lease: LeasedWorkdayApplication,
        failure_class: WorkdayFailureClass,
        *,
        review_gate_mutation_applied: bool = False,
    ) -> WorkdayUnit1Result:
        route = self._route(failure_class)
        logger.info(
            "workday_unit1_failure_routed failure_class=%s outcome=%s gate_operation=%s",
            route.failure_class.value,
            route.outcome.value,
            route.gate_operation.value,
        )
        if review_gate_mutation_applied:
            await self._persistence.apply_route(
                lease=lease,
                route=route,
                review_gate_mutation_applied=True,
            )
        else:
            await self._persistence.apply_route(lease=lease, route=route)
        return self._result(route)

    @staticmethod
    def _dispatch_state(
        state: WorkdayTransitionState,
    ) -> tuple[str, tuple[str, PortalControlIntent] | None]:
        if state in _TRANSITIONS:
            return "transition", _TRANSITIONS[state]
        if state is WorkdayTransitionState.LOGIN_FORM:
            return "hydrate", None
        if state in {
            WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        }:
            return "terminal", None
        if state is WorkdayTransitionState.AUTH_OUTCOME_PENDING:
            return "safe_hold", None
        return "safe_hold", None

    async def _hydrate_login_form(self) -> WorkdayObservedState | None:
        """Poll only fresh observations until the login form is structurally ready."""
        for attempt in range(_LOGIN_FORM_HYDRATION_ATTEMPTS):
            condition = await self._capture_condition(None)
            failure_class = self._condition_failure(condition)
            if failure_class is not None:
                raise WorkdayPageConditionError(condition)
            observed = await self._observer.observe()
            logger.info(
                "workday_unit1_state_ready phase=login_form_hydration observation=%s state=%s",
                attempt + 1,
                observed.state.value,
            )
            if observed.state in {
                WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
                WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            }:
                return observed
            if observed.state is not WorkdayTransitionState.LOGIN_FORM:
                return None
            if attempt + 1 < _LOGIN_FORM_HYDRATION_ATTEMPTS:
                await self._wait_for_hydration()
        return None

    async def _wait_for_hydration(self) -> None:
        waiter = getattr(self._page, "wait_for_hydration", None)
        if callable(waiter):
            await waiter(_LOGIN_FORM_HYDRATION_INTERVAL_MS)
            return
        await asyncio.sleep(0)

    async def _apply_session_review(
        self, lease: LeasedWorkdayApplication
    ) -> WorkdayUnit1Result:
        route = WorkdayFailureRoute(
            failure_class=WorkdayFailureClass.ANYTHING_ELSE,
            outcome=WorkdayFailureOutcome.REVIEW_REQUIRED,
            gate_operation=WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        )
        await self._persistence.apply_route(lease=lease, route=route)
        return self._result(route)

    @staticmethod
    def _checkpoint_failure_route(
        *, authentication_submitted: bool
    ) -> WorkdayFailureRoute:
        if not authentication_submitted:
            return WorkdayFailureRoute(
                failure_class=WorkdayFailureClass.ANYTHING_ELSE,
                outcome=WorkdayFailureOutcome.REVIEW_REQUIRED,
                gate_operation=WorkdayGateOperation.MARK_REVIEW_REQUIRED,
            )
        return route_workday_failure(
            WorkdayFailureFacts(
                frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN}),
                auth_submit_count=1,
            )
        )

    async def _capture_condition(
        self, fallback: WorkdayUnit1PageCondition | None
    ) -> WorkdayUnit1PageCondition | None:
        capture = getattr(self._page, "capture_page_condition", None)
        if not callable(capture):
            return fallback
        # Keep older protocol fakes usable; the production Playwright page has
        # a locator method, while these fakes intentionally expose no DOM.
        page_impl = getattr(self._page, "_page", None)
        if page_impl is not None and not hasattr(page_impl, "locator"):
            return fallback
        try:
            return await capture()
        except WorkdayStateSecurityError:
            return WorkdayUnit1PageCondition(
                native_state=NativeAccountPageState.UNKNOWN,
                pre_submit_failure=WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
            )

    @staticmethod
    def _condition_failure(
        condition: WorkdayUnit1PageCondition | None,
    ) -> WorkdayFailureClass | None:
        return None if condition is None else condition.pre_submit_failure

    @staticmethod
    def _route(failure_class: WorkdayFailureClass) -> WorkdayFailureRoute:
        return route_workday_failure(WorkdayFailureFacts(frozenset({failure_class})))

    @staticmethod
    def _result(route: WorkdayFailureRoute) -> WorkdayUnit1Result:
        statuses = {
            WorkdayFailureOutcome.DEFER: WorkdayUnit1Status.DEFERRED,
            WorkdayFailureOutcome.SKIP_APPLICATION: WorkdayUnit1Status.SKIPPED,
            WorkdayFailureOutcome.BOUNDED_BACKOFF: WorkdayUnit1Status.BACKOFF,
            WorkdayFailureOutcome.REVIEW_REQUIRED: WorkdayUnit1Status.REVIEW_REQUIRED,
            WorkdayFailureOutcome.SECURITY_HOLD: WorkdayUnit1Status.HELD,
            WorkdayFailureOutcome.USER_HOLD: WorkdayUnit1Status.HELD,
            WorkdayFailureOutcome.CREDENTIAL_HOLD: WorkdayUnit1Status.HELD,
            WorkdayFailureOutcome.SAFE_HOLD: WorkdayUnit1Status.HELD,
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN: WorkdayUnit1Status.HELD,
        }
        return WorkdayUnit1Result(statuses[route.outcome], route=route)

    async def _emit_transition(
        self,
        *,
        key: WorkdayTransitionKey,
        version_id: str | None,
        from_state: WorkdayTransitionState,
        to_state: WorkdayTransitionState | None,
        decision: str,
        started: float,
    ) -> None:
        await self._events.emit(
            {
                "event": "workday_unit1_transition",
                "transition_id": key.task_type,
                "transition_version": version_id
                or (
                    "learned"
                    if decision
                    == WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED.value
                    else "not_learned"
                ),
                "from_state": from_state.value,
                "to_state": to_state.value if to_state is not None else "unknown",
                "decision": decision,
                "duration_ms": max(0, round((self._clock() - started) * 1000)),
            }
        )
