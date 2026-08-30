from itertools import pairwise

import pytest

from services.autonomous_application import (
    AutonomousApplicationState,
    ConfidenceFailure,
    InvalidApplicationTransition,
    SubmissionConfidence,
    evaluate_submission_confidence,
    public_application_status,
    validate_application_transition,
)


def _passing_confidence() -> SubmissionConfidence:
    return SubmissionConfidence(
        eligibility_verified=True,
        duplicate_check_passed=True,
        required_answers_approved=True,
        committed_values_verified=True,
        resume_upload_required=True,
        resume_upload_verified=True,
    )


def test_complete_autonomous_path_requires_gate_and_confirmation() -> None:
    path = (
        AutonomousApplicationState.DISCOVERED,
        AutonomousApplicationState.ELIGIBLE,
        AutonomousApplicationState.LEASED,
        AutonomousApplicationState.ACCOUNT_READY,
        AutonomousApplicationState.FILLING,
    )
    for current, target in pairwise(path):
        validate_application_transition(current, target)

    validate_application_transition(
        AutonomousApplicationState.FILLING,
        AutonomousApplicationState.SUBMITTING,
        confidence=_passing_confidence(),
    )
    validate_application_transition(
        AutonomousApplicationState.SUBMITTING,
        AutonomousApplicationState.CONFIRMED,
        portal_confirmation_detected=True,
    )


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"eligibility_verified": False}, ConfidenceFailure.NOT_ELIGIBLE),
        (
            {"duplicate_check_passed": False},
            ConfidenceFailure.DUPLICATE_NOT_CLEARED,
        ),
        (
            {"required_answers_approved": False},
            ConfidenceFailure.REQUIRED_ANSWERS_UNAPPROVED,
        ),
        (
            {"committed_values_verified": False},
            ConfidenceFailure.COMMITTED_VALUES_UNVERIFIED,
        ),
        (
            {"resume_upload_verified": False},
            ConfidenceFailure.RESUME_UPLOAD_UNVERIFIED,
        ),
        ({"open_hold_codes": ("captcha",)}, ConfidenceFailure.OPEN_HOLD),
        (
            {"blocker_codes": ("payment_field",)},
            ConfidenceFailure.BLOCKER_PRESENT,
        ),
    ],
)
def test_confidence_gate_fails_closed(
    override: dict[str, object], expected: ConfidenceFailure
) -> None:
    values = {
        "eligibility_verified": True,
        "duplicate_check_passed": True,
        "required_answers_approved": True,
        "committed_values_verified": True,
        "resume_upload_required": True,
        "resume_upload_verified": True,
    }
    values.update(override)

    decision = evaluate_submission_confidence(SubmissionConfidence(**values))

    assert decision.passed is False
    assert expected in decision.failures


def test_submit_transition_requires_explicit_passing_gate() -> None:
    with pytest.raises(InvalidApplicationTransition, match="explicit confidence"):
        validate_application_transition(
            AutonomousApplicationState.FILLING,
            AutonomousApplicationState.SUBMITTING,
        )

    failing = SubmissionConfidence(
        eligibility_verified=True,
        duplicate_check_passed=False,
        required_answers_approved=True,
        committed_values_verified=True,
        resume_upload_required=False,
        resume_upload_verified=False,
    )
    with pytest.raises(InvalidApplicationTransition, match="duplicate_not_cleared"):
        validate_application_transition(
            AutonomousApplicationState.FILLING,
            AutonomousApplicationState.SUBMITTING,
            confidence=failing,
        )


def test_confirmed_transition_requires_detected_portal_confirmation() -> None:
    with pytest.raises(InvalidApplicationTransition, match="portal evidence"):
        validate_application_transition(
            AutonomousApplicationState.SUBMITTING,
            AutonomousApplicationState.CONFIRMED,
        )


def test_hold_can_resume_but_cannot_jump_directly_to_submission() -> None:
    validate_application_transition(
        AutonomousApplicationState.HELD,
        AutonomousApplicationState.RESUMED,
    )
    with pytest.raises(InvalidApplicationTransition, match="not allowed"):
        validate_application_transition(
            AutonomousApplicationState.RESUMED,
            AutonomousApplicationState.SUBMITTING,
            confidence=_passing_confidence(),
        )


def test_only_confirmed_state_maps_to_applied() -> None:
    applied_states = {
        state
        for state in AutonomousApplicationState
        if public_application_status(state) == "applied"
    }

    assert applied_states == {AutonomousApplicationState.CONFIRMED}
