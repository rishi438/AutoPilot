"""Pure, deterministic routing for bounded Workday Unit 1 failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from services.workday_transition_contracts import WorkdayFailureClass


class WorkdayFailureOutcome(str, Enum):
    """Deterministic Unit 1 outcome selected by first-match precedence."""

    DEFER = "defer"
    SECURITY_HOLD = "security_hold"
    COMPLETE_UNIT = "complete_unit"
    START_OR_REFRESH_COOLDOWN = "start_or_refresh_cooldown"
    USER_HOLD = "user_hold"
    CREDENTIAL_HOLD = "credential_hold"
    REVIEW_REQUIRED = "review_required"
    SKIP_APPLICATION = "skip_application"
    BOUNDED_BACKOFF = "bounded_backoff"
    REPAIR_ONE_TRANSITION = "repair_one_transition"
    SAFE_HOLD = "safe_hold"


class WorkdayGateOperation(str, Enum):
    """Task 05 gate operation required to apply a routed outcome."""

    KEEP_CURRENT = "keep_current"
    COMPLETE_SUCCESS = "complete_success"
    CONFIRM_ACCOUNT_LOCK = "confirm_account_lock"
    MARK_REVIEW_REQUIRED = "mark_review_required"
    START_BOUNDED_BACKOFF = "start_bounded_backoff"
    RELEASE_UNSUBMITTED = "release_unsubmitted"
    CLAIM_LLM_REPAIR = "claim_llm_repair"


@dataclass(frozen=True, slots=True)
class WorkdayFailureFacts:
    """Typed, private-data-free facts supplied to the pure router."""

    observed_classes: frozenset[WorkdayFailureClass]
    auth_submit_count: int = 0
    timeout_after_submit: bool = False
    network_ambiguity_after_submit: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_classes", frozenset(self.observed_classes))
        if self.auth_submit_count not in {0, 1}:
            raise ValueError("Authentication submit count must be zero or one.")
        if (
            self.timeout_after_submit or self.network_ambiguity_after_submit
        ) and self.auth_submit_count != 1:
            raise ValueError("Post-submit ambiguity requires one claimed submission.")
        if any(
            not isinstance(item, WorkdayFailureClass) for item in self.observed_classes
        ):
            raise TypeError("Observed classes must be WorkdayFailureClass values.")


@dataclass(frozen=True, slots=True)
class WorkdayFailureRoute:
    """One safe outcome plus its required private gate mutation."""

    failure_class: WorkdayFailureClass
    outcome: WorkdayFailureOutcome
    gate_operation: WorkdayGateOperation
    llm_repair_allowed: bool = False
    catalog_history_mutation_requested: bool = False
    signup_requested: bool = False
    authentication_resubmit_requested: bool = False


_POST_SUBMIT_CLASSES = frozenset(
    {
        WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
        WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
        WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
        WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
        WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN,
    }
)

_ROUTES = (
    (
        WorkdayFailureClass.COOLDOWN_ACTIVE,
        WorkdayFailureOutcome.DEFER,
        WorkdayGateOperation.KEEP_CURRENT,
    ),
    (
        WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
        WorkdayFailureOutcome.SECURITY_HOLD,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
    (
        WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
        WorkdayFailureOutcome.COMPLETE_UNIT,
        WorkdayGateOperation.COMPLETE_SUCCESS,
    ),
    (
        WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
        WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN,
        WorkdayGateOperation.CONFIRM_ACCOUNT_LOCK,
    ),
    (
        WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
        WorkdayFailureOutcome.USER_HOLD,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
    (
        WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
        WorkdayFailureOutcome.CREDENTIAL_HOLD,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
    (
        WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN,
        WorkdayFailureOutcome.REVIEW_REQUIRED,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
    (
        WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
        WorkdayFailureOutcome.USER_HOLD,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
    (
        WorkdayFailureClass.JOB_UNAVAILABLE,
        WorkdayFailureOutcome.SKIP_APPLICATION,
        WorkdayGateOperation.RELEASE_UNSUBMITTED,
    ),
    (
        WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
        WorkdayFailureOutcome.BOUNDED_BACKOFF,
        WorkdayGateOperation.START_BOUNDED_BACKOFF,
    ),
    (
        WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH,
        WorkdayFailureOutcome.REPAIR_ONE_TRANSITION,
        WorkdayGateOperation.CLAIM_LLM_REPAIR,
    ),
    (
        WorkdayFailureClass.ANYTHING_ELSE,
        WorkdayFailureOutcome.SAFE_HOLD,
        WorkdayGateOperation.MARK_REVIEW_REQUIRED,
    ),
)


def route_workday_failure(facts: WorkdayFailureFacts) -> WorkdayFailureRoute:
    """Select the first eligible route without invoking any external service."""
    eligible = set(facts.observed_classes)
    if facts.auth_submit_count == 1:
        eligible.intersection_update(_POST_SUBMIT_CLASSES)
        if facts.timeout_after_submit or facts.network_ambiguity_after_submit:
            eligible = {WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN}
        if not eligible:
            eligible.add(WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN)
    elif not eligible:
        eligible.add(WorkdayFailureClass.ANYTHING_ELSE)

    for failure_class, outcome, gate_operation in _ROUTES:
        if failure_class in eligible:
            return WorkdayFailureRoute(
                failure_class=failure_class,
                outcome=outcome,
                gate_operation=gate_operation,
                llm_repair_allowed=(
                    outcome is WorkdayFailureOutcome.REPAIR_ONE_TRANSITION
                ),
            )
    raise AssertionError("The failure routing table must remain exhaustive.")
