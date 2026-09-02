from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from api.automation import (
    ResolveHoldRequest,
    WorkerUnit2FinalizeRequest,
    rescan_unknown_required_question,
    resolve_hold_with_answer,
    retry_after_relogin,
    worker_finalize_unit2_application,
)
from models.database import (
    ApplicationAutomationEvent,
    ApplicationHold,
    ApplicationStatus,
    JobApplication,
    WorkdayUnit2Attempt,
)


class _MockResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _MockDb:
    def __init__(
        self,
        app: JobApplication,
        attempt: WorkdayUnit2Attempt,
        holds: list[ApplicationHold] | None = None,
        events: list[ApplicationAutomationEvent] | None = None,
    ):
        self.app = app
        self.attempt = attempt
        self.holds = list(holds or [])
        self.events = list(events or [])
        self.committed = False
        self.rolled_back = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, ApplicationAutomationEvent):
            self.events.append(obj)
        elif isinstance(obj, ApplicationHold):
            self.holds.append(obj)

    async def get(self, model: Any, ident: Any) -> Any | None:
        if model is JobApplication and ident == self.app.id:
            return self.app
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def execute(self, stmt: Any) -> _MockResult:
        stmt_str = str(stmt).lower()
        if "from application_holds" in stmt_str:
            if "application_holds.status" in stmt_str:
                return _MockResult(
                    [hold for hold in self.holds if hold.status == "open"]
                )
            return _MockResult(self.holds)
        if "from job_applications" in stmt_str:
            return _MockResult([self.app])
        if "from workday_unit2_attempts" in stmt_str:
            return _MockResult([self.attempt])
        if "select application_automation_events.event_type" in stmt_str:
            return _MockResult([e.event_type for e in self.events])
        if "from application_automation_events" in stmt_str:
            params = set(stmt.compile().params.values())
            matching_ids = [
                event.id for event in self.events if event.event_type in params
            ]
            return _MockResult(matching_ids)
        return _MockResult([])


def _unit1_completed_event(
    application_id: uuid.UUID, now: datetime
) -> ApplicationAutomationEvent:
    return ApplicationAutomationEvent(
        id=uuid.uuid4(),
        application_id=application_id,
        event_type="workday_unit1_completed",
        created_at=now,
    )


@pytest.mark.asyncio
async def test_finalize_complete_success() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.PREPARING.value,
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=15),
        automation_batch_id=uuid.uuid4(),
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="save_claimed",
        mode="normal",
        save_claim_count=1,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(
        app,
        attempt,
        events=[_unit1_completed_event(app_id, now)],
    )
    req = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="complete",
        checkpoint_version="workday_unit2_v1",
    )

    res = await worker_finalize_unit2_application(
        application_id=app_id,
        body=req,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )

    assert res.status == "completed"
    assert attempt.status == "completed"
    assert attempt.terminal_at is not None
    assert app.status == ApplicationStatus.APPLYING.value
    assert app.automation_lease_id is None
    unit2_events = [
        event
        for event in mock_db.events
        if event.event_type == "workday_unit2_completed"
    ]
    assert len(unit2_events) == 1
    assert unit2_events[0].detail == "my_information_saved_next_section_ready"
    assert mock_db.committed is True


@pytest.mark.asyncio
async def test_finalize_review_required_creates_hold() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.PREPARING.value,
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=15),
        automation_batch_id=uuid.uuid4(),
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="leased",
        mode="normal",
        save_claim_count=0,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(
        app,
        attempt,
        events=[_unit1_completed_event(app_id, now)],
    )
    req = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="review_required",
        hold_code="unknown_required_question",
        question="What is your preferred pronouns?",
    )

    res = await worker_finalize_unit2_application(
        application_id=app_id,
        body=req,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )

    assert res.status == "review_required"
    assert attempt.status == "review_required"
    assert app.status == ApplicationStatus.BLOCKED.value
    assert app.automation_lease_id is None
    assert len(mock_db.holds) == 1
    assert mock_db.holds[0].hold_code == "unknown_required_question"
    assert mock_db.holds[0].question == "What is your preferred pronouns?"
    unit2_events = [
        event
        for event in mock_db.events
        if event.event_type == "workday_unit2_review_required"
    ]
    assert len(unit2_events) == 1
    assert unit2_events[0].detail == "unknown_required_question"
    assert mock_db.committed is True


@pytest.mark.asyncio
async def test_finalize_preclick_release_success() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.PREPARING.value,
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=15),
        automation_batch_id=uuid.uuid4(),
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="leased",
        mode="normal",
        save_claim_count=0,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(
        app,
        attempt,
        events=[_unit1_completed_event(app_id, now)],
    )
    req = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="preclick_release",
    )

    res = await worker_finalize_unit2_application(
        application_id=app_id,
        body=req,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )

    assert res.status == "released"
    assert attempt.status == "released"
    assert app.status == ApplicationStatus.APPLYING.value
    assert app.automation_lease_id is None
    assert mock_db.committed is True


@pytest.mark.asyncio
async def test_finalize_preclick_release_fails_if_already_claimed() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.PREPARING.value,
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=15),
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="save_claimed",
        mode="normal",
        save_claim_count=1,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(
        app,
        attempt,
        events=[_unit1_completed_event(app_id, now)],
    )
    req = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="preclick_release",
    )

    with pytest.raises(HTTPException) as exc:
        await worker_finalize_unit2_application(
            application_id=app_id,
            body=req,
            worker_user={"id": str(user_id)},
            db=mock_db,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_finalize_idempotent_replay() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.APPLYING.value,
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="completed",
        mode="normal",
        save_claim_count=1,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(app, attempt)

    # 1. Exact replay returns same success
    req_complete = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="complete",
        checkpoint_version="workday_unit2_v1",
    )
    res = await worker_finalize_unit2_application(
        application_id=app_id,
        body=req_complete,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )
    assert res.status == "completed"

    # 2. Conflicting replay returns 409
    req_review = WorkerUnit2FinalizeRequest(
        attempt_id=attempt_id,
        lease_id=lease_id,
        outcome="review_required",
        hold_code="captcha",
    )
    with pytest.raises(HTTPException) as exc:
        await worker_finalize_unit2_application(
            application_id=app_id,
            body=req_review,
            worker_user={"id": str(user_id)},
            db=mock_db,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_stage2_aware_hold_resolution_emits_retry_ready() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    hold_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.BLOCKED.value,
        external_ats_url="https://wd1.myworkdaysite.com/job/R-1",
        automation_batch_id=uuid.uuid4(),
    )
    hold = ApplicationHold(
        id=hold_id,
        application_id=app_id,
        user_id=user_id,
        portal="workday",
        hold_code="expired_session",
        status="open",
        retry_count=0,
    )
    unit1_event = ApplicationAutomationEvent(
        id=uuid.uuid4(),
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=now,
    )

    mock_db = _MockDb(app, None, holds=[hold], events=[unit1_event])

    res = await retry_after_relogin(
        hold_id=hold_id,
        current_user={"id": str(user_id), "profile_complete": True},
        db=mock_db,
    )

    assert res["application_status"] == ApplicationStatus.APPLYING.value
    assert app.status == ApplicationStatus.APPLYING.value
    latest_event = mock_db.events[-1]
    assert latest_event.event_type == "workday_unit2_retry_ready"
