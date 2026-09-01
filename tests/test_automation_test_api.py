from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

import main as main_module
from api.automation import router as automation_router
from api.automation_test import (
    QueueWorkdayUnit1TestRequest,
    cleanup_workday_unit1_test,
    queue_workday_unit1_test,
    router as automation_test_router,
)
from main import app
from models.database import (
    ApplicationAutomationBatch,
    ApplicationAutomationEvent,
    AutomationWorkerDevice,
    JobApplication,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _QueueDatabase:
    def __init__(
        self,
        *,
        existing_id=None,
        commit_error: SQLAlchemyError | None = None,
    ):
        self.added = []
        self.existing_id = existing_id
        self.commit_error = commit_error
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement):
        del statement
        return _ScalarResult(self.existing_id)

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _CredentialRepository:
    def __init__(self, *, created=True, error=None):
        self.created = created
        self.error = error
        self.calls = []

    async def generate_if_missing(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ({"id": "credential-id"}, self.created)


class _CleanupDatabase:
    def __init__(
        self,
        *,
        application,
        batch,
        worker_device,
        marker_id=True,
        completed_id=None,
    ):
        self.application = application
        self.batch = batch
        self.worker_device = worker_device
        self.marker_id = (
            uuid.UUID("00000000-0000-0000-0000-000000000004")
            if marker_id is True
            else marker_id
        )
        self.completed_id = completed_id
        self.added = []
        self.committed = False
        self.rolled_back = False
        self._execute_count = 0

    async def execute(self, statement):
        del statement
        self._execute_count += 1
        values = {
            1: self.application,
            2: self.marker_id,
            3: self.completed_id,
            4: self.worker_device,
            5: self.application,
        }
        return _ScalarResult(values[self._execute_count])

    async def get(self, model, item_id, *, with_for_update=False):
        assert model is ApplicationAutomationBatch
        assert item_id == self.batch.id
        assert with_for_update is True
        return self.batch

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _cleanup_state():
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    batch_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    application = SimpleNamespace(
        id=application_id,
        user_id=user_id,
        automation_batch_id=batch_id,
        automation_lease_id=None,
        automation_lease_expires_at=None,
        deleted_at=None,
        session_id=None,
        job_title="Workday Stage 1 URL test R12345 abcdef12",
    )
    batch = SimpleNamespace(id=batch_id, user_id=user_id, status="queued")
    worker_device = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000007"),
        user_id=user_id,
        name="Workday Unit 1 Test Worker abcdef12",
        scope="workday_application",
        revoked_at=None,
    )
    return user_id, application, batch, worker_device


def test_url_test_request_accepts_only_url_and_strips_tracking_data() -> None:
    request = QueueWorkdayUnit1TestRequest(
        job_url=(
            "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/"
            "Engineer_R12345?source=test#details"
        )
    )

    assert request.model_dump() == {
        "job_url": (
            "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/Engineer_R12345"
        )
    }


def test_url_test_request_rejects_non_workday_url() -> None:
    with pytest.raises(ValidationError):
        QueueWorkdayUnit1TestRequest(job_url="https://example.com/job/R12345")


@pytest.mark.asyncio
async def test_url_test_atomically_queues_dummy_local_playwright_application() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    database = _QueueDatabase()
    repository = _CredentialRepository()
    http_response = Response()

    response = await queue_workday_unit1_test(
        QueueWorkdayUnit1TestRequest(
            job_url=(
                "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/Engineer_R12345"
            )
        ),
        response=http_response,
        current_user={"id": str(user_id), "email": "owner@example.com"},
        db=database,
        credential_repository=repository,
    )

    batch = next(
        item for item in database.added if isinstance(item, ApplicationAutomationBatch)
    )
    application = next(
        item for item in database.added if isinstance(item, JobApplication)
    )
    event = next(
        item for item in database.added if isinstance(item, ApplicationAutomationEvent)
    )
    device = next(
        item for item in database.added if isinstance(item, AutomationWorkerDevice)
    )
    assert batch.worker_kind == "local_playwright"
    assert application.automation_batch_id == batch.id == response.batch_id
    assert application.id == response.application_id
    assert application.job_description is None
    assert application.external_job_id == "R12345"
    assert event.detail == "workday:test_url_only"
    assert device.user_id == user_id
    assert device.scope == "workday_application"
    assert response.worker_device_id == device.id
    assert response.worker_token.startswith(f"apw_{device.id.hex}_")
    assert response.worker_token not in response.launcher_command
    assert f'-ApplicationId "{application.id}"' in response.launcher_command
    assert "-WorkerToken" not in response.launcher_command
    assert response.portal_scope == "workday:example:jobs"
    assert response.portal_credential_created is True
    assert repository.calls[0]["account_email"] == "owner@example.com"
    assert repository.calls[0]["portal_scope"] == response.portal_scope
    assert http_response.headers["cache-control"] == "no-store, max-age=0"
    assert http_response.headers["pragma"] == "no-cache"
    assert http_response.headers["referrer-policy"] == "no-referrer"
    lifetime = response.worker_token_expires_at - datetime.now(UTC)
    assert 23 * 60 * 60 < lifetime.total_seconds() <= 24 * 60 * 60
    assert database.committed is True
    assert database.rolled_back is False


@pytest.mark.asyncio
async def test_url_test_rolls_back_every_id_when_queue_commit_fails() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    database = _QueueDatabase(commit_error=SQLAlchemyError("commit failed"))
    repository = _CredentialRepository()

    with pytest.raises(HTTPException) as exc_info:
        await queue_workday_unit1_test(
            QueueWorkdayUnit1TestRequest(
                job_url=(
                    "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/"
                    "Engineer_R12345"
                )
            ),
            response=Response(),
            current_user={"id": str(user_id), "email": "owner@example.com"},
            db=database,
            credential_repository=repository,
        )

    assert exc_info.value.status_code == 500
    assert "no application, batch, or worker-device IDs" in str(exc_info.value.detail)
    assert database.rolled_back is True


@pytest.mark.asyncio
async def test_url_test_stops_before_creating_ids_when_vault_is_unavailable() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    database = _QueueDatabase()
    repository = _CredentialRepository(error=RuntimeError("vault unavailable"))

    with pytest.raises(HTTPException) as exc_info:
        await queue_workday_unit1_test(
            QueueWorkdayUnit1TestRequest(
                job_url=(
                    "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/"
                    "Engineer_R12345"
                )
            ),
            response=Response(),
            current_user={"id": str(user_id), "email": "owner@example.com"},
            db=database,
            credential_repository=repository,
        )

    assert exc_info.value.status_code == 503
    assert database.added == []
    assert database.committed is False


@pytest.mark.asyncio
async def test_url_test_refuses_to_mutate_an_existing_active_application() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    existing_id = uuid.UUID("00000000-0000-0000-0000-000000000006")
    database = _QueueDatabase(existing_id=existing_id)
    repository = _CredentialRepository()

    with pytest.raises(HTTPException) as exc_info:
        await queue_workday_unit1_test(
            QueueWorkdayUnit1TestRequest(
                job_url=(
                    "https://example.wd1.myworkdayjobs.com/en-US/jobs/job/"
                    "Engineer_R12345"
                )
            ),
            response=Response(),
            current_user={"id": str(user_id), "email": "owner@example.com"},
            db=database,
            credential_repository=repository,
        )

    assert exc_info.value.status_code == 409
    assert str(existing_id) in str(exc_info.value.detail)
    assert database.added == []
    assert database.committed is False


@pytest.mark.asyncio
async def test_cleanup_soft_deletes_incomplete_url_test_and_cancels_batch() -> None:
    user_id, application, batch, worker_device = _cleanup_state()
    database = _CleanupDatabase(
        application=application,
        batch=batch,
        worker_device=worker_device,
    )

    response = await cleanup_workday_unit1_test(
        application.id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response.application_deleted is True
    assert response.batch_status == "cancelled"
    assert application.deleted_at is not None
    assert application.deleted_at.tzinfo == UTC
    assert batch.status == "cancelled"
    assert worker_device.revoked_at is not None
    assert response.worker_device_id == worker_device.id
    assert response.worker_device_revoked is True
    assert database.added[0].event_type == "workday_unit1_test_cleaned"
    assert database.committed is True


@pytest.mark.asyncio
async def test_cleanup_refuses_completed_stage1_test() -> None:
    user_id, application, batch, worker_device = _cleanup_state()
    completed_id = uuid.UUID("00000000-0000-0000-0000-000000000005")
    database = _CleanupDatabase(
        application=application,
        batch=batch,
        worker_device=worker_device,
        completed_id=completed_id,
    )

    with pytest.raises(HTTPException) as exc_info:
        await cleanup_workday_unit1_test(
            application.id,
            current_user={"id": str(user_id)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "A completed Stage 1 test is not stale."
    assert database.committed is False


@pytest.mark.asyncio
async def test_cleanup_refuses_application_not_created_by_url_test() -> None:
    user_id, application, batch, worker_device = _cleanup_state()
    database = _CleanupDatabase(
        application=application,
        batch=batch,
        worker_device=worker_device,
        marker_id=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await cleanup_workday_unit1_test(
            application.id,
            current_user={"id": str(user_id)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Only URL-test applications can be cleaned here."
    assert database.committed is False


def test_retry_launcher_reads_only_exact_test_handoff_token_label() -> None:
    script = Path("scripts/run_workday_retry.ps1").read_text(encoding="utf-8")
    worker = Path("scripts/run_workday_account_gate.py").read_text(encoding="utf-8")

    assert "[string]$WorkerToken" not in script
    assert ".tmp\\notes.txt" not in script
    assert "Read-TestHandoffToken" in script
    assert "application token" in script
    assert "apw_[0-9a-f]{32}_[A-Za-z0-9_-]{40,64}" in script
    assert "application under test" in script
    assert worker.index('load_dotenv(PROJECT_ROOT / ".env")') < worker.index(
        "from config.settings import get_settings"
    )


def test_openapi_test_routes_and_retry_are_grouped_and_collapsed() -> None:
    test_paths = {route.path for route in automation_test_router.routes}
    assert test_paths == {
        "/test/workday-unit1",
        "/test/workday-unit1/{application_id}",
    }
    assert all("Test" in route.tags for route in automation_test_router.routes)

    retry_route = next(
        route
        for route in automation_router.routes
        if route.path == "/applications/{application_id}/retry-latest-review"
    )
    assert "Test" in retry_route.tags
    assert app.swagger_ui_parameters["docExpansion"] == "none"


def test_debug_openapi_exposes_url_test_and_retry_in_test_group(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_module.settings, "debug", True)
    debug_app = FastAPI()
    main_module.include_routers(debug_app)
    schema = debug_app.openapi()

    queue_operation = schema["paths"]["/api/v1/automation/test/workday-unit1"]["post"]
    retry_operation = schema["paths"][
        "/api/v1/automation/applications/{application_id}/retry-latest-review"
    ]["post"]
    assert queue_operation["tags"] == ["Test"]
    assert "Test" in retry_operation["tags"]
    test_tag = next(tag for tag in app.openapi_tags if tag["name"] == "Test")
    assert "foreground PowerShell" in test_tag["description"]
