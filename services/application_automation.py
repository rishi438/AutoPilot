"""Deterministic policy helpers for application automation."""

from __future__ import annotations

import re
import logging
from collections.abc import Iterable

from models.database import JobFormAnswer
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
