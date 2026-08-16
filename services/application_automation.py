"""Deterministic policy helpers for application automation."""

from __future__ import annotations

import re
from collections.abc import Iterable

from models.database import JobFormAnswer

_QUESTION_TOKEN = re.compile(r"[^a-z0-9]+")
_SENSITIVE_TOKENS = frozenset(
    {
        "salary",
        "compensation",
        "disability",
        "gender",
        "race",
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


def resolve_approved_answer(
    question: str, answers: Iterable[JobFormAnswer]
) -> JobFormAnswer | None:
    """Return one approved exact-key answer, or none when missing or ambiguous."""
    key = normalize_question(question)
    matches = [
        answer
        for answer in answers
        if answer.approved_for_reuse and answer.normalized_question == key
    ]
    if len({answer.answer.strip() for answer in matches}) != 1:
        return None
    return matches[0] if matches else None
