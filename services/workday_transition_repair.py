"""One-shot, pre-auth Workday transition repair and shared recipe learning."""

from __future__ import annotations

import asyncio
import re
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from services.portal_control_resolver import (
    PortalControlIntent,
    PortalControlSelection,
    PortalRepairCandidate,
)
from services.workday_account_gate_store import (
    WorkdayGateLease,
    WorkdayGateMutation,
)
from services.workday_failure_router import (
    WorkdayFailureOutcome,
    WorkdayFailureRoute,
    WorkdayGateOperation,
)
from services.workday_transition_catalog import (
    WorkdayTransitionCatalog,
    validate_safe_locator_strategy,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionRecipe,
    WorkdayTransitionState,
)
from services.workday_page_condition import WorkdayPageConditionError
from services.workday_state_observer import WorkdayStateSecurityError
from services.workday_transition_engine import (
    TransitionRepairTicket,
    WorkdayReplayBrowserActions,
    WorkdayReplayObserver,
    WorkdayTransitionReplayEngine,
)

_CANDIDATE_ID: Final = re.compile(r"^wdc-[0-9a-f]{16}$")
_MAX_CANDIDATES: Final = 40
_MIN_CONFIDENCE: Final = 0.5
_LOGIN_FORM_HYDRATION_ATTEMPTS: Final = 4
_LOGIN_FORM_HYDRATION_INTERVAL_MS: Final = 250
_SAFE_ROLES: Final = frozenset({"button", "link"})
_SAFE_SCOPES: Final = frozenset({"page", "active_dialog", "active_account_form"})
_LOCATOR_INTENTS: Final = {
    PortalControlIntent.APPLY: "apply",
    PortalControlIntent.APPLY_MANUALLY: "apply_manually",
    PortalControlIntent.OPEN_REGISTRATION: "open_registration",
    PortalControlIntent.SIGN_IN: "sign_in",
}
_ALLOWED_TRANSITIONS: Final = {
    PortalControlIntent.APPLY: (
        WorkdayTransitionState.JOB_PAGE,
        frozenset(
            {
                WorkdayTransitionState.APPLY_CHOICES,
                WorkdayTransitionState.ACCOUNT_PAGE,
            }
        ),
    ),
    PortalControlIntent.APPLY_MANUALLY: (
        WorkdayTransitionState.APPLY_CHOICES,
        frozenset({WorkdayTransitionState.ACCOUNT_PAGE}),
    ),
    PortalControlIntent.OPEN_REGISTRATION: (
        WorkdayTransitionState.ACCOUNT_PAGE,
        frozenset(
            {
                WorkdayTransitionState.LOGIN_FORM,
                WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
                WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            }
        ),
    ),
    PortalControlIntent.SIGN_IN: (
        WorkdayTransitionState.ACCOUNT_PAGE,
        frozenset(
            {
                WorkdayTransitionState.LOGIN_FORM,
                WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
                WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            }
        ),
    ),
}
logger = logging.getLogger(__name__)


class WorkdayRepairResolver(Protocol):
    """Configured local-model surface restricted to bounded repair fields."""

    async def select_repair(
        self,
        *,
        intent: PortalControlIntent,
        expected_next_state: str,
        candidates: list[PortalRepairCandidate],
    ) -> PortalControlSelection | None: ...


class WorkdayRepairGateStore(Protocol):
    """Private attempt mutations needed by the repair coordinator."""

    async def claim_llm_repair(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...

    async def mark_review_required(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation: ...


@dataclass(frozen=True, slots=True)
class WorkdayRepairAttemptFacts:
    """Pre-auth facts checked again by the atomic private-gate claim."""

    approved_https_origin: bool
    canonical_tenant_verified: bool
    stable_from_state: bool
    secret_accessed: bool = False
    auth_submit_count: int = 0
    irreversible_action_taken: bool = False
    llm_repair_count: int = 0


@dataclass(frozen=True, slots=True)
class WorkdayTransitionRepairRequest:
    """Safe repair authorization assembled from deterministic components."""

    route: WorkdayFailureRoute
    ticket: TransitionRepairTicket
    gate_lease: WorkdayGateLease
    facts: WorkdayRepairAttemptFacts


class WorkdayTransitionRepairStatus(str, Enum):
    """Outcome of one bounded repair-coordinator invocation."""

    INELIGIBLE = "ineligible"
    CLAIM_REJECTED = "claim_rejected"
    SAFE_HOLD = "safe_hold"
    DIRECT_AUTHENTICATED_SESSION = "direct_authenticated_session"
    VERIFIED_AND_LEARNED = "verified_and_learned"


@dataclass(frozen=True, slots=True)
class WorkdayTransitionRepairOutcome:
    """Value-free repair result."""

    status: WorkdayTransitionRepairStatus
    observed_state: WorkdayTransitionState | None = None
    hold_applied: bool = False
    failure_class: WorkdayFailureClass | None = None


class WorkdayTransitionRepairCoordinator:
    """Claim, resolve, execute, verify, and learn exactly one safe transition."""

    def __init__(
        self,
        *,
        observer: WorkdayReplayObserver,
        browser_actions: WorkdayReplayBrowserActions,
        catalog: WorkdayTransitionCatalog,
        gate_store: WorkdayRepairGateStore,
        resolver: WorkdayRepairResolver,
        history_limit: int,
        min_confidence: float = _MIN_CONFIDENCE,
    ) -> None:
        if type(history_limit) is not int or not 1 <= history_limit <= 100:
            raise ValueError("History limit must be between 1 and 100.")
        if not 0 < min_confidence <= 1:
            raise ValueError("Minimum confidence must be between 0 and 1.")
        self._observer = observer
        self._browser_actions = browser_actions
        self._catalog = catalog
        self._gate_store = gate_store
        self._resolver = resolver
        self._history_limit = history_limit
        self._min_confidence = min_confidence

    async def repair(
        self,
        request: WorkdayTransitionRepairRequest,
        *,
        observed: WorkdayObservedState | None = None,
    ) -> WorkdayTransitionRepairOutcome:
        """Run at most one local-model call and one M4 navigation action."""
        if not self._eligible(request):
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.INELIGIBLE
            )

        try:
            if observed is None:
                observed = await self._observer.observe(include_candidates=True)
            recipes = tuple(
                await self._catalog.get_current_and_history(
                    request.ticket.key, self._history_limit
                )
            )
        except WorkdayPageConditionError as exc:
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.SAFE_HOLD,
                failure_class=exc.failure_class,
            )
        except WorkdayStateSecurityError:
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.SAFE_HOLD,
                failure_class=WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
            )
        except Exception:
            logger.warning(
                "workday_transition_repair_stage_failed stage=observe_or_catalog"
            )
            return await self._safe_hold(request.gate_lease)
        if not self._fresh_mismatch_is_valid(request.ticket, observed, recipes):
            logger.info(
                "workday_transition_repair_stage_rejected stage=fresh_mismatch stored_versions=%s recipes=%s",
                request.ticket.stored_version_count,
                len(recipes),
            )
            return await self._safe_hold(request.gate_lease)

        safe_candidates = self._safe_candidates(
            observed.candidate_metadata,
            intent=request.ticket.key.action_intent,
        )
        if not safe_candidates:
            intent_counts = {
                intent.value: sum(
                    candidate.intent_key == intent.value
                    for candidate in observed.candidate_metadata
                )
                for intent in PortalControlIntent
            }
            scope_counts = {
                scope: sum(
                    candidate.scope_key == scope
                    for candidate in observed.candidate_metadata
                )
                for scope in sorted(_SAFE_SCOPES)
            }
            logger.info(
                "workday_transition_repair_stage_rejected stage=safe_candidates "
                "candidate_count=%s expected_intent=%s intent_counts=%s "
                "scope_counts=%s",
                len(observed.candidate_metadata),
                request.ticket.key.action_intent.value,
                intent_counts,
                scope_counts,
            )
            return await self._safe_hold(request.gate_lease)

        claim = await self._gate_store.claim_llm_repair(request.gate_lease)
        if not claim.applied:
            logger.info(
                "workday_transition_repair_stage_rejected stage=claim_llm_repair"
            )
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.CLAIM_REJECTED
            )

        try:
            selection = await self._resolver.select_repair(
                intent=request.ticket.key.action_intent,
                expected_next_state=request.ticket.expected_to_state.value,
                candidates=[
                    PortalRepairCandidate(
                        candidate_id=candidate.candidate_id,
                        semantic_role=candidate.semantic_role,
                        semantic_name=candidate.intent_key,
                        scope_key=candidate.scope_key,
                    )
                    for candidate in safe_candidates
                ],
            )
        except Exception:
            logger.warning("workday_transition_repair_stage_failed stage=resolver")
            return await self._safe_hold(request.gate_lease)

        selected = self._validated_selection(selection, safe_candidates)
        if selected is None:
            logger.info(
                "workday_transition_repair_stage_rejected stage=selection_validation"
            )
            return await self._safe_hold(request.gate_lease)
        locator = validate_safe_locator_strategy(
            {
                "schema_version": 1,
                "semantic_role": selected.semantic_role,
                "intent_key": _LOCATOR_INTENTS[request.ticket.key.action_intent],
                "scope_key": selected.scope_key,
                "require_unique": True,
            }
        )

        try:
            await self._browser_actions.execute_candidate(
                candidate_id=selected.candidate_id,
                action_intent=request.ticket.key.action_intent,
            )
            next_observed = await self._observer.observe()
            next_observed = await self._hydrate_auth_destination(
                request.ticket, next_observed
            )
        except WorkdayPageConditionError as exc:
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.SAFE_HOLD,
                failure_class=exc.failure_class,
            )
        except WorkdayStateSecurityError:
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.SAFE_HOLD,
                failure_class=WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
            )
        except Exception:
            logger.warning(
                "workday_transition_repair_stage_failed stage=execute_or_observe"
            )
            return await self._safe_hold(request.gate_lease)
        if not self._expected_next(request.ticket, next_observed):
            logger.info(
                "workday_transition_repair_stage_rejected stage=expected_next observed_state=%s",
                next_observed.state.value,
            )
            return await self._safe_hold(
                request.gate_lease, observed_state=next_observed.state
            )
        if (
            next_observed.state
            is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
        ):
            return WorkdayTransitionRepairOutcome(
                WorkdayTransitionRepairStatus.DIRECT_AUTHENTICATED_SESSION,
                observed_state=next_observed.state,
            )

        try:
            await self._catalog.append_verified_and_set_current(
                family_id=request.ticket.family_id,
                safe_locator_strategy=locator,
                expected_to_state=request.ticket.expected_to_state,
                risk=request.ticket.risk,
                signature_version=request.ticket.key.signature_version,
                executor_policy_version=request.ticket.key.executor_policy_version,
            )
        except Exception:
            logger.warning("workday_transition_repair_stage_failed stage=append_recipe")
            return await self._safe_hold(
                request.gate_lease, observed_state=next_observed.state
            )
        return WorkdayTransitionRepairOutcome(
            WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED,
            observed_state=next_observed.state,
        )

    async def repair_observed(
        self,
        request: WorkdayTransitionRepairRequest,
        *,
        observed: WorkdayObservedState,
    ) -> WorkdayTransitionRepairOutcome:
        """Repair from the exact candidate-bearing dispatch observation."""
        return await self.repair(request, observed=observed)

    @staticmethod
    def _eligible(request: WorkdayTransitionRepairRequest) -> bool:
        route = request.route
        ticket = request.ticket
        facts = request.facts
        allowed_transition = _ALLOWED_TRANSITIONS.get(ticket.key.action_intent)
        return (
            route.outcome is WorkdayFailureOutcome.REPAIR_ONE_TRANSITION
            and route.gate_operation is WorkdayGateOperation.CLAIM_LLM_REPAIR
            and route.llm_repair_allowed
            and ticket.all_versions_mismatched
            and ticket.stored_version_count >= 0
            and not ticket.action_may_have_happened
            and allowed_transition is not None
            and ticket.expected_from_state is allowed_transition[0]
            and ticket.expected_to_state in allowed_transition[1]
            and facts.approved_https_origin
            and facts.canonical_tenant_verified
            and facts.stable_from_state
            and not facts.secret_accessed
            and facts.auth_submit_count == 0
            and not facts.irreversible_action_taken
            and facts.llm_repair_count == 0
        )

    @staticmethod
    def _fresh_mismatch_is_valid(
        ticket: TransitionRepairTicket,
        observed: WorkdayObservedState,
        recipes: tuple[WorkdayTransitionRecipe, ...],
    ) -> bool:
        key = ticket.key
        if (
            observed.portal_family != key.portal_family
            or observed.tenant_scope != key.tenant_scope
            or observed.signature_version != key.signature_version
            or observed.safe_signature != key.from_state_signature
            or observed.state is not ticket.expected_from_state
        ):
            return False
        if ticket.stored_version_count == 0:
            return not recipes
        if not recipes or recipes[0].family_id != ticket.family_id:
            return False
        return all(
            WorkdayTransitionReplayEngine.compatible_candidate(
                recipe=recipe,
                key=key,
                expected_from_state=ticket.expected_from_state,
                candidates=observed.candidate_metadata,
            )
            is None
            for recipe in recipes
        )

    @staticmethod
    def _safe_candidates(
        candidates: tuple[WorkdaySafeCandidateMetadata, ...],
        *,
        intent: PortalControlIntent,
    ) -> tuple[WorkdaySafeCandidateMetadata, ...]:
        if not candidates or len(candidates) > _MAX_CANDIDATES:
            return ()
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            return ()
        if any(
            not _CANDIDATE_ID.fullmatch(candidate.candidate_id)
            or candidate.semantic_role not in _SAFE_ROLES
            or candidate.scope_key not in _SAFE_SCOPES
            for candidate in candidates
        ):
            return ()
        matching = tuple(
            candidate
            for candidate in candidates
            if candidate.intent_key == intent.value
        )
        return matching if matching else ()

    def _validated_selection(
        self,
        selection: PortalControlSelection | None,
        candidates: tuple[WorkdaySafeCandidateMetadata, ...],
    ) -> WorkdaySafeCandidateMetadata | None:
        if (
            selection is None
            or not isinstance(selection.candidate_id, str)
            or isinstance(selection.confidence, bool)
            or not isinstance(selection.confidence, (int, float))
            or not self._min_confidence <= float(selection.confidence) <= 1
        ):
            return None
        matches = [
            candidate
            for candidate in candidates
            if candidate.candidate_id == selection.candidate_id
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _expected_next(
        ticket: TransitionRepairTicket, observed: WorkdayObservedState
    ) -> bool:
        key = ticket.key
        direct_session = (
            observed.state is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            and ticket.key.action_intent
            in {
                PortalControlIntent.APPLY,
                PortalControlIntent.APPLY_MANUALLY,
                PortalControlIntent.OPEN_REGISTRATION,
                PortalControlIntent.SIGN_IN,
            }
        )
        valid_state = (
            observed.state is ticket.expected_to_state
            or direct_session
            or (
                ticket.key.action_intent is PortalControlIntent.SIGN_IN
                and ticket.expected_to_state
                is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
                and observed.state
                is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            )
        )
        return (
            observed.portal_family == key.portal_family
            and observed.tenant_scope == key.tenant_scope
            and observed.signature_version == key.signature_version
            and valid_state
        )

    async def _hydrate_auth_destination(
        self,
        ticket: TransitionRepairTicket,
        observed: WorkdayObservedState,
    ) -> WorkdayObservedState:
        """Reobserve a partial login or registration form before learning it."""
        should_hydrate = (
            ticket.key.action_intent
            in {
                PortalControlIntent.OPEN_REGISTRATION,
                PortalControlIntent.SIGN_IN,
            }
            and ticket.expected_to_state
            is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
            and observed.state is WorkdayTransitionState.LOGIN_FORM
        )
        if not should_hydrate:
            return observed

        for _ in range(1, _LOGIN_FORM_HYDRATION_ATTEMPTS):
            waiter = getattr(self._browser_actions, "wait_for_hydration", None)
            if callable(waiter):
                await waiter(_LOGIN_FORM_HYDRATION_INTERVAL_MS)
            else:
                await asyncio.sleep(0)
            observed = await self._observer.observe()
            if observed.state is not WorkdayTransitionState.LOGIN_FORM:
                return observed
        return observed

    async def _safe_hold(
        self,
        lease: WorkdayGateLease,
        *,
        observed_state: WorkdayTransitionState | None = None,
    ) -> WorkdayTransitionRepairOutcome:
        mutation = await self._gate_store.mark_review_required(lease)
        return WorkdayTransitionRepairOutcome(
            WorkdayTransitionRepairStatus.SAFE_HOLD,
            observed_state=observed_state,
            hold_applied=mutation.applied,
        )
