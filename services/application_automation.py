"""Deterministic policy helpers for application automation."""

from __future__ import annotations

import re
import logging
from collections.abc import Iterable

from typing import Any
from datetime import UTC, datetime

from models.database import (
    ApplicationAutomationEvent,
    ApplicationStatus,
    JobApplication,
    JobFormAnswer,
)
from utils.encryption import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)

_QUESTION_TOKEN = re.compile(r"[^a-z0-9]+")
_SENSITIVE_TOKENS = frozenset(
    {
        "birth",
        "citizenship",
        "salary",
        "compensation",
        "disability",
        "dob",
        "gender",
        "health",
        "marital",
        "medical",
        "nationality",
        "pan",
        "passport",
        "race",
        "religion",
        "ssn",
        "tax",
        "ethnicity",
        "veteran",
        "authorization",
        "visa",
    }
)


def normalize_question(question: str) -> str:
    """Return a stable key for an exact, conservative reusable-answer lookup."""
    return _QUESTION_TOKEN.sub(" ", question.lower()).strip()


def classify_sensitivity(question: str) -> str:
    """Classify questions that require explicit per-answer approval."""
    tokens = set(normalize_question(question).split())
    return "sensitive" if tokens & _SENSITIVE_TOKENS else "standard"


def protect_reusable_answer(value: str) -> tuple[str, str]:
    """Return legacy-placeholder and encrypted-at-rest values for persistence."""
    return "", encrypt_api_key(value)


def reusable_answer_value(answer: JobFormAnswer) -> str | None:
    """Resolve one reusable value without exposing ciphertext or corrupt data."""
    if answer.answer_encrypted:
        try:
            return decrypt_api_key(answer.answer_encrypted)
        except ValueError:
            logger.warning("Could not decrypt reusable answer id=%s", answer.id)
            return None
    value = (answer.answer or "").strip()
    return value or None


def is_prohibited_answer_material(value: str) -> bool:
    """Reject browser/payment secrets while allowing legitimate profile facts."""
    markers = (
        r"password|passcode|otp|one[ -]?time[ -]?code|cookie|"
        r"session(?:\s+value)?|access[ -]?token|bearer|authorization\s+header|"
        r"api[ -]?key|credit[ -]?card|card\s+number|cvv|payment\s+secret"
    )
    return bool(re.search(markers, value, re.IGNORECASE))


def resolve_approved_answer(
    question: str, answers: Iterable[JobFormAnswer]
) -> JobFormAnswer | None:
    """Return one approved exact-key answer, or none when missing or ambiguous."""
    key = normalize_question(question)
    matches = [
        (answer, value)
        for answer in answers
        if answer.approved_for_reuse and answer.normalized_question == key
        if (value := reusable_answer_value(answer)) is not None
    ]
    if len({value.strip() for _, value in matches}) != 1:
        return None
    return matches[0][0] if matches else None


PROGRESS_AUTOMATION_EVENT_TYPES: tuple[str, ...] = (
    "workday_unit1_completed",
    "workday_unit1_review_required",
    "workday_unit2_started",
    "workday_unit2_save_claimed",
    "workday_unit2_review_required",
    "workday_unit2_retry_ready",
    "workday_unit2_completed",
)

_WORKDAY_URL_PATTERN = re.compile(
    r"^https://(?:[a-z0-9-]+\.)*(?:myworkdayjobs|myworkdaysite)\.com(?:[/:?#]|$)",
    re.IGNORECASE,
)


def _is_workday_application(
    application: JobApplication,
    events: list[ApplicationAutomationEvent],
) -> bool:
    """Return True if application is Workday-based via portal, URL, or events."""
    portal = (application.portal or "").strip().lower()
    if portal == "workday":
        return True
    for url in (application.external_ats_url, application.job_url):
        if url and _WORKDAY_URL_PATTERN.match(url):
            return True
    return any(
        bool(
            e.event_type
            and (
                e.event_type.startswith("workday_unit1_")
                or e.event_type.startswith("workday_unit2_")
            )
        )
        for e in events
    )


def _sort_events(
    events: Iterable[ApplicationAutomationEvent] | None,
) -> list[ApplicationAutomationEvent]:
    """Sort events deterministically by (created_at, id)."""
    min_date = datetime.min.replace(tzinfo=UTC)
    return sorted(
        list(events) if events is not None else [],
        key=lambda e: (
            e.created_at if e.created_at is not None else min_date,
            str(getattr(e, "id", "") or ""),
        ),
    )


def has_unit1_completed(
    events: Iterable[ApplicationAutomationEvent] | None = None,
) -> bool:
    """Return True if durable workday_unit1_completed evidence exists."""
    if not events:
        return False
    return any(e.event_type == "workday_unit1_completed" for e in events)


def has_unit2_completed(
    events: Iterable[ApplicationAutomationEvent] | None = None,
) -> bool:
    """Return True if durable workday_unit2_completed evidence exists."""
    if not events:
        return False
    return any(e.event_type == "workday_unit2_completed" for e in events)


def has_unresolved_unit2_review(
    events: Iterable[ApplicationAutomationEvent] | None = None,
) -> bool:
    """Return True if the latest Unit 2 review/retry lifecycle event is review_required."""
    if not events:
        return False
    sorted_events = _sort_events(events)
    for event in reversed(sorted_events):
        if event.event_type == "workday_unit2_review_required":
            return True
        if event.event_type in (
            "workday_unit2_retry_ready",
            "workday_unit2_started",
            "workday_unit2_completed",
        ):
            return False
    return False


_has_unresolved_unit2_review = has_unresolved_unit2_review


def derive_automation_progress(
    application: JobApplication,
    events: Iterable[ApplicationAutomationEvent] | None = None,
) -> dict[str, Any] | None:
    """Project safe, ownership-scoped automation progress from durable events.

    Derives sub-stage progress while keeping JobApplication.status = 'applying'
    without mutating the main ApplicationStatus enum.
    """
    sorted_events = _sort_events(events)

    if not _is_workday_application(application, sorted_events):
        return None

    unit1_completed_event = next(
        (
            e
            for e in reversed(sorted_events)
            if e.event_type == "workday_unit1_completed"
        ),
        None,
    )
    unit2_completed_event = next(
        (
            e
            for e in reversed(sorted_events)
            if e.event_type == "workday_unit2_completed"
        ),
        None,
    )
    unit1_completed = unit1_completed_event is not None
    unit2_completed = unit2_completed_event is not None
    unit2_completed_at = unit2_completed_event.created_at if unit2_completed else None

    unit2_lifecycle_events = [
        e
        for e in sorted_events
        if e.event_type
        in (
            "workday_unit2_started",
            "workday_unit2_save_claimed",
            "workday_unit2_review_required",
            "workday_unit2_retry_ready",
            "workday_unit2_completed",
        )
    ]
    latest_u2_event = unit2_lifecycle_events[-1] if unit2_lifecycle_events else None

    # Current application status takes precedence over historic events,
    # but durable completion receipts are preserved across top-level holds/failures.
    if application.status == ApplicationStatus.BLOCKED.value:
        # Require an actual Unit 2 review receipt rather than inferring from top-level blocked
        if _has_unresolved_unit2_review(sorted_events):
            return {
                "stage": "workday_unit2",
                "stage_status": "review_required",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Review required",
                "unit1_completed": unit1_completed,
                "completed_at": None,
                "unit2_completed": unit2_completed,
                "unit2_completed_at": unit2_completed_at,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "review_required",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Review required",
            "unit1_completed": unit1_completed,
            "completed_at": None,
            "unit2_completed": unit2_completed,
            "unit2_completed_at": unit2_completed_at,
        }

    if application.status == ApplicationStatus.FAILED.value:
        if unit1_completed and not unit2_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "failed",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Failed",
                "unit1_completed": True,
                "completed_at": None,
                "unit2_completed": False,
                "unit2_completed_at": None,
            }
        if unit2_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "completed",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Stage 2 complete",
                "unit1_completed": True,
                "completed_at": unit2_completed_event.created_at,
                "unit2_completed": True,
                "unit2_completed_at": unit2_completed_at,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "failed",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Failed",
            "unit1_completed": False,
            "completed_at": None,
            "unit2_completed": False,
            "unit2_completed_at": None,
        }

    if application.status == ApplicationStatus.APPLYING.value:
        if unit2_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "completed",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Stage 2 complete",
                "unit1_completed": True,
                "completed_at": unit2_completed_event.created_at,
                "unit2_completed": True,
                "unit2_completed_at": unit2_completed_at,
            }
        if unit1_completed:
            if latest_u2_event and latest_u2_event.event_type in (
                "workday_unit2_started",
                "workday_unit2_save_claimed",
                "workday_unit2_retry_ready",
            ):
                return {
                    "stage": "workday_unit2",
                    "stage_status": "in_progress",
                    "next_stage": None,
                    "next_stage_status": None,
                    "label": "Stage 2 in progress",
                    "unit1_completed": True,
                    "completed_at": None,
                    "unit2_completed": False,
                    "unit2_completed_at": None,
                }
            return {
                "stage": "workday_unit1",
                "stage_status": "completed",
                "next_stage": "workday_unit2",
                "next_stage_status": "not_started",
                "label": "Stage 1 complete — ready for Stage 2",
                "unit1_completed": True,
                "completed_at": unit1_completed_event.created_at,
                "unit2_completed": False,
                "unit2_completed_at": None,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "in_progress",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Applying",
            "unit1_completed": False,
            "completed_at": None,
            "unit2_completed": False,
            "unit2_completed_at": None,
        }

    if application.status == ApplicationStatus.RETRYING.value:
        if unit1_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "retrying",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Retrying Stage 2",
                "unit1_completed": True,
                "completed_at": None,
                "unit2_completed": unit2_completed,
                "unit2_completed_at": unit2_completed_at,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "retrying",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Retrying Stage 1",
            "unit1_completed": False,
            "completed_at": None,
            "unit2_completed": False,
            "unit2_completed_at": None,
        }

    if application.status == ApplicationStatus.QUEUED.value:
        if unit1_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "queued",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Queued for Stage 2",
                "unit1_completed": True,
                "completed_at": None,
                "unit2_completed": unit2_completed,
                "unit2_completed_at": unit2_completed_at,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "queued",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Queued for Stage 1",
            "unit1_completed": False,
            "completed_at": None,
            "unit2_completed": False,
            "unit2_completed_at": None,
        }

    if application.status == ApplicationStatus.PREPARING.value:
        if unit1_completed:
            return {
                "stage": "workday_unit2",
                "stage_status": "in_progress",
                "next_stage": None,
                "next_stage_status": None,
                "label": "Stage 2 in progress",
                "unit1_completed": True,
                "completed_at": None,
                "unit2_completed": unit2_completed,
                "unit2_completed_at": unit2_completed_at,
            }
        return {
            "stage": "workday_unit1",
            "stage_status": "in_progress",
            "next_stage": None,
            "next_stage_status": None,
            "label": "Stage 1 in progress",
            "unit1_completed": False,
            "completed_at": None,
            "unit2_completed": False,
            "unit2_completed_at": None,
        }

    return None


def is_stage2_eligible(
    application: JobApplication,
    events: Iterable[ApplicationAutomationEvent] | None = None,
) -> bool:
    """Return True if application is applying, Workday, Stage 1 complete, Stage 2 incomplete, and no open Unit 2 review."""
    if application.status != ApplicationStatus.APPLYING.value:
        return False
    sorted_events = _sort_events(events)
    if not _is_workday_application(application, sorted_events):
        return False
    if not has_unit1_completed(sorted_events):
        return False
    if has_unit2_completed(sorted_events):
        return False
    if _has_unresolved_unit2_review(sorted_events):
        return False
    return True
