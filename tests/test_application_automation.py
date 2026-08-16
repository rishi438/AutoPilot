from models.database import JobFormAnswer
from api.automation import QueueJobRequest
from api.workflow import WorkflowStartRequest
from services.application_automation import (
    classify_sensitivity,
    normalize_question,
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


def test_saved_job_analysis_request_accepts_only_an_application_reference() -> None:
    request = WorkflowStartRequest(
        application_id="00000000-0000-0000-0000-000000000001"
    )

    assert str(request.application_id) == "00000000-0000-0000-0000-000000000001"
