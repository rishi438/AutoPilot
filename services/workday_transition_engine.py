"""Deterministic replay of verified, value-free Workday transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol
from uuid import UUID

from services.portal_control_resolver import PortalControlIntent
from services.workday_transition_catalog import (
    UnsafeWorkdayLocatorStrategy,
    WorkdayTransitionCatalog,
    validate_safe_locator_strategy,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)
from services.workday_page_condition import WorkdayPageConditionError
from services.workday_state_observer import WorkdayStateSecurityError

_CANDIDATE_ID: Final = re.compile(r"^wdc-[0-9a-f]{16}$")
_EXECUTABLE_INTENTS: Final = frozenset(
    {
        PortalControlIntent.APPLY,
        PortalControlIntent.APPLY_MANUALLY,
        PortalControlIntent.OPEN_REGISTRATION,
        PortalControlIntent.SIGN_IN,
    }
)
_LOCATOR_INTENTS: Final = {
    PortalControlIntent.APPLY: "apply",
    PortalControlIntent.APPLY_MANUALLY: "apply_manually",
    PortalControlIntent.OPEN_REGISTRATION: "open_registration",
    PortalControlIntent.SIGN_IN: "sign_in",
}
_ALLOWED_NEXT_STATES: Final = {
    PortalControlIntent.APPLY: frozenset(
        {
            WorkdayTransitionState.APPLY_CHOICES,
            WorkdayTransitionState.ACCOUNT_PAGE,
        }
    ),
    PortalControlIntent.APPLY_MANUALLY: frozenset(
        {WorkdayTransitionState.ACCOUNT_PAGE}
    ),
    PortalControlIntent.OPEN_REGISTRATION: frozenset(
        {
            WorkdayTransitionState.LOGIN_FORM,
            WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        }
    ),
    PortalControlIntent.SIGN_IN: frozenset(
        {
            WorkdayTransitionState.LOGIN_FORM,
            WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        }
    ),
}
_BOOTSTRAP_TRANSITIONS: Final = {
    PortalControlIntent.APPLY: (
        WorkdayTransitionState.APPLY_CHOICES,
        WorkdayTransitionRisk.NAVIGATION_ONLY,
    ),
    PortalControlIntent.APPLY_MANUALLY: (
        WorkdayTransitionState.ACCOUNT_PAGE,
        WorkdayTransitionRisk.NAVIGATION_ONLY,
    ),
    PortalControlIntent.OPEN_REGISTRATION: (
        WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        WorkdayTransitionRisk.AUTH_STRUCTURE,
    ),
    PortalControlIntent.SIGN_IN: (
        WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        WorkdayTransitionRisk.AUTH_STRUCTURE,
    ),
}


class TransitionReplayError(RuntimeError):
    """Base error for a replay rejected before an action can occur."""


class TransitionPreconditionFailed(TransitionReplayError):
    """The fresh page observation does not match the requested transition."""


class TransitionRepairRequired(TransitionReplayError):
    """No stored version structurally matches before the action boundary."""

    def __init__(
        self, message: str, *, ticket: TransitionRepairTicket | None = None
    ) -> None:
        super().__init__(message)
        self.ticket = ticket


class TransitionReplayStatus(str, Enum):
    """Typed deterministic routing after one replay attempt."""

    CURRENT_SUCCEEDED = "current_succeeded"
    PREVIOUS_SUCCEEDED_CURRENT_UPDATED = "previous_succeeded_current_updated"
    PREVIOUS_SUCCEEDED_CURRENT_CONFLICT = "previous_succeeded_current_conflict"
    DIRECT_AUTHENTICATED_SESSION = "direct_authenticated_session"
    ACTION_OUTCOME_UNVERIFIED = "action_outcome_unverified"
    UNEXPECTED_NEXT_STATE = "unexpected_next_state"


@dataclass(frozen=True, slots=True)
class TransitionReplayOutcome:
    """Safe result containing no page content or private account data."""

    status: TransitionReplayStatus
    version_id: UUID
    observed_state: WorkdayTransitionState | None
    action_may_have_happened: bool = True
    failure_class: WorkdayFailureClass | None = None


@dataclass(frozen=True, slots=True)
class TransitionRepairTicket:
    """Value-free proof that stored versions mismatched before any action."""

    key: WorkdayTransitionKey
    expected_from_state: WorkdayTransitionState
    family_id: UUID
    expected_to_state: WorkdayTransitionState
    risk: WorkdayTransitionRisk
    stored_version_count: int
    all_versions_mismatched: bool = True
    action_may_have_happened: bool = False


class WorkdayReplayObserver(Protocol):
    """Fresh safe-state observation surface used before and after an action."""

    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState: ...


class WorkdayReplayBrowserActions(Protocol):
    """Minimal surface for one bounded, allowlisted candidate action."""

    async def execute_candidate(
        self, *, candidate_id: str, action_intent: PortalControlIntent
    ) -> None: ...


class WorkdayTransitionReplayEngine:
    """Replay CURRENT then bounded VERIFIED history without model involvement."""

    def __init__(
        self,
        *,
        observer: WorkdayReplayObserver,
        browser_actions: WorkdayReplayBrowserActions,
        catalog: WorkdayTransitionCatalog,
        history_limit: int,
    ) -> None:
        if type(history_limit) is not int or not 1 <= history_limit <= 100:
            raise ValueError("History limit must be between 1 and 100.")
        self._observer = observer
        self._browser_actions = browser_actions
        self._catalog = catalog
        self._history_limit = history_limit

    async def replay(
        self,
        *,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
        observed: WorkdayObservedState | None = None,
    ) -> TransitionReplayOutcome:
        """Execute at most one compatible action and verify its declared next state."""
        if observed is None:
            observed = await self._observer.observe(include_candidates=True)
        self._verify_precondition(
            observed=observed,
            key=key,
            expected_from_state=expected_from_state,
        )

        recipes = tuple(
            await self._catalog.get_current_and_history(key, self._history_limit)
        )
        if not recipes:
            bootstrap = _BOOTSTRAP_TRANSITIONS.get(key.action_intent)
            if bootstrap is None:
                raise TransitionRepairRequired(
                    "No verified transition version is stored."
                )
            family_id = await self._catalog.ensure_family(key)
            expected_to_state, risk = bootstrap
            raise TransitionRepairRequired(
                "The first transition version requires bounded repair.",
                ticket=TransitionRepairTicket(
                    key=key,
                    expected_from_state=expected_from_state,
                    family_id=family_id,
                    expected_to_state=expected_to_state,
                    risk=risk,
                    stored_version_count=0,
                ),
            )

        expected_current_id = recipes[0].version_id
        for index, recipe in enumerate(recipes):
            candidate_id = self._compatible_candidate(
                recipe=recipe,
                key=key,
                expected_from_state=expected_from_state,
                candidates=observed.candidate_metadata,
            )
            if candidate_id is None:
                continue

            try:
                await self._browser_actions.execute_candidate(
                    candidate_id=candidate_id,
                    action_intent=key.action_intent,
                )
            except Exception:
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.ACTION_OUTCOME_UNVERIFIED,
                    version_id=recipe.version_id,
                    observed_state=None,
                )

            try:
                next_observed = await self._observer.observe()
            except WorkdayPageConditionError as exc:
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.ACTION_OUTCOME_UNVERIFIED,
                    version_id=recipe.version_id,
                    observed_state=None,
                    failure_class=exc.failure_class,
                )
            except WorkdayStateSecurityError:
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.ACTION_OUTCOME_UNVERIFIED,
                    version_id=recipe.version_id,
                    observed_state=None,
                    failure_class=WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
                )
            except Exception:
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.ACTION_OUTCOME_UNVERIFIED,
                    version_id=recipe.version_id,
                    observed_state=None,
                )
            if not self._is_expected_next(next_observed, key, recipe):
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.UNEXPECTED_NEXT_STATE,
                    version_id=recipe.version_id,
                    observed_state=next_observed.state,
                )
            if (
                next_observed.state
                is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            ):
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.DIRECT_AUTHENTICATED_SESSION,
                    version_id=recipe.version_id,
                    observed_state=next_observed.state,
                )
            if index == 0:
                return TransitionReplayOutcome(
                    status=TransitionReplayStatus.CURRENT_SUCCEEDED,
                    version_id=recipe.version_id,
                    observed_state=next_observed.state,
                )

            updated = await self._catalog.rollback_current(
                family_id=recipe.family_id,
                version_id=recipe.version_id,
                expected_current_id=expected_current_id,
            )
            return TransitionReplayOutcome(
                status=(
                    TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_UPDATED
                    if updated
                    else TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_CONFLICT
                ),
                version_id=recipe.version_id,
                observed_state=next_observed.state,
            )

        current = recipes[0]
        raise TransitionRepairRequired(
            "All stored transition versions mismatched before action.",
            ticket=TransitionRepairTicket(
                key=key,
                expected_from_state=expected_from_state,
                family_id=current.family_id,
                expected_to_state=current.expected_to_state,
                risk=current.risk,
                stored_version_count=len(recipes),
            ),
        )

    async def replay_observed(
        self,
        *,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
        observed: WorkdayObservedState,
    ) -> TransitionReplayOutcome:
        """Replay from the exact candidate-bearing dispatch observation."""
        return await self.replay(
            key=key,
            expected_from_state=expected_from_state,
            observed=observed,
        )

    @staticmethod
    def _verify_precondition(
        *,
        observed: WorkdayObservedState,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
    ) -> None:
        if (
            observed.portal_family != key.portal_family
            or observed.tenant_scope != key.tenant_scope
            or observed.signature_version != key.signature_version
            or observed.safe_signature != key.from_state_signature
            or observed.state is not expected_from_state
        ):
            raise TransitionPreconditionFailed(
                "The fresh Workday state does not match the requested transition."
            )

    @staticmethod
    def _compatible_candidate(
        *,
        recipe: WorkdayTransitionRecipe,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
        candidates: tuple[WorkdaySafeCandidateMetadata, ...],
    ) -> str | None:
        if (
            key.action_intent not in _EXECUTABLE_INTENTS
            or recipe.status is not WorkdayTransitionStatus.VERIFIED
            or recipe.signature_version != key.signature_version
            or recipe.executor_policy_version != key.executor_policy_version
            or recipe.risk
            not in {
                WorkdayTransitionRisk.NAVIGATION_ONLY,
                WorkdayTransitionRisk.AUTH_STRUCTURE,
            }
            or recipe.expected_to_state not in _ALLOWED_NEXT_STATES[key.action_intent]
            or not _valid_from_state(expected_from_state, key.action_intent)
        ):
            return None
        try:
            locator = validate_safe_locator_strategy(recipe.safe_locator_strategy)
        except UnsafeWorkdayLocatorStrategy:
            return None
        if locator["intent_key"] != _LOCATOR_INTENTS[key.action_intent]:
            return None

        if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
            return None
        matches = [
            candidate
            for candidate in candidates
            if _CANDIDATE_ID.fullmatch(candidate.candidate_id)
            and candidate.semantic_role == locator["semantic_role"]
            and candidate.intent_key == key.action_intent.value
            and candidate.scope_key == locator["scope_key"]
        ]
        return matches[0].candidate_id if len(matches) == 1 else None

    @staticmethod
    def compatible_candidate(
        *,
        recipe: WorkdayTransitionRecipe,
        key: WorkdayTransitionKey,
        expected_from_state: WorkdayTransitionState,
        candidates: tuple[WorkdaySafeCandidateMetadata, ...],
    ) -> str | None:
        """Expose the replay compatibility rule to the bounded repair gate."""
        return WorkdayTransitionReplayEngine._compatible_candidate(
            recipe=recipe,
            key=key,
            expected_from_state=expected_from_state,
            candidates=candidates,
        )

    @staticmethod
    def _is_expected_next(
        observed: WorkdayObservedState,
        key: WorkdayTransitionKey,
        recipe: WorkdayTransitionRecipe,
    ) -> bool:
        expected_state = recipe.expected_to_state
        direct_session = (
            observed.state is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
            and key.action_intent in _EXECUTABLE_INTENTS
        )
        valid_state = observed.state is expected_state or direct_session
        return (
            observed.portal_family == key.portal_family
            and observed.tenant_scope == key.tenant_scope
            and observed.signature_version == key.signature_version
            and valid_state
        )


def _valid_from_state(
    state: WorkdayTransitionState, intent: PortalControlIntent
) -> bool:
    return (
        (
            intent is PortalControlIntent.APPLY
            and state is WorkdayTransitionState.JOB_PAGE
        )
        or (
            intent is PortalControlIntent.APPLY_MANUALLY
            and state is WorkdayTransitionState.APPLY_CHOICES
        )
        or (
            intent is PortalControlIntent.OPEN_REGISTRATION
            and state is WorkdayTransitionState.ACCOUNT_PAGE
        )
        or (
            intent is PortalControlIntent.SIGN_IN
            and state is WorkdayTransitionState.ACCOUNT_PAGE
        )
    )
