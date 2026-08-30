from models.database import JobFormAnswer
import pytest

from api.automation import (
    QueueJobRequest,
    SaveReusableAnswerRequest,
    _merge_external_ats_url,
)
from api.workflow import WorkflowStartRequest
from services.application_automation import (
    classify_sensitivity,
    is_prohibited_answer_material,
    normalize_question,
    protect_reusable_answer,
    reusable_answer_value,
    resolve_approved_answer,
)


def _answer(question: str, value: str, *, approved: bool = True) -> JobFormAnswer:
    return JobFormAnswer(
        question=question,
        answer=value,
        normalized_question=normalize_question(question),
        approved_for_reuse=approved,
    )


def test_normalize_question_is_stable_across_punctuation_and_case() -> None:
    assert (
        normalize_question("Are you authorized to work in India?")
        == "are you authorized to work in india"
    )
    assert (
        normalize_question("ARE you authorized -- to work in India!")
        == "are you authorized to work in india"
    )


def test_resolve_answer_requires_one_approved_non_ambiguous_value() -> None:
    question = "Are you authorized to work in India?"
    assert (
        resolve_approved_answer(question, [_answer(question, "Yes", approved=False)])
        is None
    )
    assert (
        resolve_approved_answer(
            question, [_answer(question, "Yes"), _answer(question, "No")]
        )
        is None
    )
    assert (
        resolve_approved_answer(
            question, [_answer(question, "Yes"), _answer(question, "Yes")]
        ).answer
        == "Yes"
    )


def test_sensitive_questions_are_never_treated_as_standard() -> None:
    assert classify_sensitivity("What are your salary expectations?") == "sensitive"
    assert classify_sensitivity("What is your preferred work location?") == "standard"


def test_reusable_answer_is_encrypted_at_rest(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.application_automation.encrypt_api_key",
        lambda value: f"enc:v1:dummy-{value}",
    )
    monkeypatch.setattr(
        "services.application_automation.decrypt_api_key",
        lambda value: value.removeprefix("enc:v1:dummy-"),
    )
    plaintext, encrypted = protect_reusable_answer("Female")
    answer = _answer("Gender", plaintext)
    answer.answer_encrypted = encrypted

    assert answer.answer == ""
    assert answer.answer_encrypted != "Female"
    assert reusable_answer_value(answer) == "Female"


def test_prohibited_material_does_not_block_work_authorization() -> None:
    assert not is_prohibited_answer_material("Are you authorized to work in India? Yes")
    assert is_prohibited_answer_material("Authorization header: Bearer secret")


def test_queue_job_description_is_cleaned_before_persistence() -> None:
    request = QueueJobRequest(
        batch_id="00000000-0000-0000-0000-000000000001",
        portal="naukri",
        external_job_id="job-1",
        job_title="Data Engineer",
        job_url="https://www.naukri.com/job-1",
        job_description="  Build data pipelines.\n\n  Python and SQL required.  ",
    )

    assert request.job_description == "Build data pipelines. Python and SQL required."


def test_queue_job_rejects_workday_id_that_does_not_match_url() -> None:
    with pytest.raises(ValueError, match="must match the Workday job URL"):
        QueueJobRequest(
            batch_id="00000000-0000-0000-0000-000000000001",
            portal="workday",
            external_job_id="R-569995",
            job_title="Senior Software Engineer",
            job_url=(
                "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/"
                "job/Hyderabad-India/Senior-Software-Engineer_R-565447"
            ),
        )


def test_direct_workday_url_clears_stale_external_handoff() -> None:
    assert (
        _merge_external_ats_url(
            job_url=(
                "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/"
                "job/Hyderabad-India/Senior-Software-Engineer_R-565447"
            ),
            requested_url=None,
            existing_url=(
                "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/"
                "job/Hyderabad-India/Software-Engineer_R-569995"
            ),
        )
        is None
    )


def test_non_workday_card_retains_discovered_ats_handoff() -> None:
    handoff = "https://wd1.myworkdaysite.com/recruiting/example/site/job/Test_R-1"
    assert (
        _merge_external_ats_url(
            job_url="https://jobs.example.com/card-1",
            requested_url=None,
            existing_url=handoff,
        )
        == handoff
    )


def test_saved_job_analysis_request_accepts_only_an_application_reference() -> None:
    request = WorkflowStartRequest(
        application_id="00000000-0000-0000-0000-000000000001"
    )

    assert str(request.application_id) == "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    "question,answer",
    [
        ("Password", "anything"),
        ("Nationality", "Bearer abc.def.ghi"),
        ("OTP", "123456"),
        ("Payment details", "4111 1111 1111 1111"),
    ],
)
def test_reusable_answer_rejects_secrets_and_payment_data(
    question: str, answer: str
) -> None:
    with pytest.raises(ValueError):
        SaveReusableAnswerRequest(question=question, answer=answer)


def test_reusable_answer_rejects_control_characters() -> None:
    with pytest.raises(ValueError):
        SaveReusableAnswerRequest(question="Nationality", answer="Indian\x00")
