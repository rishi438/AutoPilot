from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.applications import (
    ApplicationResponse,
    AutomationProgressResponse,
    _format_application_response,
)
from models.database import (
    ApplicationAutomationEvent,
    ApplicationStatus,
    JobApplication,
)


class _MockScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _MockDatabase:
    def __init__(self, events=None, workflow_sessions=None):
        self.events = events or []
        self.workflow_sessions = workflow_sessions or []

    async def execute(self, statement):
        del statement
        return _MockScalarResult(self.events)


@pytest.mark.asyncio
async def test_format_application_response_projects_unit1_completed() -> None:
    app_id = uuid.uuid4()
    user_id = uuid.uuid4()
    completed_at = datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.APPLYING.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="workday",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_completed",
            detail="authenticated_application_ready_submitted",
            created_at=completed_at,
        )
    ]
    db = _MockDatabase(events=events)

    response = await _format_application_response(app, db)

    assert isinstance(response, ApplicationResponse)
    assert response.status == "applying"
    assert response.automation_progress is not None
    assert response.automation_progress.stage == "workday_unit1"
    assert response.automation_progress.stage_status == "completed"
    assert response.automation_progress.next_stage == "workday_unit2"
    assert response.automation_progress.next_stage_status == "not_started"
    assert response.automation_progress.label == "Stage 1 complete — ready for Stage 2"
    assert response.automation_progress.unit1_completed is True
    assert response.automation_progress.completed_at == completed_at


@pytest.mark.asyncio
async def test_format_application_response_uses_events_map_to_avoid_n_plus_1() -> None:
    app_id = uuid.uuid4()
    user_id = uuid.uuid4()
    completed_at = datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.APPLYING.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="workday",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )
    events_map = {
        app_id: [
            ApplicationAutomationEvent(
                application_id=app_id,
                event_type="workday_unit1_completed",
                detail="authenticated_application_ready_submitted",
                created_at=completed_at,
            )
        ]
    }

    class FailOnExecuteDb:
        async def execute(self, statement):
            raise AssertionError(
                "db.execute should not be called when events_map is passed"
            )

    response = await _format_application_response(
        app, FailOnExecuteDb(), events_map=events_map
    )

    assert response.automation_progress is not None
    assert response.automation_progress.unit1_completed is True
    assert response.automation_progress.label == "Stage 1 complete — ready for Stage 2"


@pytest.mark.asyncio
async def test_format_application_response_projects_review_required() -> None:
    app_id = uuid.uuid4()
    user_id = uuid.uuid4()

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.BLOCKED.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="workday",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )
    events = [
        ApplicationAutomationEvent(
            application_id=app_id,
            event_type="workday_unit1_review_required",
            detail="checkpoint_failed",
            created_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
        )
    ]
    db = _MockDatabase(events=events)

    response = await _format_application_response(app, db)

    assert response.status == "blocked"
    assert response.automation_progress is not None
    assert response.automation_progress.stage == "workday_unit1"
    assert response.automation_progress.stage_status == "review_required"
    assert response.automation_progress.label == "Review required"
    assert response.automation_progress.unit1_completed is False


def test_automation_progress_model_validation() -> None:
    progress = AutomationProgressResponse(
        stage="workday_unit1",
        stage_status="completed",
        next_stage="workday_unit2",
        next_stage_status="not_started",
        label="Stage 1 complete — ready for Stage 2",
        unit1_completed=True,
    )
    assert progress.unit1_completed is True
    assert progress.next_stage == "workday_unit2"


@pytest.mark.asyncio
async def test_format_application_response_returns_none_progress_for_non_workday() -> (
    None
):
    app_id = uuid.uuid4()
    user_id = uuid.uuid4()

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.QUEUED.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="greenhouse",
        job_url="https://boards.greenhouse.io/example/jobs/123",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )
    db = _MockDatabase(events=[])

    response = await _format_application_response(app, db)

    assert response.status == "queued"
    assert response.automation_progress is None


@pytest.mark.asyncio
async def test_format_application_response_projects_preparing_stage1_in_progress() -> (
    None
):
    app_id = uuid.uuid4()
    user_id = uuid.uuid4()

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.PREPARING.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/recruiting/acme/jobs/job/1",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )
    db = _MockDatabase(events=[])

    response = await _format_application_response(app, db)

    assert response.status == "preparing"
    assert response.automation_progress is not None
    assert response.automation_progress.stage == "workday_unit1"
    assert response.automation_progress.stage_status == "in_progress"
    assert response.automation_progress.label == "Stage 1 in progress"
    assert response.automation_progress.unit1_completed is False


@pytest.mark.asyncio
async def test_progress_events_query_filters_to_allowlisted_event_types() -> None:
    from sqlalchemy.dialects import postgresql

    app_id = uuid.uuid4()
    user_id = uuid.uuid4()
    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.APPLYING.value,
        job_title="Software Engineer",
        company_name="Acme Corp",
        portal="workday",
        created_at=datetime(2026, 8, 30, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 30, 9, 30, 0, tzinfo=UTC),
    )

    captured_statements = []

    class CapturingDb:
        async def execute(self, statement):
            captured_statements.append(statement)
            return _MockScalarResult([])

    await _format_application_response(app, CapturingDb())

    assert len(captured_statements) == 1
    compiled = str(
        captured_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "application_automation_events.event_type in (" in compiled
    assert "workday_unit1_completed" in compiled
    assert "workday_unit1_review_required" in compiled
