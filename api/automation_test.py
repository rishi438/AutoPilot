"""Debug-only, owner-scoped helpers for manual Workday Unit 1 checks."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.automation import (
    WORKDAY_EXTERNAL_JOB_ID_SUFFIX,
    WORKDAY_HTTPS_URL_PATTERN,
)
from config.settings import get_settings
from models.database import (
    ApplicationAutomationBatch,
    ApplicationAutomationEvent,
    ApplicationStatus,
    AutomationWorkerDevice,
    JobApplication,
)
from services.application_soft_delete import (
    ApplicationSoftDeleteNotFoundError,
    soft_delete_owned_application_in_transaction,
)
from services.portal_account_automation import derive_workday_portal_scope
from services.portal_credentials import (
    PortalCredentialError,
    PortalCredentialRepository,
)
from utils.auth import get_current_user_with_complete_profile
from utils.database import get_database
from utils.portal_vault_database import get_portal_vault_collections
from utils.worker_auth import WORKDAY_APPLICATION_SCOPE, issue_worker_token

router = APIRouter(tags=["Test"])
logger = logging.getLogger(__name__)

_TEST_EVENT_DETAIL = "workday:test_url_only"


def _user_id(current_user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(current_user.get("id") or current_user.get("_id")))


def _portal_credential_repository() -> PortalCredentialRepository:
    settings = get_settings()
    if (
        not settings.portal_vault_enabled
        or settings.portal_vault_encryption_key is None
        or settings.portal_vault_password_recovery_key is None
    ):
        raise HTTPException(503, "Portal credential vault is not configured.")
    try:
        collections = get_portal_vault_collections()
    except RuntimeError as exc:
        raise HTTPException(503, "Portal credential vault is unavailable.") from exc
    return PortalCredentialRepository(
        collections,
        settings.portal_vault_encryption_key.get_secret_value(),
        settings.portal_vault_password_recovery_key.get_secret_value(),
    )


def _disable_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


def _canonical_workday_job_url(value: str) -> str:
    """Return a query-free public Workday job URL accepted by Unit 1."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not re.match(
        WORKDAY_HTTPS_URL_PATTERN,
        f"https://{host}/",
        re.IGNORECASE,
    ):
        raise ValueError("A supported HTTPS Workday job URL is required.")
    if parsed.username or parsed.password:
        raise ValueError("The Workday job URL must not contain credentials.")
    if parsed.port not in {None, 443}:
        raise ValueError("The Workday job URL must use the default HTTPS port.")
    path = parsed.path.rstrip("/")
    if not path or not unquote(path).rsplit("/", 1)[-1].strip():
        raise ValueError("The Workday job URL must identify one job.")
    return urlunsplit(("https", host, path, "", ""))


def _workday_test_external_job_id(job_url: str) -> str:
    path_leaf = unquote(urlsplit(job_url).path).rstrip("/").rsplit("/", 1)[-1]
    match = WORKDAY_EXTERNAL_JOB_ID_SUFFIX.search(path_leaf)
    if match is not None:
        return match.group("external_id")
    digest = sha256(job_url.encode("utf-8")).hexdigest()
    return f"test-{digest[:32]}"


class QueueWorkdayUnit1TestRequest(BaseModel):
    """The URL is the only job metadata a manual test operator supplies."""

    model_config = ConfigDict(extra="forbid")

    job_url: str = Field(min_length=1, max_length=4000)

    @field_validator("job_url")
    @classmethod
    def validate_job_url(cls, value: str) -> str:
        return _canonical_workday_job_url(value)


class QueueWorkdayUnit1TestResponse(BaseModel):
    batch_id: uuid.UUID
    application_id: uuid.UUID
    status: Literal["queued"]
    external_job_id: str
    job_title: str
    company_name: str
    portal_scope: str
    portal_credential_created: bool
    worker_device_id: uuid.UUID
    worker_token: str = Field(
        description="One-time plaintext token; paste it only into the hidden prompt."
    )
    worker_token_expires_at: datetime
    launcher_command: str
    retry_latest_review_path: str
    cleanup_path: str
    revoke_worker_device_path: str


class CleanupWorkdayUnit1TestResponse(BaseModel):
    application_id: uuid.UUID
    batch_id: uuid.UUID | None
    application_deleted: Literal[True]
    batch_status: Literal["cancelled"] | None
    worker_device_id: uuid.UUID | None
    worker_device_revoked: bool


@router.post(
    "/test/workday-unit1",
    response_model=QueueWorkdayUnit1TestResponse,
    status_code=201,
    summary="Queue a URL-only Workday Stage 1 test",
)
async def queue_workday_unit1_test(
    body: QueueWorkdayUnit1TestRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
    credential_repository: PortalCredentialRepository = Depends(
        _portal_credential_repository
    ),
) -> QueueWorkdayUnit1TestResponse:
    """Prepare the tenant vault and queue a local-Playwright Stage 1 test.

    The request accepts only a public Workday job URL. The response supplies the
    generated metadata, a one-time worker token, the visible foreground launcher
    command, and retry/cleanup paths. The reusable tenant credential is prepared
    first; a PostgreSQL failure retains no application, batch, or worker-device
    ID. The token is excluded from the command and the response is non-cacheable.
    """
    _disable_secret_caching(response)
    user_id = _user_id(current_user)
    existing_id = (
        await db.execute(
            select(JobApplication.id).where(
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
                JobApplication.job_url == body.job_url,
            )
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        raise HTTPException(
            409,
            f"An active application already uses this Workday URL: {existing_id}.",
        )

    batch_id = uuid.uuid4()
    application_id = uuid.uuid4()
    run_suffix = uuid.uuid4().hex[:8]
    external_job_id = _workday_test_external_job_id(body.job_url)
    host = urlsplit(body.job_url).hostname or "workday"
    job_title = f"Workday Stage 1 URL test {external_job_id} {run_suffix}"
    company_name = f"Workday URL test - {host.split('.', 1)[0]}"
    portal_scope = derive_workday_portal_scope(body.job_url)
    account_email = str(current_user.get("email") or "").strip().lower()
    if not account_email:
        raise HTTPException(422, "A completed profile email is required.")
    try:
        _, portal_credential_created = await credential_repository.generate_if_missing(
            user_id=str(user_id),
            portal_scope=portal_scope,
            portal_name=company_name,
            portal_login_url=body.job_url,
            account_email=account_email,
        )
    except PortalCredentialError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "workday_url_test_vault_prepare_failed user_id=%s portal_scope=%s",
            user_id,
            portal_scope,
        )
        raise HTTPException(503, "Portal credential vault is unavailable.") from exc

    batch = ApplicationAutomationBatch(
        id=batch_id,
        user_id=user_id,
        worker_kind="local_playwright",
        status="queued",
    )
    application = JobApplication(
        id=application_id,
        user_id=user_id,
        job_title=job_title,
        company_name=company_name,
        job_url=body.job_url,
        portal="workday",
        external_job_id=external_job_id,
        external_ats_url=None,
        job_description=None,
        automation_batch_id=batch_id,
        status=ApplicationStatus.QUEUED.value,
    )
    event = ApplicationAutomationEvent(
        application_id=application_id,
        batch_id=batch_id,
        event_type="job_queued",
        detail=_TEST_EVENT_DETAIL,
    )
    device_id = uuid.uuid4()
    token, token_digest = issue_worker_token(device_id)
    worker_token_expires_at = datetime.now(UTC) + timedelta(days=1)
    device = AutomationWorkerDevice(
        id=device_id,
        user_id=user_id,
        name=f"Workday Unit 1 Test Worker {run_suffix}",
        scope=WORKDAY_APPLICATION_SCOPE,
        token_digest=token_digest,
        expires_at=worker_token_expires_at,
    )

    try:
        db.add(batch)
        db.add(application)
        db.add(event)
        db.add(device)
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.exception(
            "workday_url_test_queue_failed user_id=%s",
            user_id,
        )
        raise HTTPException(
            500,
            (
                "The Workday URL test could not be queued; no application, batch, "
                "or worker-device IDs were retained."
            ),
        ) from exc

    logger.info(
        "workday_url_test_queued application_id=%s batch_id=%s device_id=%s",
        application_id,
        batch_id,
        device_id,
    )
    return QueueWorkdayUnit1TestResponse(
        batch_id=batch_id,
        application_id=application_id,
        status="queued",
        external_job_id=external_job_id,
        job_title=job_title,
        company_name=company_name,
        portal_scope=portal_scope,
        portal_credential_created=portal_credential_created,
        worker_device_id=device_id,
        worker_token=token,
        worker_token_expires_at=worker_token_expires_at,
        launcher_command=(
            f'.\\scripts\\run_workday_retry.ps1 -ApplicationId "{application_id}"'
        ),
        retry_latest_review_path=(
            f"/api/v1/automation/applications/{application_id}/retry-latest-review"
        ),
        cleanup_path=f"/api/v1/automation/test/workday-unit1/{application_id}",
        revoke_worker_device_path=f"/api/v1/automation/worker-devices/{device_id}",
    )


@router.delete(
    "/test/workday-unit1/{application_id}",
    response_model=CleanupWorkdayUnit1TestResponse,
    summary="Clean up a failed URL-only Workday Stage 1 test",
)
async def cleanup_workday_unit1_test(
    application_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
) -> CleanupWorkdayUnit1TestResponse:
    """Soft-delete only an incomplete application created by the URL test route.

    Cleanup cancels the associated test batch, revokes its worker device, and
    preserves audit records. It refuses non-test applications, completed Stage
    1 tests, and active leases.
    """
    user_id = _user_id(current_user)
    application = (
        await db.execute(
            select(JobApplication)
            .where(
                JobApplication.id == application_id,
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Active URL-test application not found.")

    marker_id = (
        await db.execute(
            select(ApplicationAutomationEvent.id)
            .where(
                ApplicationAutomationEvent.application_id == application_id,
                ApplicationAutomationEvent.event_type == "job_queued",
                ApplicationAutomationEvent.detail == _TEST_EVENT_DETAIL,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if marker_id is None:
        raise HTTPException(409, "Only URL-test applications can be cleaned here.")

    completed_id = (
        await db.execute(
            select(ApplicationAutomationEvent.id)
            .where(
                ApplicationAutomationEvent.application_id == application_id,
                ApplicationAutomationEvent.event_type == "workday_unit1_completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if completed_id is not None:
        raise HTTPException(409, "A completed Stage 1 test is not stale.")

    run_suffix = str(application.job_title or "").rsplit(" ", 1)[-1]
    worker_device = (
        await db.execute(
            select(AutomationWorkerDevice)
            .where(
                AutomationWorkerDevice.user_id == user_id,
                AutomationWorkerDevice.name
                == f"Workday Unit 1 Test Worker {run_suffix}",
                AutomationWorkerDevice.scope == WORKDAY_APPLICATION_SCOPE,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if application.automation_lease_id is not None and (
        application.automation_lease_expires_at is None
        or application.automation_lease_expires_at >= now
    ):
        raise HTTPException(409, "The test application still has an active lease.")

    batch_id = application.automation_batch_id
    batch = (
        await db.get(ApplicationAutomationBatch, batch_id, with_for_update=True)
        if batch_id is not None
        else None
    )
    if batch is not None and batch.user_id != user_id:
        raise HTTPException(409, "The test batch ownership does not match.")

    try:
        application.automation_lease_id = None
        application.automation_lease_expires_at = None
        await soft_delete_owned_application_in_transaction(
            db,
            application_id=application_id,
            user_id=user_id,
            now=now,
        )
        if batch is not None:
            batch.status = "cancelled"
        if worker_device is not None and worker_device.revoked_at is None:
            worker_device.revoked_at = now
        db.add(
            ApplicationAutomationEvent(
                application_id=application_id,
                batch_id=batch_id,
                event_type="workday_unit1_test_cleaned",
                detail="incomplete_test",
            )
        )
        await db.commit()
    except ApplicationSoftDeleteNotFoundError as exc:
        await db.rollback()
        raise HTTPException(404, "Active URL-test application not found.") from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.exception(
            "workday_url_test_cleanup_failed application_id=%s",
            application_id,
        )
        raise HTTPException(
            500,
            "The failed test could not be cleaned; its IDs were retained.",
        ) from exc

    logger.info(
        "workday_url_test_cleaned application_id=%s batch_id=%s",
        application_id,
        batch_id,
    )
    return CleanupWorkdayUnit1TestResponse(
        application_id=application_id,
        batch_id=batch_id,
        application_deleted=True,
        batch_status="cancelled" if batch is not None else None,
        worker_device_id=(worker_device.id if worker_device is not None else None),
        worker_device_revoked=worker_device is not None,
    )
