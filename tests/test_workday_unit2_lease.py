from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from api.automation import (
    LeasedWorkdayUnit2EnvelopeResponse,
    WorkerUnit2StartupReleaseRequest,
    worker_lease_next_unit2_application,
    worker_release_unit2_startup_lease,
)
from models.database import (
    ApplicationAutomationBatch,
    ApplicationAutomationEvent,
    ApplicationHold,
    ApplicationStatus,
    JobApplication,
    WorkdayUnit2Attempt,
)
from services.workday_worker_api import (
    LeasedWorkdayUnit2Application,
    WorkdayWorkerApi,
    WorkdayWorkerTransportError,
)

ATTEMPT_COLUMNS = {
    "id",
    "application_id",
    "lease_id",
    "status",
    "mode",
    "save_claim_count",
    "lease_expires_at",
    "heartbeat_at",
    "terminal_at",
    "created_at",
    "updated_at",
}


def test_workday_unit2_attempt_model_schema() -> None:
    assert set(WorkdayUnit2Attempt.__table__.columns.keys()) == ATTEMPT_COLUMNS
    columns = WorkdayUnit2Attempt.__table__.c

    assert columns["id"].primary_key is True
    assert columns["application_id"].nullable is False
    assert columns["lease_id"].nullable is False
    assert columns["status"].nullable is False
    assert columns["mode"].nullable is False
    assert columns["save_claim_count"].nullable is False
    assert columns["lease_expires_at"].nullable is False
    assert columns["heartbeat_at"].nullable is True
    assert columns["terminal_at"].nullable is True
    assert columns["created_at"].nullable is False
    assert columns["updated_at"].nullable is False

    for col in (
        "lease_expires_at",
        "heartbeat_at",
        "terminal_at",
        "created_at",
        "updated_at",
    ):
        assert columns[col].type.timezone is True

    # Unique constraints and check constraints
    unique_names = {
        c.name
        for c in WorkdayUnit2Attempt.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_workday_unit2_attempt_lease_id" in unique_names

    check_constraints = {
        c.name: str(c.sqltext)
        for c in WorkdayUnit2Attempt.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert "ck_workday_unit2_attempt_save_claim_count" in check_constraints
    assert "ck_workday_unit2_attempt_status" in check_constraints
    assert "ck_workday_unit2_attempt_mode" in check_constraints

    index_names = {idx.name for idx in WorkdayUnit2Attempt.__table__.indexes}
    assert "uq_workday_unit2_attempt_one_active_per_application" in index_names


def _migration_module():
    alembic_package = importlib.import_module("alembic")
    if not hasattr(alembic_package, "op"):
        alembic_package.op = object()
    return importlib.import_module(
        "alembic.versions.20260901_0001_043_add_workday_unit2_attempts"
    )


def test_workday_unit2_migration_schema() -> None:
    migration = _migration_module()
    assert migration.revision == "20260901_043"
    assert migration.down_revision == "20260828_042"


# Mock Database Session for Async FastAPI Handlers
class _MockResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _MockSession:
    def __init__(
        self,
        applications: list[JobApplication] | None = None,
        events: list[ApplicationAutomationEvent] | None = None,
        attempts: list[WorkdayUnit2Attempt] | None = None,
        holds: list[ApplicationHold] | None = None,
    ):
        self.applications: dict[uuid.UUID, JobApplication] = {
            app.id: app for app in (applications or [])
        }
        self.events: list[ApplicationAutomationEvent] = list(events or [])
        self.attempts: list[WorkdayUnit2Attempt] = list(attempts or [])
        self.holds: list[ApplicationHold] = list(holds or [])
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, ApplicationAutomationEvent):
            self.events.append(obj)
        elif isinstance(obj, WorkdayUnit2Attempt):
            self.attempts.append(obj)
        elif isinstance(obj, ApplicationHold):
            self.holds.append(obj)
        elif isinstance(obj, JobApplication):
            self.applications[obj.id] = obj

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def execute(self, stmt: Any) -> _MockResult:
        # Check statement target
        stmt_str = str(stmt).lower()
        if (
            "from job_applications join" in stmt_str
            or stmt_str.startswith("select job_applications.")
            or "from job_applications\nwhere" in stmt_str
        ):
            matching_apps = [
                app for app in self.applications.values() if app.deleted_at is None
            ]
            return _MockResult(matching_apps)
        elif "from workday_unit2_attempts" in stmt_str:
            matching_attempts = list(self.attempts)
            return _MockResult(matching_attempts)
        elif "from application_automation_events" in stmt_str:
            matching_events = list(self.events)
            return _MockResult(matching_events)
        elif "from job_applications" in stmt_str:
            matching_apps = [
                app for app in self.applications.values() if app.deleted_at is None
            ]
            return _MockResult(matching_apps)
        return _MockResult([])


@pytest.mark.asyncio
async def test_worker_lease_next_unit2_success() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/en-US/recruiting/co/job/R-100",
        automation_batch_id=batch_id,
        created_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC),
    )
    batch = ApplicationAutomationBatch(
        id=batch_id,
        user_id=user_id,
        worker_kind="local_playwright",
    )
    event_u1 = ApplicationAutomationEvent(
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=datetime(2026, 9, 1, 10, 5, 0, tzinfo=UTC),
    )

    db = _MockSession(
        applications=[app],
        events=[event_u1],
    )

    response = await worker_lease_next_unit2_application(
        application_id=None,
        worker_user=worker_user,
        db=db,
    )

    assert response.application is not None
    leased = response.application
    assert leased.application_id == app_id
    assert leased.user_id == user_id
    assert leased.portal == "workday"
    assert leased.mode == "normal"
    assert leased.lease_id is not None
    assert leased.attempt_id is not None
    assert leased.lease_expires_at > datetime.now(UTC)

    # State mutations
    assert app.status == ApplicationStatus.PREPARING.value
    assert app.automation_lease_id == leased.lease_id
    assert db.committed is True

    # Attempt and event added
    assert any(
        isinstance(x, WorkdayUnit2Attempt) and x.lease_id == leased.lease_id
        for x in db.added
    )
    assert any(
        isinstance(x, ApplicationAutomationEvent)
        and x.event_type == "workday_unit2_started"
        for x in db.added
    )


@pytest.mark.asyncio
async def test_worker_lease_next_unit2_empty_when_no_candidates() -> None:
    user_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}
    db = _MockSession(applications=[], events=[])

    response = await worker_lease_next_unit2_application(
        application_id=None,
        worker_user=worker_user,
        db=db,
    )
    assert response.application is None


@pytest.mark.asyncio
async def test_worker_lease_next_unit2_recovers_expired_unclaimed_attempt() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.PREPARING.value,
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/1",
        automation_lease_id=uuid.uuid4(),
        automation_lease_expires_at=now - timedelta(minutes=5),
        created_at=now - timedelta(hours=1),
    )
    event_u1 = ApplicationAutomationEvent(
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=now - timedelta(minutes=30),
    )
    old_attempt = WorkdayUnit2Attempt(
        id=uuid.uuid4(),
        application_id=app_id,
        lease_id=app.automation_lease_id,
        status="leased",
        mode="normal",
        save_claim_count=0,
        lease_expires_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=30),
    )

    db = _MockSession(
        applications=[app],
        events=[event_u1],
        attempts=[old_attempt],
    )

    response = await worker_lease_next_unit2_application(
        application_id=None,
        worker_user=worker_user,
        db=db,
    )

    assert response.application is not None
    assert old_attempt.status == "released"
    assert old_attempt.terminal_at is not None


@pytest.mark.asyncio
async def test_worker_lease_next_unit2_attempt_limit_creates_hold_and_blocks() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.APPLYING.value,
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/1",
        created_at=now - timedelta(hours=1),
    )
    event_u1 = ApplicationAutomationEvent(
        application_id=app_id,
        event_type="workday_unit1_completed",
        created_at=now - timedelta(minutes=30),
    )
    # 3 prior released attempts
    prior_attempts = [
        WorkdayUnit2Attempt(
            id=uuid.uuid4(),
            application_id=app_id,
            lease_id=uuid.uuid4(),
            status="released",
            mode="normal",
            save_claim_count=0,
            lease_expires_at=now - timedelta(minutes=20),
            created_at=now - timedelta(minutes=25),
        ),
        WorkdayUnit2Attempt(
            id=uuid.uuid4(),
            application_id=app_id,
            lease_id=uuid.uuid4(),
            status="released",
            mode="normal",
            save_claim_count=0,
            lease_expires_at=now - timedelta(minutes=10),
            created_at=now - timedelta(minutes=15),
        ),
        WorkdayUnit2Attempt(
            id=uuid.uuid4(),
            application_id=app_id,
            lease_id=uuid.uuid4(),
            status="released",
            mode="normal",
            save_claim_count=0,
            lease_expires_at=now - timedelta(minutes=2),
            created_at=now - timedelta(minutes=5),
        ),
    ]

    db = _MockSession(
        applications=[app],
        events=[event_u1],
        attempts=prior_attempts,
    )

    response = await worker_lease_next_unit2_application(
        application_id=None,
        worker_user=worker_user,
        db=db,
    )

    # Limit reached -> empty lease returned, hold created, app blocked
    assert response.application is None
    assert app.status == ApplicationStatus.BLOCKED.value
    assert any(isinstance(x, ApplicationHold) and x.status == "open" for x in db.added)
    assert any(
        isinstance(x, ApplicationAutomationEvent)
        and x.event_type == "workday_unit2_review_required"
        for x in db.added
    )


@pytest.mark.asyncio
async def test_worker_release_unit2_startup_lease_success() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.PREPARING.value,
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/1",
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=10),
    )
    attempt = WorkdayUnit2Attempt(
        id=uuid.uuid4(),
        application_id=app_id,
        lease_id=lease_id,
        status="leased",
        mode="normal",
        save_claim_count=0,
        lease_expires_at=now + timedelta(minutes=10),
    )
    db = _MockSession(applications=[app], attempts=[attempt])

    result = await worker_release_unit2_startup_lease(
        application_id=app_id,
        body=WorkerUnit2StartupReleaseRequest(lease_id=lease_id),
        worker_user=worker_user,
        db=db,
    )

    assert result == {"status": "released"}
    assert app.status == ApplicationStatus.APPLYING.value
    assert app.automation_lease_id is None
    assert attempt.status == "released"
    assert attempt.terminal_at is not None
    assert db.committed is True


@pytest.mark.asyncio
async def test_worker_release_unit2_startup_lease_rejects_claimed_attempt() -> None:
    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    worker_user = {"id": str(user_id)}
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        status=ApplicationStatus.PREPARING.value,
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/1",
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=10),
    )
    attempt = WorkdayUnit2Attempt(
        id=uuid.uuid4(),
        application_id=app_id,
        lease_id=lease_id,
        status="save_claimed",
        mode="normal",
        save_claim_count=1,
        lease_expires_at=now + timedelta(minutes=10),
    )
    db = _MockSession(applications=[app], attempts=[attempt])

    with pytest.raises(HTTPException) as exc_info:
        await worker_release_unit2_startup_lease(
            application_id=app_id,
            body=WorkerUnit2StartupReleaseRequest(lease_id=lease_id),
            worker_user=worker_user,
            db=db,
        )
    assert exc_info.value.status_code == 409
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_workday_worker_api_unit2_transport() -> None:
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    user_id = uuid.uuid4()

    lease_payload = {
        "application": {
            "application_id": str(app_id),
            "attempt_id": str(attempt_id),
            "lease_id": str(lease_id),
            "lease_expires_at": "2026-09-01T12:00:00Z",
            "mode": "normal",
            "user_id": str(user_id),
            "portal": "workday",
            "job_url": "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Engineer_R-1",
            "external_ats_url": None,
            "job_title": "Engineer",
            "company_name": "Wells Fargo",
        }
    }

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/automation/worker/unit2/queue/next":
            return httpx.Response(200, json=lease_payload)
        elif "/startup-release" in request.url.path:
            return httpx.Response(200, json={"status": "released"})
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WorkdayWorkerApi(
        base_url="http://127.0.0.1:8000",
        bearer_token="secret-token",
        client=client,
    )

    lease = await api.lease_next_unit2()
    assert lease is not None
    assert lease.application_id == app_id
    assert lease.attempt_id == attempt_id
    assert lease.lease_id == lease_id
    assert lease.portal == "workday"

    await api.release_startup_lease_unit2(lease)
    assert len(requests) == 2
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/automation/worker/unit2/queue/next"
    assert requests[1].method == "POST"
    assert f"/queue/{app_id}/startup-release" in requests[1].url.path

    await api.close()
