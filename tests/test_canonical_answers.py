from datetime import UTC, datetime

import pytest

from services.canonical_answers import (
    CanonicalAnswerFact,
    CanonicalFactProvenance,
    CanonicalFactScope,
    CanonicalOptionMapping,
    create_approved_canonical_fact,
    resolve_canonical_answer,
)


def _fact(monkeypatch, **overrides) -> CanonicalAnswerFact:
    monkeypatch.setattr(
        "services.canonical_answers.encrypt_api_key",
        lambda value: f"enc:v1:{value[::-1]}",
    )
    monkeypatch.setattr(
        "services.canonical_answers.decrypt_api_key",
        lambda value: value.removeprefix("enc:v1:")[::-1],
    )
    values = {
        "canonical_key": "work_authorization.india",
        "answer": "Yes",
        "semantic_variants": (
            "Are you authorized to work in India?",
            "Do you have Indian work authorization?",
        ),
        "scope": CanonicalFactScope.GLOBAL,
        "provenance": CanonicalFactProvenance.USER_CONFIRMED,
        "reviewed_at": datetime(2026, 8, 23, tzinfo=UTC),
    }
    values.update(overrides)
    return create_approved_canonical_fact(**values)


def test_approved_fact_stores_only_encrypted_value(monkeypatch) -> None:
    fact = _fact(monkeypatch, answer="Yes")

    assert fact.answer_encrypted != "Yes"
    assert "Yes" not in repr(fact)


def test_exact_known_variant_resolves_without_repeat_prompt(monkeypatch) -> None:
    fact = _fact(monkeypatch)

    resolved = resolve_canonical_answer(
        "DO YOU HAVE INDIAN WORK AUTHORIZATION!",
        [fact],
        portal_hostname="wd5.myworkdayjobs.com",
    )

    assert resolved is not None
    assert resolved.canonical_key == "work_authorization.india"
    assert resolved.value == "Yes"


def test_unapproved_wording_does_not_guess_equivalence(monkeypatch) -> None:
    fact = _fact(monkeypatch)

    resolved = resolve_canonical_answer(
        "Can you legally take this job?",
        [fact],
        portal_hostname="wd5.myworkdayjobs.com",
    )

    assert resolved is None


def test_portal_scope_isolated_and_preferred_over_global(monkeypatch) -> None:
    global_fact = _fact(monkeypatch, answer="Yes")
    portal_fact = _fact(
        monkeypatch,
        answer="No",
        scope=CanonicalFactScope.PORTAL,
        portal_hostname="wd5.myworkdayjobs.com",
    )

    workday = resolve_canonical_answer(
        "Are you authorized to work in India?",
        [global_fact, portal_fact],
        portal_hostname="wd5.myworkdayjobs.com",
    )
    other = resolve_canonical_answer(
        "Are you authorized to work in India?",
        [global_fact, portal_fact],
        portal_hostname="careers.example.com",
    )

    assert workday is not None and workday.value == "No"
    assert other is not None and other.value == "Yes"


def test_option_mapping_selects_actual_visible_choice(monkeypatch) -> None:
    fact = _fact(
        monkeypatch,
        answer="No",
        option_mappings=(
            CanonicalOptionMapping(
                option_label="Not applicable",
                canonical_value="No",
            ),
        ),
    )

    resolved = resolve_canonical_answer(
        "Are you authorized to work in India?",
        [fact],
        portal_hostname=None,
        available_options=("Not applicable", "Yes"),
    )

    assert resolved is not None and resolved.value == "Not applicable"


def test_unmapped_option_set_fails_closed(monkeypatch) -> None:
    fact = _fact(monkeypatch, answer="No")

    assert (
        resolve_canonical_answer(
            "Are you authorized to work in India?",
            [fact],
            portal_hostname=None,
            available_options=("Not applicable", "Yes"),
        )
        is None
    )


def test_conflicting_facts_fail_closed(monkeypatch) -> None:
    yes = _fact(monkeypatch, answer="Yes")
    no = _fact(monkeypatch, answer="No")

    assert (
        resolve_canonical_answer(
            "Are you authorized to work in India?",
            [yes, no],
            portal_hostname=None,
        )
        is None
    )


def test_fact_requires_timezone_aware_review_date(monkeypatch) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _fact(monkeypatch, reviewed_at=datetime(2026, 8, 23))
