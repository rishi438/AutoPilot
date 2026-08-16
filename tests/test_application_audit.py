import pytest
from pydantic import ValidationError

from api.automation import RecordResultRequest
from api.applications import _safe_audit_detail


def test_submitted_answers_flag_ai_unknown_and_changed_values_for_review() -> None:
    request = RecordResultRequest(
        lease_id="00000000-0000-0000-0000-000000000001",
        result="applied",
        submitted_answers=[
            {
                "question": "Are you authorized to work in India?",
                "answer": "Yes",
                "answer_source": "ai",
                "changed_from_previous": True,
            }
        ],
    )

    answer = request.submitted_answers[0]
    assert answer.answer_source == "ai"
    assert answer.changed_from_previous is True


def test_submitted_answers_reject_browser_secret_questions() -> None:
    with pytest.raises(ValidationError, match="Browser credentials"):
        RecordResultRequest(
            lease_id="00000000-0000-0000-0000-000000000001",
            result="applied",
            submitted_answers=[
                {
                    "question": "What is your portal password?",
                    "answer": "not-recorded",
                    "answer_source": "manual",
                }
            ],
        )


def test_audit_detail_redacts_browser_secret_markers() -> None:
    assert _safe_audit_detail("confirmation saved") == "confirmation saved"
    assert _safe_audit_detail("cookie refreshed") == "[redacted]"
