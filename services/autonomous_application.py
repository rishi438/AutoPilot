"""Fail-closed lifecycle policy for autonomous job applications."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AutonomousApplicationState(str, Enum):
    """Worker lifecycle states, independent of portal and transport details."""

    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    LEASED = "leased"
    ACCOUNT_READY = "account_ready"
    FILLING = "filling"
    HELD = "held"
    RESUMED = "resumed"
    SUBMITTING = "submitting"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ConfidenceFailure(str, Enum):
    """Stable reasons why an application must not be submitted."""

    NOT_ELIGIBLE = "not_eligible"
    DUPLICATE_NOT_CLEARED = "duplicate_not_cleared"
    REQUIRED_ANSWERS_UNAPPROVED = "required_answers_unapproved"
    COMMITTED_VALUES_UNVERIFIED = "committed_values_unverified"
    RESUME_UPLOAD_UNVERIFIED = "resume_upload_unverified"
    OPEN_HOLD = "open_hold"
    BLOCKER_PRESENT = "blocker_present"


@dataclass(frozen=True, kw_only=True)
class SubmissionConfidence:
    """Evidence required immediately before the worker may submit."""

    eligibility_verified: bool
    duplicate_check_passed: bool
    required_answers_approved: bool
    committed_values_verified: bool
    resume_upload_required: bool
    resume_upload_verified: bool
    open_hold_codes: tuple[str, ...] = ()
    blocker_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfidenceDecision:
    """Deterministic confidence-gate outcome with machine-readable failures."""

    passed: bool
    failures: tuple[ConfidenceFailure, ...]


class InvalidApplicationTransition(ValueError):
    """Raised when lifecycle evidence does not permit a requested transition."""


_ALLOWED_TRANSITIONS: dict[
    AutonomousApplicationState, frozenset[AutonomousApplicationState]
] = {
    AutonomousApplicationState.DISCOVERED: frozenset(
        {
            AutonomousApplicationState.ELIGIBLE,
            AutonomousApplicationState.FAILED,
            AutonomousApplicationState.SKIPPED,
        }
    ),
    AutonomousApplicationState.ELIGIBLE: frozenset(
        {
            AutonomousApplicationState.LEASED,
            AutonomousApplicationState.FAILED,
            AutonomousApplicationState.SKIPPED,
        }
    ),
    AutonomousApplicationState.LEASED: frozenset(
        {
            AutonomousApplicationState.ACCOUNT_READY,
            AutonomousApplicationState.HELD,
            AutonomousApplicationState.FAILED,
        }
    ),
    AutonomousApplicationState.ACCOUNT_READY: frozenset(
        {
            AutonomousApplicationState.FILLING,
            AutonomousApplicationState.HELD,
            AutonomousApplicationState.FAILED,
        }
    ),
    AutonomousApplicationState.FILLING: frozenset(
        {
            AutonomousApplicationState.HELD,
            AutonomousApplicationState.SUBMITTING,
            AutonomousApplicationState.FAILED,
        }
    ),
    AutonomousApplicationState.HELD: frozenset(
        {
            AutonomousApplicationState.RESUMED,
            AutonomousApplicationState.FAILED,
            AutonomousApplicationState.SKIPPED,
        }
    ),
    AutonomousApplicationState.RESUMED: frozenset(
        {
            AutonomousApplicationState.ACCOUNT_READY,
            AutonomousApplicationState.FILLING,
            AutonomousApplicationState.HELD,
            AutonomousApplicationState.FAILED,
        }
    ),
    AutonomousApplicationState.SUBMITTING: frozenset(
        {
            AutonomousApplicationState.CONFIRMED,
            AutonomousApplicationState.HELD,
            AutonomousApplicationState.FAILED,
        }
    ),
    AutonomousApplicationState.CONFIRMED: frozenset(),
    AutonomousApplicationState.FAILED: frozenset(),
    AutonomousApplicationState.SKIPPED: frozenset(),
}


_PUBLIC_APPLICATION_STATUS: dict[AutonomousApplicationState, str] = {
    AutonomousApplicationState.DISCOVERED: "discovered",
    AutonomousApplicationState.ELIGIBLE: "queued",
    AutonomousApplicationState.LEASED: "preparing",
    AutonomousApplicationState.ACCOUNT_READY: "preparing",
    AutonomousApplicationState.FILLING: "applying",
    AutonomousApplicationState.HELD: "blocked",
    AutonomousApplicationState.RESUMED: "retrying",
    AutonomousApplicationState.SUBMITTING: "applying",
    AutonomousApplicationState.CONFIRMED: "applied",
    AutonomousApplicationState.FAILED: "failed",
    AutonomousApplicationState.SKIPPED: "skipped",
}


def evaluate_submission_confidence(
    confidence: SubmissionConfidence,
) -> ConfidenceDecision:
    """Return every failed pre-submit check without exposing form values."""
    failures: list[ConfidenceFailure] = []
    if not confidence.eligibility_verified:
        failures.append(ConfidenceFailure.NOT_ELIGIBLE)
    if not confidence.duplicate_check_passed:
        failures.append(ConfidenceFailure.DUPLICATE_NOT_CLEARED)
    if not confidence.required_answers_approved:
        failures.append(ConfidenceFailure.REQUIRED_ANSWERS_UNAPPROVED)
    if not confidence.committed_values_verified:
        failures.append(ConfidenceFailure.COMMITTED_VALUES_UNVERIFIED)
    if confidence.resume_upload_required and not confidence.resume_upload_verified:
        failures.append(ConfidenceFailure.RESUME_UPLOAD_UNVERIFIED)
    if confidence.open_hold_codes:
        failures.append(ConfidenceFailure.OPEN_HOLD)
    if confidence.blocker_codes:
        failures.append(ConfidenceFailure.BLOCKER_PRESENT)
    return ConfidenceDecision(passed=not failures, failures=tuple(failures))


def validate_application_transition(
    current: AutonomousApplicationState,
    target: AutonomousApplicationState,
    *,
    confidence: SubmissionConfidence | None = None,
    portal_confirmation_detected: bool = False,
) -> None:
    """Validate one lifecycle transition and its required evidence."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidApplicationTransition(
            f"Transition from {current.value} to {target.value} is not allowed."
        )
    if target is AutonomousApplicationState.SUBMITTING:
        if confidence is None:
            raise InvalidApplicationTransition(
                "Submission requires explicit confidence-gate evidence."
            )
        decision = evaluate_submission_confidence(confidence)
        if not decision.passed:
            reasons = ", ".join(failure.value for failure in decision.failures)
            raise InvalidApplicationTransition(
                f"Submission confidence gate failed: {reasons}."
            )
    if (
        target is AutonomousApplicationState.CONFIRMED
        and not portal_confirmation_detected
    ):
        raise InvalidApplicationTransition(
            "Confirmation requires detected portal evidence."
        )


def public_application_status(state: AutonomousApplicationState) -> str:
    """Collapse worker state into the existing user-facing application status."""
    return _PUBLIC_APPLICATION_STATUS[state]
