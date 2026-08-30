from __future__ import annotations

import pytest

from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureOutcome,
    WorkdayGateOperation,
    route_workday_failure,
)
from services.workday_transition_contracts import WorkdayFailureClass


@pytest.mark.parametrize(
    ("failure_class", "submit_count", "outcome", "gate_operation"),
    [
        (
            WorkdayFailureClass.COOLDOWN_ACTIVE,
            0,
            WorkdayFailureOutcome.DEFER,
            WorkdayGateOperation.KEEP_CURRENT,
        ),
        (
            WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
            0,
            WorkdayFailureOutcome.SECURITY_HOLD,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
            1,
            WorkdayFailureOutcome.COMPLETE_UNIT,
            WorkdayGateOperation.COMPLETE_SUCCESS,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
            1,
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN,
            WorkdayGateOperation.CONFIRM_ACCOUNT_LOCK,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
            1,
            WorkdayFailureOutcome.USER_HOLD,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
            1,
            WorkdayFailureOutcome.CREDENTIAL_HOLD,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN,
            1,
            WorkdayFailureOutcome.REVIEW_REQUIRED,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
        (
            WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            0,
            WorkdayFailureOutcome.USER_HOLD,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
        (
            WorkdayFailureClass.JOB_UNAVAILABLE,
            0,
            WorkdayFailureOutcome.SKIP_APPLICATION,
            WorkdayGateOperation.RELEASE_UNSUBMITTED,
        ),
        (
            WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
            0,
            WorkdayFailureOutcome.BOUNDED_BACKOFF,
            WorkdayGateOperation.START_BOUNDED_BACKOFF,
        ),
        (
            WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH,
            0,
            WorkdayFailureOutcome.REPAIR_ONE_TRANSITION,
            WorkdayGateOperation.CLAIM_LLM_REPAIR,
        ),
        (
            WorkdayFailureClass.ANYTHING_ELSE,
            0,
            WorkdayFailureOutcome.SAFE_HOLD,
            WorkdayGateOperation.MARK_REVIEW_REQUIRED,
        ),
    ],
)
def test_every_first_match_row_has_exact_outcome_and_gate_operation(
    failure_class: WorkdayFailureClass,
    submit_count: int,
    outcome: WorkdayFailureOutcome,
    gate_operation: WorkdayGateOperation,
) -> None:
    route = route_workday_failure(
        WorkdayFailureFacts(frozenset({failure_class}), submit_count)
    )

    assert route.failure_class is failure_class
    assert route.outcome is outcome
    assert route.gate_operation is gate_operation


def test_first_match_wins_when_multiple_classes_are_observed() -> None:
    route = route_workday_failure(
        WorkdayFailureFacts(
            frozenset(
                {
                    WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
                    WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT,
                    WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH,
                }
            )
        )
    )

    assert route.outcome is WorkdayFailureOutcome.SECURITY_HOLD


def test_submitted_attempt_cannot_take_pre_submit_backoff_or_repair_route() -> None:
    route = route_workday_failure(
        WorkdayFailureFacts(
            frozenset(
                {
                    WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
                    WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH,
                }
            ),
            auth_submit_count=1,
        )
    )

    assert route.failure_class is WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN
    assert route.outcome is WorkdayFailureOutcome.REVIEW_REQUIRED
    assert route.gate_operation is WorkdayGateOperation.MARK_REVIEW_REQUIRED
    assert route.llm_repair_allowed is False


@pytest.mark.parametrize(
    "ambiguity_fact",
    ["timeout_after_submit", "network_ambiguity_after_submit"],
)
def test_post_submit_timeout_or_network_ambiguity_requires_review(
    ambiguity_fact: str,
) -> None:
    values = {ambiguity_fact: True}
    route = route_workday_failure(
        WorkdayFailureFacts(
            frozenset({WorkdayFailureClass.PRE_SUBMIT_TRANSIENT}),
            auth_submit_count=1,
            **values,
        )
    )

    assert route.failure_class is WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN
    assert route.outcome is WorkdayFailureOutcome.REVIEW_REQUIRED


def test_only_structural_pre_auth_drift_can_authorize_one_repair() -> None:
    repair = route_workday_failure(
        WorkdayFailureFacts(frozenset({WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH}))
    )
    safe_hold = route_workday_failure(WorkdayFailureFacts(frozenset()))

    assert repair.llm_repair_allowed is True
    assert repair.gate_operation is WorkdayGateOperation.CLAIM_LLM_REPAIR
    assert safe_hold.llm_repair_allowed is False


@pytest.mark.parametrize("failure_class", list(WorkdayFailureClass))
def test_routes_never_request_catalog_mutation_signup_or_auth_resubmit(
    failure_class: WorkdayFailureClass,
) -> None:
    submit_count = 1 if failure_class.value.startswith("post_submit_") else 0
    route = route_workday_failure(
        WorkdayFailureFacts(frozenset({failure_class}), submit_count)
    )

    assert route.catalog_history_mutation_requested is False
    assert route.signup_requested is False
    assert route.authentication_resubmit_requested is False


def test_invalid_credentials_route_to_hold_without_signup_or_resubmit() -> None:
    route = route_workday_failure(
        WorkdayFailureFacts(
            frozenset({WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED}),
            auth_submit_count=1,
        )
    )

    assert route.outcome is WorkdayFailureOutcome.CREDENTIAL_HOLD
    assert route.signup_requested is False
    assert route.authentication_resubmit_requested is False


@pytest.mark.parametrize("submit_count", [-1, 2])
def test_submit_count_is_fixed_to_zero_or_one(submit_count: int) -> None:
    with pytest.raises(ValueError, match="zero or one"):
        WorkdayFailureFacts(frozenset(), submit_count)
