import uuid
from datetime import UTC, datetime

from models.database import (
    ApplicationAutomationEvent,
    ApplicationStatus,
    JobApplication,
    JobFormAnswer,
)
import pytest

from api.automation import (
    QueueJobRequest,
    SaveReusableAnswerRequest,
    _merge_external_ats_url,
)
from api.workflow import WorkflowStartRequest
from services.application_automation import (
    classify_sensitivity,
    derive_automation_progress,
    has_unit1_completed,
    has_unit2_completed,
    is_prohibited_answer_material,
    is_stage2_eligible,
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


def test_derive_automation_progress_projects_completed_stage1() -> None:
    app_id = uuid.uuid4()
    completed_at = datetime.now(UTC)
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="application_leased",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            detail="authenticated_application_ready_submitted",
            created_at=completed_at,
        ),
    ]

    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit1"
    assert progress["stage_status"] == "completed"
    assert progress["next_stage"] == "workday_unit2"
    assert progress["next_stage_status"] == "not_started"
    assert progress["label"] == "Stage 1 complete — ready for Stage 2"
    assert progress["unit1_completed"] is True
    assert progress["completed_at"] == completed_at
    assert application.status == ApplicationStatus.APPLYING.value
    assert is_stage2_eligible(application, events) is True


def test_derive_automation_progress_projects_review_required() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.BLOCKED.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_review_required",
            detail="checkpoint_failed",
            created_at=datetime.now(UTC),
        ),
    ]

    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit1"
    assert progress["stage_status"] == "review_required"
    assert progress["label"] == "Review required"
    assert progress["unit1_completed"] is False
    assert is_stage2_eligible(application, events) is False


def test_derive_automation_progress_projects_queued_and_retrying() -> None:
    app_id = uuid.uuid4()
    app_queued = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.QUEUED.value,
        portal="workday",
    )
    progress_queued = derive_automation_progress(app_queued, [])
    assert progress_queued is not None
    assert progress_queued["stage"] == "workday_unit1"
    assert progress_queued["stage_status"] == "queued"
    assert progress_queued["unit1_completed"] is False

    app_retrying = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.RETRYING.value,
        portal="workday",
    )
    progress_retrying = derive_automation_progress(app_retrying, [])
    assert progress_retrying is not None
    assert progress_retrying["stage"] == "workday_unit1"
    assert progress_retrying["stage_status"] == "retrying"
    assert progress_retrying["unit1_completed"] is False


def test_derive_automation_progress_returns_none_for_non_automated_app() -> None:
    application = JobApplication(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=ApplicationStatus.COMPLETED.value,
    )
    assert derive_automation_progress(application, []) is None
    assert has_unit1_completed([]) is False


def test_derive_automation_progress_ignores_non_workday_queued_application() -> None:
    application = JobApplication(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=ApplicationStatus.QUEUED.value,
        portal="greenhouse",
        job_url="https://boards.greenhouse.io/example/jobs/123",
    )
    assert derive_automation_progress(application, []) is None


def test_derive_automation_progress_status_blocked_takes_precedence_over_historic_completion() -> (
    None
):
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.BLOCKED.value,
        portal="workday",
    )
    # Historic completion event exists, but current status is blocked.
    # Current stage status is review_required, but durable unit1_completed remains True.
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_review_required",
            created_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
        ),
    ]

    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage_status"] == "review_required"
    assert progress["label"] == "Review required"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is False
    assert is_stage2_eligible(application, events) is False


def test_derive_automation_progress_handles_unordered_events() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    # Events passed out of chronological order
    newer_completion = ApplicationAutomationEvent(
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=datetime(2026, 8, 30, 10, 0, 0, tzinfo=UTC),
    )
    older_leased = ApplicationAutomationEvent(
        application_id=app_id,
        event_type="application_leased",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
    )
    progress = derive_automation_progress(application, [newer_completion, older_leased])
    assert progress is not None
    assert progress["stage_status"] == "completed"
    assert progress["unit1_completed"] is True
    assert progress["completed_at"] == datetime(2026, 8, 30, 10, 0, 0, tzinfo=UTC)


def test_derive_automation_progress_deterministic_equal_timestamps() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    same_time = datetime(2026, 8, 30, 10, 0, 0, tzinfo=UTC)
    event_started = ApplicationAutomationEvent(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        application_id=app_id,
        event_type="workday_unit2_started",
        created_at=same_time,
    )
    event_u1_done = ApplicationAutomationEvent(
        id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=same_time,
    )
    # When passed in reverse ID order, ID tiebreaker ensures event_u1_done comes first, then event_started
    progress = derive_automation_progress(application, [event_started, event_u1_done])
    assert progress is not None
    assert progress["stage"] == "workday_unit2"
    assert progress["stage_status"] == "in_progress"
    assert progress["label"] == "Stage 2 in progress"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is False


def test_derive_automation_progress_unit2_in_progress() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_started",
            created_at=datetime(2026, 8, 30, 9, 10, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_save_claimed",
            created_at=datetime(2026, 8, 30, 9, 15, 0, tzinfo=UTC),
        ),
    ]
    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit2"
    assert progress["stage_status"] == "in_progress"
    assert progress["label"] == "Stage 2 in progress"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is False


def test_derive_automation_progress_unit2_review_required() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.BLOCKED.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_started",
            created_at=datetime(2026, 8, 30, 9, 10, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_review_required",
            created_at=datetime(2026, 8, 30, 9, 20, 0, tzinfo=UTC),
        ),
    ]
    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit2"
    assert progress["stage_status"] == "review_required"
    assert progress["label"] == "Review required"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is False


def test_derive_automation_progress_unit2_completed() -> None:
    app_id = uuid.uuid4()
    u2_completed_at = datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC)
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_started",
            created_at=datetime(2026, 8, 30, 9, 10, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_completed",
            created_at=u2_completed_at,
        ),
    ]
    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit2"
    assert progress["stage_status"] == "completed"
    assert progress["label"] == "Stage 2 complete"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is True
    assert progress["unit2_completed_at"] == u2_completed_at
    assert progress["completed_at"] == u2_completed_at
    assert has_unit2_completed(events) is True
    # Once Unit 2 is completed, it is no longer stage 2 eligible
    assert is_stage2_eligible(application, events) is False


def test_derive_automation_progress_unit2_failed_after_unit1() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.FAILED.value,
        portal="workday",
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        ),
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_started",
            created_at=datetime(2026, 8, 30, 9, 10, 0, tzinfo=UTC),
        ),
    ]
    progress = derive_automation_progress(application, events)
    assert progress is not None
    assert progress["stage"] == "workday_unit2"
    assert progress["stage_status"] == "failed"
    assert progress["label"] == "Failed"
    assert progress["unit1_completed"] is True
    assert progress["unit2_completed"] is False


def test_is_stage2_eligible_with_unresolved_and_resolved_unit2_review() -> None:
    app_id = uuid.uuid4()
    application = JobApplication(
        id=app_id,
        user_id=uuid.uuid4(),
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
    )
    # Unit 1 done, no Unit 2 events -> eligible
    events_u1 = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        )
    ]
    assert is_stage2_eligible(application, events_u1) is True

    # Unit 2 review_required -> not eligible
    events_review = events_u1 + [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_review_required",
            created_at=datetime(2026, 8, 30, 9, 10, 0, tzinfo=UTC),
        )
    ]
    assert is_stage2_eligible(application, events_review) is False

    # Unit 2 retry_ready after review -> eligible again
    events_retry_ready = events_review + [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit2_retry_ready",
            created_at=datetime(2026, 8, 30, 9, 20, 0, tzinfo=UTC),
        )
    ]
    assert is_stage2_eligible(application, events_retry_ready) is True
