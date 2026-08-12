"""Extract deterministic, user-specific signals from job descriptions.

The parser does not contain a profession-specific skill catalogue. Callers
provide the current user's saved skills, and may override language-dependent
section headings and stop words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from utils.logging_config import get_structured_logger

MAX_DESCRIPTION_LENGTH = 20_000
MAX_TERMS = 500
MAX_TERM_LENGTH = 200

DEFAULT_SECTION_HEADINGS: Mapping[str, tuple[str, ...]] = {
    "responsibilities": (
        "responsibilities",
        "what you will do",
        "your impact",
        "the role",
    ),
    "requirements": (
        "requirements",
        "qualifications",
        "what you bring",
        "must have",
    ),
    "preferred": ("preferred", "nice to have", "bonus", "desired"),
}

DEFAULT_STOP_WORDS = frozenset(
    {
        "and",
        "are",
        "experience",
        "for",
        "from",
        "have",
        "our",
        "skills",
        "team",
        "that",
        "the",
        "this",
        "will",
        "with",
        "work",
        "you",
        "your",
    }
)

# Ordered from the most specific/highest level so overlapping titles resolve
# consistently (for example, "senior engineering manager" becomes "staff").
SENIORITY_PATTERNS = (
    ("staff", r"\b(?:staff|principal|architect|manager|director|head of)\b"),
    ("senior", r"\b(?:senior|sr\.?|lead)\b"),
    ("mid", r"\bmid[ -]?level\b"),
    ("entry", r"\b(?:entry[ -]?level|junior|graduate)\b"),
    ("intern", r"\b(?:intern|internship)\b"),
)

YEAR_RANGE_PATTERN = re.compile(
    r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
SINGLE_YEAR_PATTERN = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"[^\W\d_][\w+#.-]{2,}", re.UNICODE)

logger = get_structured_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobDescriptionAnalysis:
    """Structured facts extracted from one job description.

    Attributes:
        skills: User-profile skills found in the description.
        keywords: Matched skills followed by repeated description terms.
        seniority: Explicit seniority label, when one is present.
        years_experience: Highest minimum experience requirement found.
        sections: Text grouped under recognised section headings.
    """

    skills: tuple[str, ...]
    keywords: tuple[str, ...]
    seniority: str | None
    years_experience: int | None
    sections: Mapping[str, str]


class JobDescriptionParseError(ValueError):
    """Report a job-description validation or parsing failure."""


def _normalise(text: str) -> str:
    """Case-fold text and collapse whitespace for phrase matching."""
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _normalise_terms(
    terms: Iterable[str] | None,
    *,
    field_name: str,
) -> tuple[str, ...]:
    """Validate terms and remove case-insensitive duplicates in input order."""
    if terms is None:
        return ()
    if isinstance(terms, str):
        raise JobDescriptionParseError(f"{field_name} must be a collection of strings.")

    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if not isinstance(term, str):
            raise JobDescriptionParseError(f"{field_name} must contain only strings.")
        clean_term = term.strip()
        if len(clean_term) > MAX_TERM_LENGTH:
            raise JobDescriptionParseError(
                f"{field_name} entries must not exceed {MAX_TERM_LENGTH} characters."
            )
        key = clean_term.casefold()
        if clean_term and key not in seen:
            unique_terms.append(clean_term)
            seen.add(key)
        if len(unique_terms) > MAX_TERMS:
            raise JobDescriptionParseError(
                f"{field_name} must not contain more than {MAX_TERMS} entries."
            )
    return tuple(unique_terms)


def _extract_skills(
    description: str,
    skill_vocabulary: Iterable[str] | None,
) -> tuple[str, ...]:
    """Return caller-provided skills that occur in the description."""
    normalised_description = _normalise(description)
    skills = _normalise_terms(skill_vocabulary, field_name="skill_vocabulary")
    return tuple(
        skill
        for skill in skills
        if re.search(
            rf"(?<!\w){re.escape(skill.casefold())}(?!\w)",
            normalised_description,
        )
    )


def _extract_years(description: str) -> int | None:
    """Return the highest minimum years-of-experience requirement."""
    range_starts = [
        int(match.group(1)) for match in YEAR_RANGE_PATTERN.finditer(description)
    ]
    without_ranges = YEAR_RANGE_PATTERN.sub("", description)
    single_values = [
        int(match.group(1)) for match in SINGLE_YEAR_PATTERN.finditer(without_ranges)
    ]
    requirements = [*range_starts, *single_values]
    return max(requirements) if requirements else None


def _heading_lookup(
    section_headings: Mapping[str, Iterable[str]],
) -> dict[str, str]:
    """Map every normalised heading alias to its canonical section name."""
    lookup: dict[str, str] = {}
    for section_name, aliases in section_headings.items():
        if not isinstance(section_name, str) or not section_name.strip():
            raise JobDescriptionParseError(
                "section_headings keys must be non-empty strings."
            )
        for alias in _normalise_terms(aliases, field_name="section_headings aliases"):
            lookup[alias.casefold().rstrip(":")] = section_name.strip()
    return lookup


def _extract_sections(
    description: str,
    section_headings: Mapping[str, Iterable[str]],
) -> dict[str, str]:
    """Group description lines under configurable section headings."""
    heading_lookup = _heading_lookup(section_headings)
    collected: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in description.splitlines():
        line = raw_line.strip(" -•\t")
        if not line:
            continue

        possible_heading, separator, inline_content = line.partition(":")
        section = heading_lookup.get(possible_heading.casefold().strip())
        if section is None and not separator:
            section = heading_lookup.get(line.casefold().rstrip(":"))

        if section is not None:
            current_section = section
            if inline_content.strip():
                collected.setdefault(section, []).append(inline_content.strip())
            continue

        if separator and not inline_content.strip():
            current_section = None
            continue

        if current_section is not None:
            collected.setdefault(current_section, []).append(line)

    return {name: "\n".join(lines) for name, lines in collected.items()}


def _extract_keywords(
    description: str,
    skills: tuple[str, ...],
    stop_words: Iterable[str] | None,
) -> tuple[str, ...]:
    """Return matched skills and repeated non-stop-word terms."""
    configured_stop_words = (
        DEFAULT_STOP_WORDS
        if stop_words is None
        else frozenset(
            term.casefold()
            for term in _normalise_terms(stop_words, field_name="stop_words")
        )
    )
    counts: dict[str, int] = {}
    for raw_word in WORD_PATTERN.findall(description.casefold()):
        word = raw_word.strip(".-")
        if word not in configured_stop_words:
            counts[word] = counts.get(word, 0) + 1

    skill_keys = {skill.casefold() for skill in skills}
    frequent_words = sorted(
        (
            word
            for word, count in counts.items()
            if count >= 2 and word.casefold() not in skill_keys
        ),
        key=lambda word: (-counts[word], word),
    )
    return (*skills, *frequent_words)


def parse_job_description(
    description: str,
    *,
    skill_vocabulary: Iterable[str] | None = None,
    section_headings: Mapping[str, Iterable[str]] | None = None,
    stop_words: Iterable[str] | None = None,
) -> JobDescriptionAnalysis:
    """Parse a job description into deterministic tailoring signals.

    Args:
        description: Raw job-description text to analyse.
        skill_vocabulary: Skills from the current user's profile. Only these
            skills can appear in the ``skills`` result. Matches preserve the
            profile order and remove case-insensitive duplicates.
        section_headings: Optional canonical section names mapped to accepted
            heading aliases. This supports portal-specific wording and other
            languages without changing parser code.
        stop_words: Optional words excluded from keyword frequency analysis.
            Passing a collection replaces the default English stop words.

    Returns:
        A structured analysis containing matched skills, repeated keywords,
        explicit seniority, experience requirements, and grouped sections.

    Raises:
        JobDescriptionParseError: If input validation fails or the description
            cannot be parsed safely.
    """
    try:
        if not isinstance(description, str) or not description.strip():
            raise JobDescriptionParseError("Job description must not be blank.")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise JobDescriptionParseError(
                f"Job description exceeds {MAX_DESCRIPTION_LENGTH} characters."
            )

        headings = (
            DEFAULT_SECTION_HEADINGS if section_headings is None else section_headings
        )
        skills = _extract_skills(description, skill_vocabulary)
        normalised_description = _normalise(description)
        seniority = next(
            (
                name
                for name, pattern in SENIORITY_PATTERNS
                if re.search(pattern, normalised_description)
            ),
            None,
        )
        return JobDescriptionAnalysis(
            skills=skills,
            keywords=_extract_keywords(description, skills, stop_words),
            seniority=seniority,
            years_experience=_extract_years(description),
            sections=MappingProxyType(_extract_sections(description, headings)),
        )
    except JobDescriptionParseError:
        raise
    except (TypeError, ValueError, re.error) as exc:
        logger.error("Job description parsing failed", exc_info=True)
        raise JobDescriptionParseError("Job description could not be parsed.") from exc
