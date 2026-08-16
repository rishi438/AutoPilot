"""Worker-neutral APIs for safe application holds and retries."""

from __future__ import annotations

import uuid
import re
from hashlib import sha256
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    ApplicationAutomationEvent,
    ApplicationAutomationBatch,
    ApplicationHold,
    ApplicationStatus,
    JobApplication,
    JobFormAnswer,
    ApplicationSubmittedAnswer,
)
from services.application_automation import classify_sensitivity, normalize_question
from utils.auth import get_current_user_with_complete_profile
from utils.database import get_database

router = APIRouter()
MAX_HOLD_RETRIES = 3
AUTOMATION_LEASE_MINUTES = 10
PORTAL_LOGIN_URLS = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "instahyre": "https://www.instahyre.com/login/",
    "hirist": "https://www.hirist.com/login",
    "foundit": "https://www.foundit.in/seeker/login",
}


def _user_id(user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(user.get("id") or user.get("_id")))


class CreateHoldRequest(BaseModel):
    application_id: uuid.UUID
    hold_code: Literal[
        "unknown_required_question",
        "captcha",
        "otp",
        "expired_session",
        "unsupported_step",
        "validation_failure",
        "upload_failure",
        "unknown_page_state",
    ]
    remediation: str = Field(min_length=1, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)
    error_detail: str | None = Field(default=None, max_length=2000)
    portal: str | None = Field(default=None, max_length=50)


class ResolveHoldRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=5000)
    field_type: str | None = Field(default=None, max_length=50)
    approved_for_reuse: bool = False


class CreateBatchRequest(BaseModel):
    worker_kind: Literal["extension", "local_playwright"]


class QueueJobRequest(BaseModel):
    """Safe metadata only; portal credentials and cookies are rejected."""

    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    portal: str = Field(min_length=1, max_length=50)
    external_job_id: str = Field(min_length=1, max_length=255)
    job_title: str = Field(min_length=1, max_length=500)
    company_name: str | None = Field(default=None, max_length=500)
    job_url: str = Field(min_length=1, max_length=4000)
    external_ats_url: str | None = Field(default=None, max_length=4000)
    job_description: str | None = Field(default=None, max_length=50000)

    @field_validator("job_url", "external_ats_url")
    @classmethod
    def require_https_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("Only HTTPS job URLs are accepted.")
        return value

    @field_validator("job_description")
    @classmethod
    def clean_job_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


class SubmittedAnswerRequest(BaseModel):
    """One answer actually submitted to a portal, with its safe provenance."""

    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    answer_source: Literal["profile", "approved_rule", "ai", "manual", "unknown"]
    changed_from_previous: bool = False

    @model_validator(mode="after")
    def reject_browser_secret_material(self) -> "SubmittedAnswerRequest":
        secret_markers = r"password|passcode|cookie|session|token|authorization|otp"
        if re.search(secret_markers, self.question, re.IGNORECASE):
            raise ValueError(
                "Browser credentials and session material cannot be recorded."
            )
        return self


class RecordResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    result: Literal["applied", "failed", "skipped", "retrying"]
    confirmation_evidence: str | None = Field(default=None, max_length=1000)
    submitted_answers: list[SubmittedAnswerRequest] = Field(
        default_factory=list, max_length=100
    )


def _hold_response(
    hold: ApplicationHold, application: JobApplication | None = None
) -> dict[str, Any]:
    payload = {
        "id": str(hold.id),
        "application_id": str(hold.application_id),
        "portal": hold.portal,
        "hold_code": hold.hold_code,
        "question": hold.question,
        "remediation": hold.remediation,
        "retry_count": hold.retry_count,
        "status": hold.status,
        "created_at": hold.created_at,
    }
    if application is not None:
        payload["job_title"] = application.job_title
        payload["company_name"] = application.company_name
        if hold.hold_code == "expired_session":
            payload["relogin_url"] = PORTAL_LOGIN_URLS.get(
                (hold.portal or "").lower(),
                application.external_ats_url or application.job_url,
            )
    return payload


@router.get("/holds")
async def list_holds(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Return only the caller's unresolved application blockers."""
    rows = (
        await db.execute(
            select(ApplicationHold, JobApplication)
            .join(JobApplication, JobApplication.id == ApplicationHold.application_id)
            .where(
                ApplicationHold.user_id == _user_id(current_user),
                ApplicationHold.status == "open",
            )
            .order_by(ApplicationHold.created_at.desc())
        )
    ).all()
    return [_hold_response(hold, application) for hold, application in rows]


@router.post("/holds", status_code=201)
async def create_hold(
    body: CreateHoldRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Create one safe blocker and pause only its application."""
    user_id = _user_id(current_user)
    application = (
        await db.execute(
            select(JobApplication).where(
                JobApplication.id == body.application_id,
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found.")
    existing = (
        await db.execute(
            select(ApplicationHold.id).where(
                ApplicationHold.application_id == application.id,
                ApplicationHold.status == "open",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "Application already has an open hold.")

    hold = ApplicationHold(
        user_id=user_id,
        application_id=application.id,
        portal=body.portal or application.portal,
        question=body.question,
        normalized_question=(
            normalize_question(body.question) if body.question else None
        ),
        hold_code=body.hold_code,
        remediation=body.remediation,
        error_detail=body.error_detail,
    )
    application.status = ApplicationStatus.BLOCKED.value
    db.add(hold)
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="hold_created",
            detail=body.hold_code,
        )
    )
    await db.commit()
    await db.refresh(hold)
    return _hold_response(hold)


@router.post("/holds/{hold_id}/answer")
async def resolve_hold_with_answer(
    hold_id: uuid.UUID,
    body: ResolveHoldRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Persist a user response, resolve its hold, and enqueue one bounded retry."""
    user_id = _user_id(current_user)
    hold = (
        await db.execute(
            select(ApplicationHold).where(
                ApplicationHold.id == hold_id,
                ApplicationHold.user_id == user_id,
                ApplicationHold.status == "open",
            )
        )
    ).scalar_one_or_none()
    if hold is None:
        raise HTTPException(404, "Open application hold not found.")
    if not hold.question or not hold.normalized_question:
        raise HTTPException(400, "This hold cannot be resolved with a reusable answer.")

    application = await db.get(JobApplication, hold.application_id)
    if application is None or application.user_id != user_id:
        raise HTTPException(404, "Application not found.")
    hold.retry_count += 1
    hold.status = "resolved"
    hold.resolved_at = datetime.now(UTC)
    answer = JobFormAnswer(
        user_id=user_id,
        question=hold.question,
        answer=body.answer,
        normalized_question=hold.normalized_question,
        field_type=body.field_type,
        sensitivity=classify_sensitivity(hold.question),
        approved_for_reuse=body.approved_for_reuse,
        source_portal=hold.portal,
    )
    if hold.retry_count <= MAX_HOLD_RETRIES:
        application.status = ApplicationStatus.RETRYING.value
        event_type = "retry_enqueued"
    else:
        application.status = ApplicationStatus.FAILED.value
        event_type = "retry_exhausted"
    db.add(answer)
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type=event_type,
            detail=hold.hold_code,
        )
    )
    await db.commit()
    return {
        "hold_id": str(hold.id),
        "application_id": str(application.id),
        "application_status": application.status,
        "retry_count": hold.retry_count,
    }


@router.post("/holds/{hold_id}/retry")
async def retry_after_relogin(
    hold_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Retry one job after the user confirms the portal session was renewed."""
    user_id = _user_id(current_user)
    hold = (
        await db.execute(
            select(ApplicationHold).where(
                ApplicationHold.id == hold_id,
                ApplicationHold.user_id == user_id,
                ApplicationHold.status == "open",
                ApplicationHold.hold_code == "expired_session",
            )
        )
    ).scalar_one_or_none()
    if hold is None:
        raise HTTPException(404, "Open expired-session hold not found.")
    application = await db.get(JobApplication, hold.application_id)
    if application is None or application.user_id != user_id:
        raise HTTPException(404, "Application not found.")
    hold.retry_count += 1
    if hold.retry_count > MAX_HOLD_RETRIES:
        hold.status = "exhausted"
        application.status = ApplicationStatus.FAILED.value
        event_type = "retry_exhausted"
    else:
        hold.status = "resolved"
        hold.resolved_at = datetime.now(UTC)
        application.status = ApplicationStatus.RETRYING.value
        event_type = "retry_enqueued_after_relogin"
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type=event_type,
            detail=hold.portal,
        )
    )
    await db.commit()
    return {
        "hold_id": str(hold.id),
        "application_id": str(application.id),
        "application_status": application.status,
        "retry_count": hold.retry_count,
    }


@router.post("/batches", status_code=201)
async def create_batch(
    body: CreateBatchRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Create a credential-free batch for one supported worker type."""
    batch = ApplicationAutomationBatch(
        user_id=_user_id(current_user), worker_kind=body.worker_kind
    )
    db.add(batch)
    await db.flush()
    await db.commit()
    return {
        "id": str(batch.id),
        "worker_kind": batch.worker_kind,
        "status": batch.status,
    }


@router.post("/queue/jobs", status_code=201)
async def queue_job(
    body: QueueJobRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Sync one deduplicated portal job using safe metadata only."""
    user_id = _user_id(current_user)
    description_hash = (
        sha256(body.job_description.encode("utf-8")).hexdigest()
        if body.job_description
        else None
    )
    title_company_match = JobApplication.job_title == body.job_title
    title_company_match &= (
        JobApplication.company_name == body.company_name
        if body.company_name is not None
        else JobApplication.company_name.is_(None)
    )
    batch = await db.get(ApplicationAutomationBatch, body.batch_id)
    if batch is None or batch.user_id != user_id:
        raise HTTPException(404, "Automation batch not found.")
    application = (
        await db.execute(
            select(JobApplication).where(
                JobApplication.user_id == user_id,
                or_(
                    (JobApplication.portal == body.portal)
                    & (JobApplication.external_job_id == body.external_job_id),
                    JobApplication.job_url == body.job_url,
                    title_company_match,
                ),
            )
        )
    ).scalar_one_or_none()
    created = application is None
    restored = False
    if application is None:
        application = JobApplication(
            user_id=user_id,
            job_title=body.job_title,
            company_name=body.company_name,
            job_url=body.job_url,
            portal=body.portal,
            external_job_id=body.external_job_id,
            external_ats_url=body.external_ats_url,
            job_description=body.job_description,
            job_description_hash=description_hash,
            job_description_captured_at=(
                datetime.now(UTC) if description_hash else None
            ),
            automation_batch_id=batch.id,
            status=ApplicationStatus.QUEUED.value,
        )
        db.add(application)
        await db.flush()
    else:
        # A later explicit browser save may have more accurate portal metadata
        # than the original card (for example, a company name discovered after
        # the page's About Company panel loads). Keep the existing application
        # identity but refresh the user-supplied snapshot metadata.
        application.job_title = body.job_title
        application.company_name = body.company_name
        application.job_url = body.job_url
        application.portal = body.portal
        application.external_job_id = body.external_job_id
        application.automation_batch_id = batch.id
        application.external_ats_url = (
            body.external_ats_url or application.external_ats_url
        )
        if description_hash and description_hash != application.job_description_hash:
            application.job_description = body.job_description
            application.job_description_hash = description_hash
            application.job_description_captured_at = datetime.now(UTC)
        if application.deleted_at is not None:
            # Saving the same portal job again is an explicit user action. Restore
            # the prior soft-deleted card so the new queue state is visible.
            application.deleted_at = None
            application.status = ApplicationStatus.QUEUED.value
            restored = True
        elif application.status in {
            ApplicationStatus.DISCOVERED.value,
            ApplicationStatus.SKIPPED.value,
        }:
            application.status = ApplicationStatus.QUEUED.value
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=batch.id,
            event_type="job_restored" if restored else "job_queued",
            detail=(f"{body.portal}:jd_snapshot" if description_hash else body.portal),
        )
    )
    await db.commit()
    return {
        "id": str(application.id),
        "status": application.status,
        "created": created,
        "restored": restored,
    }


@router.get("/queue/next")
async def lease_next_application(
    worker_kind: Literal["extension", "local_playwright"] = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Atomically lease one eligible application; expired leases become eligible again."""
    now = datetime.now(UTC)
    application = (
        await db.execute(
            select(JobApplication)
            .join(ApplicationAutomationBatch)
            .where(
                JobApplication.user_id == _user_id(current_user),
                ApplicationAutomationBatch.worker_kind == worker_kind,
                or_(
                    JobApplication.status.in_(
                        [
                            ApplicationStatus.QUEUED.value,
                            ApplicationStatus.RETRYING.value,
                        ]
                    ),
                    (JobApplication.status == ApplicationStatus.PREPARING.value)
                    & (JobApplication.automation_lease_expires_at < now),
                ),
            )
            .order_by(JobApplication.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
    ).scalar_one_or_none()
    if application is None:
        return {"application": None}
    lease_id = uuid.uuid4()
    application.status = ApplicationStatus.PREPARING.value
    application.automation_lease_id = lease_id
    application.automation_lease_expires_at = now + timedelta(
        minutes=AUTOMATION_LEASE_MINUTES
    )
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="application_leased",
            detail=worker_kind,
        )
    )
    await db.commit()
    return {
        "application": {
            "id": str(application.id),
            "lease_id": str(lease_id),
            "portal": application.portal,
            "job_url": application.job_url,
            "external_ats_url": application.external_ats_url,
            "job_title": application.job_title,
            "company_name": application.company_name,
        }
    }


@router.post("/queue/{application_id}/result")
async def record_application_result(
    application_id: uuid.UUID,
    body: RecordResultRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Accept an outcome only from the active worker lease for this application."""
    application = await db.get(JobApplication, application_id)
    if application is None or application.user_id != _user_id(current_user):
        raise HTTPException(404, "Application not found.")
    if (
        application.automation_lease_id != body.lease_id
        or application.automation_lease_expires_at is None
        or application.automation_lease_expires_at < datetime.now(UTC)
    ):
        raise HTTPException(409, "Application lease is invalid or expired.")
    application.status = body.result
    application.automation_lease_id = None
    application.automation_lease_expires_at = None
    if body.result == ApplicationStatus.APPLIED.value:
        application.applied_date = datetime.now(UTC)
    for submitted_answer in body.submitted_answers:
        review_reasons: list[str] = []
        if submitted_answer.answer_source == "unknown":
            review_reasons.append("unknown_source")
        if submitted_answer.answer_source == "ai":
            review_reasons.append("ai_generated")
        if submitted_answer.changed_from_previous:
            review_reasons.append("changed_from_previous")
        db.add(
            ApplicationSubmittedAnswer(
                application_id=application.id,
                question=submitted_answer.question,
                answer=submitted_answer.answer,
                answer_source=submitted_answer.answer_source,
                review_reasons=review_reasons,
            )
        )
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type=f"application_{body.result}",
            detail=body.confirmation_evidence,
        )
    )
    await db.commit()
    return {"id": str(application.id), "status": application.status}
