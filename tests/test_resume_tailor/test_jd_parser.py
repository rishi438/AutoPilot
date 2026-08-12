"""Tests for deterministic job-description parsing."""

import pytest

from services.resume_tailor.jd_parser import (
    MAX_DESCRIPTION_LENGTH,
    JobDescriptionParseError,
    parse_job_description,
)


def test_parser_extracts_profile_skills_seniority_and_sections() -> None:
    """Extract signals while preserving the user's profile-skill order."""
    analysis = parse_job_description(
        "Senior Backend Engineer\n\n"
        "Responsibilities:\n- Build Python and FastAPI services with PostgreSQL.\n"
        "Requirements:\n- 5+ years of experience with Python, Docker, and AWS.\n"
        "Preferred:\n- Kubernetes experience.\n",
        skill_vocabulary=(
            "Python",
            "FastAPI",
            "PostgreSQL",
            "Docker",
            "AWS",
            "Kubernetes",
        ),
    )

    assert analysis.seniority == "senior"
    assert analysis.years_experience == 5
    assert analysis.skills == (
        "Python",
        "FastAPI",
        "PostgreSQL",
        "Docker",
        "AWS",
        "Kubernetes",
    )
    assert "requirements" in analysis.sections
    assert "FastAPI" in analysis.keywords


def test_parser_supports_any_profession_without_a_fixed_skill_list() -> None:
    """Match a non-technology role using only its user's supplied skills."""
    analysis = parse_job_description(
        "Chef needed to prepare pastry, manage kitchen inventory, and use HACCP procedures.",
        skill_vocabulary=("Pastry", "HACCP", "Kitchen Inventory", "Python"),
    )

    assert analysis.skills == ("Pastry", "HACCP", "Kitchen Inventory")


def test_parser_supports_custom_language_configuration() -> None:
    """Allow callers to configure headings and stop words for another language."""
    analysis = parse_job_description(
        "Compétences:\nCommunication avec les patients. Communication écrite.",
        skill_vocabulary=("Communication", "Soins infirmiers"),
        section_headings={"requirements": ("Compétences",)},
        stop_words=("avec", "les"),
    )

    assert analysis.skills == ("Communication",)
    assert analysis.keywords == ("Communication",)
    assert analysis.sections["requirements"].startswith("Communication")


def test_parser_uses_lower_bound_for_experience_ranges() -> None:
    """Treat a range as its minimum requirement, not its upper bound."""
    analysis = parse_job_description("Requires 3-5 years of relevant experience.")

    assert analysis.years_experience == 3


def test_parser_deduplicates_profile_skills_case_insensitively() -> None:
    """Avoid duplicate matches when a profile repeats a skill with new casing."""
    analysis = parse_job_description(
        "Strong SQL skills are required.",
        skill_vocabulary=("SQL", "sql", " SQL "),
    )

    assert analysis.skills == ("SQL",)


def test_parser_resets_section_at_an_unknown_heading() -> None:
    """Do not attach unrelated sections to the last recognised section."""
    analysis = parse_job_description(
        "Requirements:\nRegistered nurse licence.\nBenefits:\nHealth insurance."
    )

    assert analysis.sections["requirements"] == "Registered nurse licence."


def test_parser_prefers_explicit_management_seniority() -> None:
    """Resolve overlapping seniority labels using the documented priority."""
    analysis = parse_job_description("Senior Engineering Manager")

    assert analysis.seniority == "staff"


def test_parser_normalises_keyword_punctuation() -> None:
    """Count equivalent sentence-ending tokens as the same keyword."""
    analysis = parse_job_description("Collaboration. Collaboration.")

    assert analysis.keywords == ("collaboration",)


@pytest.mark.parametrize("description", ["", "   ", None])
def test_parser_rejects_blank_descriptions(description: object) -> None:
    """Reject missing descriptions with the public domain exception."""
    with pytest.raises(JobDescriptionParseError, match="must not be blank"):
        parse_job_description(description)  # type: ignore[arg-type]


def test_parser_rejects_invalid_skill_vocabulary() -> None:
    """Report malformed profile data without leaking an internal exception."""
    with pytest.raises(JobDescriptionParseError, match="only strings"):
        parse_job_description(
            "A role description",
            skill_vocabulary=("Communication", 42),  # type: ignore[arg-type]
        )


def test_parser_rejects_a_single_string_as_skill_vocabulary() -> None:
    """Reject a string before it can be interpreted as individual characters."""
    with pytest.raises(JobDescriptionParseError, match="collection of strings"):
        parse_job_description("Go developer", skill_vocabulary="Go")


def test_parser_rejects_an_oversized_description() -> None:
    """Bound parser work before running phrase and keyword extraction."""
    with pytest.raises(JobDescriptionParseError, match="exceeds"):
        parse_job_description("x" * (MAX_DESCRIPTION_LENGTH + 1))
