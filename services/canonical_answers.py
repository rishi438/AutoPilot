"""Canonical, encrypted answer-family contracts for autonomous form filling."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from services.application_automation import normalize_question
from utils.encryption import decrypt_api_key, encrypt_api_key

_CANONICAL_KEY = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_PORTAL_HOSTNAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*"
)


class CanonicalFactScope(str, Enum):
    """Where an approved fact may be reused."""

    GLOBAL = "global"
    PORTAL = "portal"


class CanonicalFactProvenance(str, Enum):
    """How a user-approved canonical fact entered the system."""

    PROFILE = "profile"
    USER_CONFIRMED = "user_confirmed"
    MIGRATED_APPROVED_ANSWER = "migrated_approved_answer"


@dataclass(frozen=True)
class CanonicalOptionMapping:
    """Map one portal choice label to the fact's canonical answer value."""

    option_label: str
    canonical_value: str

    def __post_init__(self) -> None:
        if not normalize_question(self.option_label):
            raise ValueError("Option labels must contain letters or numbers.")
        if not normalize_question(self.canonical_value):
            raise ValueError("Canonical option values must contain letters or numbers.")


@dataclass(frozen=True)
class CanonicalAnswerFact:
    """One approved encrypted fact and the exact wording family it answers."""

    canonical_key: str
    answer_encrypted: str = field(repr=False)
    semantic_variants: tuple[str, ...]
    option_mappings: tuple[CanonicalOptionMapping, ...]
    scope: CanonicalFactScope
    provenance: CanonicalFactProvenance
    reviewed_at: datetime
    approved_for_reuse: bool
    portal_hostname: str | None = None

    def __post_init__(self) -> None:
        if not _CANONICAL_KEY.fullmatch(self.canonical_key):
            raise ValueError("Canonical keys must use lowercase dotted identifiers.")
        if not self.answer_encrypted:
            raise ValueError("Canonical answers must be encrypted.")
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise ValueError("Review timestamps must be timezone-aware.")
        normalized_variants = tuple(
            dict.fromkeys(normalize_question(value) for value in self.semantic_variants)
        )
        if not normalized_variants or any(not value for value in normalized_variants):
            raise ValueError("At least one meaningful semantic variant is required.")
        object.__setattr__(self, "semantic_variants", normalized_variants)
        if self.scope is CanonicalFactScope.GLOBAL and self.portal_hostname is not None:
            raise ValueError("Global facts cannot specify a portal hostname.")
        if self.scope is CanonicalFactScope.PORTAL:
            hostname = (self.portal_hostname or "").strip().lower()
            if not hostname or not _PORTAL_HOSTNAME.fullmatch(hostname):
                raise ValueError("Portal-scoped facts require a valid hostname.")
            object.__setattr__(self, "portal_hostname", hostname)


@dataclass(frozen=True)
class ResolvedCanonicalAnswer:
    """Plaintext is returned only at the form-fill boundary."""

    canonical_key: str
    value: str = field(repr=False)
    provenance: CanonicalFactProvenance
    reviewed_at: datetime


def create_approved_canonical_fact(
    *,
    canonical_key: str,
    answer: str,
    semantic_variants: Sequence[str],
    option_mappings: Sequence[CanonicalOptionMapping] = (),
    scope: CanonicalFactScope,
    provenance: CanonicalFactProvenance,
    reviewed_at: datetime,
    portal_hostname: str | None = None,
) -> CanonicalAnswerFact:
    """Encrypt a user-approved value and construct its reusable fact model."""
    value = answer.strip()
    if not value:
        raise ValueError("Canonical answers must not be blank.")
    return CanonicalAnswerFact(
        canonical_key=canonical_key,
        answer_encrypted=encrypt_api_key(value),
        semantic_variants=tuple(semantic_variants),
        option_mappings=tuple(option_mappings),
        scope=scope,
        provenance=provenance,
        reviewed_at=reviewed_at,
        approved_for_reuse=True,
        portal_hostname=portal_hostname,
    )


def resolve_canonical_answer(
    question: str,
    facts: Iterable[CanonicalAnswerFact],
    *,
    portal_hostname: str | None,
    available_options: Sequence[str] = (),
) -> ResolvedCanonicalAnswer | None:
    """Resolve an exact known variant, failing closed on ambiguity or bad options."""
    question_key = normalize_question(question)
    hostname = (portal_hostname or "").strip().lower() or None
    candidates = [
        fact
        for fact in facts
        if fact.approved_for_reuse
        and question_key in fact.semantic_variants
        and (
            fact.scope is CanonicalFactScope.GLOBAL or fact.portal_hostname == hostname
        )
    ]
    portal_candidates = [
        fact for fact in candidates if fact.scope is CanonicalFactScope.PORTAL
    ]
    if portal_candidates:
        candidates = portal_candidates

    resolved: list[tuple[CanonicalAnswerFact, str]] = []
    for fact in candidates:
        try:
            value = decrypt_api_key(fact.answer_encrypted).strip()
        except ValueError:
            continue
        if value:
            resolved.append((fact, value))
    identities = {(fact.canonical_key, value) for fact, value in resolved}
    if len(identities) != 1:
        return None

    fact, canonical_value = resolved[0]
    fill_value = _select_available_option(
        canonical_value,
        available_options,
        fact.option_mappings,
    )
    if available_options and fill_value is None:
        return None
    return ResolvedCanonicalAnswer(
        canonical_key=fact.canonical_key,
        value=fill_value or canonical_value,
        provenance=fact.provenance,
        reviewed_at=fact.reviewed_at,
    )


def _select_available_option(
    canonical_value: str,
    available_options: Sequence[str],
    mappings: Sequence[CanonicalOptionMapping],
) -> str | None:
    answer_key = normalize_question(canonical_value)
    for option in available_options:
        if normalize_question(option) == answer_key:
            return option
    mapped_values = {
        normalize_question(mapping.option_label): normalize_question(
            mapping.canonical_value
        )
        for mapping in mappings
    }
    for option in available_options:
        if mapped_values.get(normalize_question(option)) == answer_key:
            return option
    return None
