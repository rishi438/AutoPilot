"""Worker-neutral APIs for safe application holds and retries."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from api.extension_autofill import (
    AutofillFieldIn,
    AutofillMapRequest,
    AutofillMapResponse,
    map_form_fields_from_approved_sources,
)

from models.database import (
    AutomationWorkerDevice,
    ApplicationAutomationEvent,
    ApplicationAutomationBatch,
    ApplicationHold,
    ApplicationStatus,
    JobApplication,
    JobFormAnswer,
    ApplicationSubmittedAnswer,
    ApplicationDraftAnswer,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
)
from services.application_automation import (
    classify_sensitivity,
    is_prohibited_answer_material,
    normalize_question,
    protect_reusable_answer,
)
from services.portal_account_automation import (
    NativeAccountPageState,
    derive_workday_portal_scope,
)
from services.portal_credentials import (
    PortalAccountMetadataRepository,
    PortalCredentialError,
)
from services.workday_account_gate_store import (
    create_workday_account_gate_store,
    SQLAlchemyWorkdayAccountGateStore,
    WorkdayGateAcquireRequest,
    WorkdayGateDecision,
    WorkdayGateLease,
    WorkdayGateStoreError,
)
from services.workday_cooldown_notices import (
    SQLAlchemyWorkdayCooldownNoticeStore,
    WorkdayCooldownDecision,
    WorkdayCooldownNoticeConflictError,
    WorkdayCooldownNoticeNotFoundError,
    WorkdayCooldownNoticeView,
)
from utils.auth import get_current_user_with_complete_profile
from utils.cache import invalidate_workflow_state
from utils.database import get_database
from utils.portal_vault_database import get_portal_vault_collections
from utils.worker_auth import (
    WORKDAY_ACCOUNT_GATE_SCOPE,
    WORKDAY_APPLICATION_SCOPE,
    get_workday_application_worker_user,
    get_workday_worker_user,
    issue_worker_token,
)

router = APIRouter()
logger = logging.getLogger(__name__)
MAX_HOLD_RETRIES = 3
AUTOMATION_LEASE_MINUTES = 10
WORKDAY_GATE_CANDIDATE_LIMIT = 25
WORKDAY_HTTPS_URL_PATTERN = (
    r"^https://(?:[a-z0-9-]+\.)*(?:myworkdayjobs|myworkdaysite)\.com(?:[/:?#]|$)"
)
WORKDAY_EXTERNAL_JOB_ID_SUFFIX = re.compile(
    r"_(?P<external_id>[a-z][a-z0-9-]*\d[a-z0-9-]*)$", re.IGNORECASE
)
PORTAL_LOGIN_URLS = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "instahyre": "https://www.instahyre.com/login/",
    "hirist": "https://www.hirist.com/login",
    "foundit": "https://www.foundit.in/seeker/login",
}


def _user_id(user: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(str(user.get("id") or user.get("_id")))


class CreateHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: uuid.UUID
    lease_id: uuid.UUID | None = None
    hold_code: Literal[
        "unknown_required_question",
        "captcha",
        "otp",
        "expired_session",
        "native_credentials_required",
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


class CreateWorkerDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    expires_in_days: int = Field(default=30, ge=1, le=90)
    scope: Literal["workday_account_gate", "workday_application"] = (
        WORKDAY_ACCOUNT_GATE_SCOPE
    )


class WorkerAutofillMapRequest(BaseModel):
    """Lease-bound field descriptors; browser values are never logged."""

    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    fields: list[AutofillFieldIn] = Field(min_length=1, max_length=80)
    page_url: str = Field(min_length=1, max_length=2048)


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

    @model_validator(mode="after")
    def require_matching_workday_job_id(self) -> "QueueJobRequest":
        if not re.match(WORKDAY_HTTPS_URL_PATTERN, self.job_url, re.IGNORECASE):
            return self
        path_leaf = unquote(urlsplit(self.job_url).path).rstrip("/").rsplit("/", 1)[-1]
        match = WORKDAY_EXTERNAL_JOB_ID_SUFFIX.search(path_leaf)
        if (
            match is not None
            and match.group("external_id").casefold() != self.external_job_id.casefold()
        ):
            raise ValueError("external_job_id must match the Workday job URL.")
        return self


def _merge_external_ats_url(
    *, job_url: str, requested_url: str | None, existing_url: str | None
) -> str | None:
    """Do not retain an older ATS handoff when the saved URL is already Workday."""
    if re.match(WORKDAY_HTTPS_URL_PATTERN, job_url, re.IGNORECASE):
        return requested_url
    return requested_url or existing_url


def _application_answer_hostname(application: JobApplication) -> str | None:
    """Return the exact HTTPS hostname that scopes reusable portal answers."""
    for value in (application.external_ats_url, application.job_url):
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme.lower() == "https" and parsed.hostname:
            return parsed.hostname.lower()
    return None


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


class AccountStateRequest(BaseModel):
    """Credential-free Workday account state observed by the active worker."""

    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    page_state: NativeAccountPageState


class ReleaseWorkdayStartupLeaseRequest(BaseModel):
    """Server-issued authorities required to release a pre-action startup lease."""

    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    gate_id: uuid.UUID
    gate_generation: int = Field(ge=0)
    gate_lease_token: str = Field(min_length=1, max_length=64)


class ReleaseWorkdayCooldownLeaseRequest(BaseModel):
    """Release a worker lease without creating an immediate retry."""

    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    gate_id: uuid.UUID
    gate_generation: int = Field(ge=0)
    gate_lease_token: str = Field(min_length=1, max_length=64)
    reason: Literal["start_or_refresh_cooldown", "defer"]


class CompleteWorkdayUnit1Request(BaseModel):
    """Complete or atomically review one guarded Unit 1 application lease."""

    model_config = ConfigDict(extra="forbid")

    lease_id: uuid.UUID
    gate_id: uuid.UUID
    gate_generation: int = Field(ge=0)
    gate_lease_token: str = Field(min_length=1, max_length=64)
    authentication_submitted: bool
    outcome: Literal["complete", "review"] = "complete"
    hold_code: Literal[
        "unknown_page_state", "native_credentials_required", "captcha", "otp"
    ] = "unknown_page_state"


class WorkdayCooldownDecisionRequest(BaseModel):
    """One terminal user choice; no choice exposes a retry operation."""

    model_config = ConfigDict(extra="forbid")

    decision: WorkdayCooldownDecision
    extend_until: datetime | None = None
    confirm_delete: bool = False

    @model_validator(mode="after")
    def validate_choice(self) -> "WorkdayCooldownDecisionRequest":
        if self.decision is WorkdayCooldownDecision.EXTEND:
            if self.extend_until is None:
                raise ValueError("Extend requires a future UTC time.")
        elif self.extend_until is not None:
            raise ValueError("An extension time is valid only for extend.")
        if self.decision is WorkdayCooldownDecision.DELETE:
            if not self.confirm_delete:
                raise ValueError("Delete requires explicit confirmation.")
        elif self.confirm_delete:
            raise ValueError("Delete confirmation is valid only for delete.")
        return self


class DraftAnswerRequest(BaseModel):
    """A reviewed, application-scoped answer proposal; never browser state."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    answer_source: Literal["profile", "approved_rule", "ai", "manual"]
    review_reasons: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("review_reasons")
    @classmethod
    def validate_review_reasons(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 80:
                raise ValueError(
                    "Review reasons must be non-empty and at most 80 characters."
                )
        return values

    @model_validator(mode="after")
    def reject_browser_secret_material(self) -> "DraftAnswerRequest":
        secret_markers = r"password|passcode|cookie|session(?:\s+value)?|token|authorization\s+header|otp"
        if re.search(secret_markers, f"{self.question} {self.answer}", re.IGNORECASE):
            raise ValueError(
                "Browser credentials and session material cannot be recorded."
            )
        return self


class DraftAnswersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[DraftAnswerRequest] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_questions(self) -> "DraftAnswersRequest":
        questions = [answer.question.strip().casefold() for answer in self.answers]
        if len(questions) != len(set(questions)):
            raise ValueError("Each draft question may appear only once.")
        return self


class SaveReusableAnswerRequest(BaseModel):
    """A user-confirmed dynamic answer that may be reused on matching forms."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    field_type: str | None = Field(default=None, max_length=50)
    source_portal: str | None = Field(default=None, max_length=50)

    @field_validator("question", "answer")
    @classmethod
    def require_meaningful_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Question and answer must not be blank.")
        return cleaned

    @field_validator("field_type", "source_portal")
    @classmethod
    def normalize_optional_context(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,48})", cleaned):
            raise ValueError("Portal context must be a hostname.")
        return cleaned or None

    @model_validator(mode="after")
    def reject_unsafe_content(self) -> "SaveReusableAnswerRequest":
        value = f"{self.question} {self.answer}"
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("Answers may not contain control characters.")
        if is_prohibited_answer_material(value):
            raise ValueError(
                "Credentials, session data, and payment data cannot be saved."
            )
        digits_only = re.sub(r"\D", "", self.answer)
        if 13 <= len(digits_only) <= 19:
            raise ValueError(
                "Credentials, session data, and payment data cannot be saved."
            )
        return self


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


def _cooldown_notice_response(notice: WorkdayCooldownNoticeView) -> dict[str, Any]:
    """Expose only the application binding and safe server-UTC wait."""
    return {
        "id": str(notice.id),
        "application_id": str(notice.application_id),
        "status": notice.status,
        "message": "The Workday account is temporarily locked.",
        "safe_next_attempt_at": notice.safe_next_attempt_at,
        "created_at": notice.created_at,
    }


@router.get("/cooldown-notices")
async def list_workday_cooldown_notices(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """List only pending cooldown choices owned by the authenticated user."""
    notices = await SQLAlchemyWorkdayCooldownNoticeStore(db).list_pending(
        user_id=_user_id(current_user)
    )
    return [_cooldown_notice_response(notice) for notice in notices]


@router.post("/cooldown-notices/{notice_id}/decision")
async def decide_workday_cooldown_notice(
    notice_id: uuid.UUID,
    body: WorkdayCooldownDecisionRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Keep, lengthen, or delete one application without scheduling a retry."""
    try:
        result = await SQLAlchemyWorkdayCooldownNoticeStore(db).decide(
            user_id=_user_id(current_user),
            notice_id=notice_id,
            decision=body.decision,
            extend_until=body.extend_until,
            confirm_delete=body.confirm_delete,
        )
    except WorkdayCooldownNoticeNotFoundError as exc:
        raise HTTPException(404, "Cooldown notice not found.") from exc
    except WorkdayCooldownNoticeConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if result.invalidated_session_id:
        await invalidate_workflow_state(result.invalidated_session_id)
        from api.workflow import cancel_local_workflow_task

        cancel_local_workflow_task(result.invalidated_session_id)
    return _cooldown_notice_response(result.notice)


def _has_active_lease(
    application: JobApplication, lease_id: uuid.UUID, *, now: datetime
) -> bool:
    return bool(
        application.automation_lease_id == lease_id
        and application.automation_lease_expires_at is not None
        and application.automation_lease_expires_at >= now
    )


@router.post("/worker-devices", status_code=201)
async def create_worker_device(
    body: CreateWorkerDeviceRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Create one expiring device token; plaintext is returned exactly once."""
    now = datetime.now(UTC)
    device_id = uuid.uuid4()
    token, token_digest = issue_worker_token(device_id)
    device = AutomationWorkerDevice(
        id=device_id,
        user_id=_user_id(current_user),
        name=body.name.strip(),
        scope=body.scope,
        token_digest=token_digest,
        expires_at=now + timedelta(days=body.expires_in_days),
    )
    db.add(device)
    await db.commit()
    logger.info(
        "automation_worker_device_created device_id=%s user_id=%s scope=%s expires_at=%s",
        device.id,
        device.user_id,
        device.scope,
        device.expires_at,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "id": str(device.id),
        "name": device.name,
        "scope": device.scope,
        "expires_at": device.expires_at,
        "token": token,
    }


@router.get("/worker-devices")
async def list_worker_devices(
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """List device metadata without exposing stored token digests."""
    devices = list(
        (
            await db.execute(
                select(AutomationWorkerDevice)
                .where(AutomationWorkerDevice.user_id == _user_id(current_user))
                .order_by(AutomationWorkerDevice.created_at.desc())
            )
        ).scalars()
    )
    return [
        {
            "id": str(device.id),
            "name": device.name,
            "scope": device.scope,
            "expires_at": device.expires_at,
            "revoked_at": device.revoked_at,
            "created_at": device.created_at,
        }
        for device in devices
    ]


@router.delete("/worker-devices/{device_id}")
async def revoke_worker_device(
    device_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Revoke one owned worker device immediately and idempotently."""
    device = (
        await db.execute(
            select(AutomationWorkerDevice).where(
                AutomationWorkerDevice.id == device_id,
                AutomationWorkerDevice.user_id == _user_id(current_user),
            )
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(404, "Worker device not found.")
    if device.revoked_at is None:
        device.revoked_at = datetime.now(UTC)
        await db.commit()
    return {"id": str(device.id), "revoked": True}


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
    if body.lease_id is not None and not _has_active_lease(
        application, body.lease_id, now=datetime.now(UTC)
    ):
        raise HTTPException(409, "Application lease is invalid or expired.")
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
    if body.lease_id is not None:
        application.automation_lease_id = None
        application.automation_lease_expires_at = None
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
    logger.warning(
        "automation_hold_created hold_id=%s application_id=%s hold_code=%s portal=%s",
        hold.id,
        application.id,
        hold.hold_code,
        hold.portal,
    )
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
    if is_prohibited_answer_material(f"{hold.question} {body.answer}"):
        raise HTTPException(
            422, "Credentials, session data, and payment data cannot be saved."
        )

    application = await db.get(JobApplication, hold.application_id)
    if application is None or application.user_id != user_id:
        raise HTTPException(404, "Application not found.")
    answer_hostname = _application_answer_hostname(application)
    if answer_hostname is None:
        raise HTTPException(422, "Application has no valid HTTPS answer scope.")
    hold.retry_count += 1
    hold.status = "resolved"
    hold.resolved_at = datetime.now(UTC)
    answer_plaintext, answer_encrypted = protect_reusable_answer(body.answer)
    answer = JobFormAnswer(
        user_id=user_id,
        question=hold.question,
        answer=answer_plaintext,
        answer_encrypted=answer_encrypted,
        normalized_question=hold.normalized_question,
        field_type=body.field_type,
        sensitivity=classify_sensitivity(hold.question),
        approved_for_reuse=body.approved_for_reuse,
        source_portal=answer_hostname,
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


@router.post("/holds/{hold_id}/rescan")
async def rescan_unknown_required_question(
    hold_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Requeue one recoverable form hold for a bounded rescan."""
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
    if hold.hold_code not in {"unknown_required_question", "validation_failure"}:
        raise HTTPException(
            409,
            "Only an unknown-required-question or validation-failure hold can be rescanned.",
        )

    application = await db.get(JobApplication, hold.application_id)
    if application is None or application.user_id != user_id:
        raise HTTPException(404, "Application not found.")
    if (
        application.status != ApplicationStatus.BLOCKED.value
        or application.automation_lease_id is not None
        or application.automation_lease_expires_at is not None
    ):
        raise HTTPException(409, "The application is not safely blocked and unleased.")

    hold.retry_count += 1
    if hold.retry_count > MAX_HOLD_RETRIES:
        hold.status = "exhausted"
        application.status = ApplicationStatus.FAILED.value
        event_type = "retry_exhausted"
    else:
        hold.status = "resolved"
        hold.resolved_at = datetime.now(UTC)
        application.status = ApplicationStatus.RETRYING.value
        event_type = "retry_enqueued_for_form_rescan"
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


@router.post(
    "/applications/{application_id}/retry-latest-review",
    summary="[Test] Retry latest review hold",
)
async def retry_latest_review_hold(
    application_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Retry the newest review hold for one owned application deterministically."""
    user_id = _user_id(current_user)
    hold = (
        await db.execute(
            select(ApplicationHold)
            .where(
                ApplicationHold.application_id == application_id,
                ApplicationHold.user_id == user_id,
                ApplicationHold.status.in_({"open", "resolved"}),
                ApplicationHold.hold_code.in_(
                    {"unknown_page_state", "unsupported_step"}
                ),
            )
            .order_by(ApplicationHold.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if hold is None:
        raise HTTPException(404, "No review hold found for this application.")
    return await retry_review_hold(hold.id, current_user, db)


@router.post("/holds/{hold_id}/retry-review")
async def retry_review_hold(
    hold_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Retry one questionless review hold after the user acknowledges the blocker."""
    user_id = _user_id(current_user)
    hold = (
        await db.execute(
            select(ApplicationHold).where(
                ApplicationHold.id == hold_id,
                ApplicationHold.user_id == user_id,
                ApplicationHold.status.in_({"open", "resolved"}),
                ApplicationHold.hold_code.in_(
                    {"unknown_page_state", "unsupported_step"}
                ),
            )
        )
    ).scalar_one_or_none()
    if hold is None:
        raise HTTPException(404, "Open review hold not found.")
    application = await db.get(JobApplication, hold.application_id)
    if application is None or application.user_id != user_id:
        raise HTTPException(404, "Application not found.")
    if hold.status == "open" and (
        application.status != ApplicationStatus.BLOCKED.value
        or application.automation_lease_id is not None
        or application.automation_lease_expires_at is not None
    ):
        raise HTTPException(409, "The application is not safely blocked and unleased.")

    review_attempt = (
        await db.execute(
            select(WorkdayAuthAttempt)
            .join(
                WorkdayAccountGate, WorkdayAccountGate.id == WorkdayAuthAttempt.gate_id
            )
            .where(
                WorkdayAuthAttempt.application_id == application.id,
                WorkdayAuthAttempt.status == "review_required",
                WorkdayAccountGate.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if review_attempt is None:
        raise HTTPException(
            409, "No review-required Workday gate attempt is available."
        )
    gate = await db.get(
        WorkdayAccountGate, review_attempt.gate_id, with_for_update=True
    )
    if gate is None or gate.state != "review_required":
        raise HTTPException(409, "The Workday gate is not awaiting review.")
    review_attempt.status = "abandoned"
    gate.state = "open"
    gate.generation += 1

    if hold.status == "open":
        hold.retry_count += 1
        if hold.retry_count > MAX_HOLD_RETRIES:
            hold.status = "exhausted"
            application.status = ApplicationStatus.FAILED.value
            event_type = "retry_exhausted"
        else:
            hold.status = "resolved"
            hold.resolved_at = datetime.now(UTC)
            application.status = ApplicationStatus.RETRYING.value
            event_type = "retry_enqueued_after_review"
    else:
        application.status = ApplicationStatus.RETRYING.value
        event_type = "retry_reopened_after_review"
    application.automation_lease_id = None
    application.automation_lease_expires_at = None
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
    logger.info(
        "automation_batch_created batch_id=%s user_id=%s worker_kind=%s",
        batch.id,
        batch.user_id,
        batch.worker_kind,
    )
    return {
        "id": str(batch.id),
        "worker_kind": batch.worker_kind,
        "status": batch.status,
    }


@router.put("/answer-library")
async def save_reusable_answer(
    body: SaveReusableAnswerRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Upsert one user-confirmed, inert reusable answer record."""
    user_id = _user_id(current_user)
    normalized_question = normalize_question(body.question)
    if not normalized_question:
        raise HTTPException(422, "Question must contain letters or numbers.")
    existing = (
        (
            await db.execute(
                select(JobFormAnswer)
                .where(
                    JobFormAnswer.user_id == user_id,
                    JobFormAnswer.normalized_question == normalized_question,
                    JobFormAnswer.source_portal == body.source_portal,
                )
                .order_by(JobFormAnswer.updated_at.desc())
            )
        )
        .scalars()
        .first()
    )
    answer_plaintext, answer_encrypted = protect_reusable_answer(body.answer)
    if existing is None:
        existing = JobFormAnswer(
            user_id=user_id,
            question=body.question.strip(),
            normalized_question=normalized_question,
            answer=answer_plaintext,
            answer_encrypted=answer_encrypted,
            field_type=body.field_type,
            sensitivity=classify_sensitivity(body.question),
            approved_for_reuse=True,
            source_portal=body.source_portal,
        )
        db.add(existing)
    else:
        existing.question = body.question.strip()
        existing.answer = answer_plaintext
        existing.answer_encrypted = answer_encrypted
        existing.field_type = body.field_type
        existing.sensitivity = classify_sensitivity(body.question)
        existing.approved_for_reuse = True
        existing.last_used_at = datetime.now(UTC)
    await db.commit()
    return {
        "id": str(existing.id),
        "normalized_question": normalized_question,
        "approved_for_reuse": True,
    }


@router.put("/applications/{application_id}/draft-answers")
async def save_reviewed_draft_answers(
    application_id: uuid.UUID,
    body: DraftAnswersRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Replace the reviewed answer draft for one owned application."""
    user_id = _user_id(current_user)
    application = (
        await db.execute(
            select(JobApplication).where(
                JobApplication.id == application_id,
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found.")

    existing = list(
        (
            await db.execute(
                select(ApplicationDraftAnswer).where(
                    ApplicationDraftAnswer.application_id == application_id,
                    ApplicationDraftAnswer.user_id == user_id,
                )
            )
        ).scalars()
    )
    by_question = {answer.question.casefold(): answer for answer in existing}
    retained: set[str] = set()
    for item in body.answers:
        key = item.question.strip().casefold()
        retained.add(key)
        draft = by_question.get(key)
        if draft is None:
            draft = ApplicationDraftAnswer(
                user_id=user_id,
                application_id=application_id,
                question=item.question.strip(),
            )
            db.add(draft)
        draft.answer = item.answer
        draft.answer_source = item.answer_source
        draft.review_reasons = list(item.review_reasons)
    for draft in existing:
        if draft.question.casefold() not in retained:
            await db.delete(draft)
    db.add(
        ApplicationAutomationEvent(
            application_id=application_id,
            batch_id=application.automation_batch_id,
            event_type="form_answers_drafted",
            detail=f"count={len(body.answers)}",
        )
    )
    await db.commit()
    return {
        "application_id": str(application_id),
        "draft_answers": [
            {
                "question": item.question.strip(),
                "answer": item.answer,
                "answer_source": item.answer_source,
                "review_reasons": list(item.review_reasons),
            }
            for item in body.answers
        ],
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
        application.external_ats_url = _merge_external_ats_url(
            job_url=body.job_url,
            requested_url=body.external_ats_url,
            existing_url=application.external_ats_url,
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
    logger.info(
        "automation_job_queued application_id=%s batch_id=%s portal=%s external_job_id=%s created=%s restored=%s",
        application.id,
        batch.id,
        application.portal,
        application.external_job_id,
        created,
        restored,
    )
    return {
        "id": str(application.id),
        "status": application.status,
        "created": created,
        "restored": restored,
    }


async def _resolve_workday_account_ref(
    *, user_id: uuid.UUID, portal_scope: str
) -> uuid.UUID | None:
    """Read only the owned vault record's opaque identifier."""
    try:
        collections = get_portal_vault_collections()
        return await PortalAccountMetadataRepository(
            collections.credentials
        ).resolve_account_ref(
            user_id=str(user_id),
            portal_scope=portal_scope,
        )
    except (PortalCredentialError, RuntimeError):
        logger.warning(
            "workday_gate_account_metadata_unavailable portal_scope=%s",
            portal_scope,
        )
        return None


@router.get("/queue/next")
async def lease_next_application(
    worker_kind: Literal["extension", "local_playwright"] = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
    application_id: uuid.UUID | None = None,
):
    """Lease one eligible application, with a private gate for Workday."""
    now = datetime.now(UTC)
    user_id = _user_id(current_user)
    eligibility_filters = [
        JobApplication.user_id == user_id,
        JobApplication.deleted_at.is_(None),
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
    ]
    if worker_kind == "local_playwright":
        target_url = func.coalesce(
            JobApplication.external_ats_url, JobApplication.job_url, ""
        )
        eligibility_filters.append(target_url.op("~*")(WORKDAY_HTTPS_URL_PATTERN))
        attempt_reclaimable = or_(
            WorkdayAuthAttempt.id.is_(None),
            WorkdayAuthAttempt.lease_expires_at <= now,
        )
        eligibility_filters.append(
            or_(
                JobApplication.workday_account_gate_id.is_(None),
                and_(WorkdayAccountGate.state == "open", attempt_reclaimable),
                and_(
                    WorkdayAccountGate.state == "probe_in_progress",
                    attempt_reclaimable,
                ),
                and_(
                    WorkdayAccountGate.state == "auth_outcome_pending",
                    WorkdayAuthAttempt.application_id == JobApplication.id,
                ),
                and_(
                    WorkdayAccountGate.state == "cooling_down",
                    or_(
                        WorkdayAccountGate.cooldown_until.is_(None),
                        WorkdayAccountGate.cooldown_until <= now,
                    ),
                    or_(
                        WorkdayAccountGate.user_min_until.is_(None),
                        WorkdayAccountGate.user_min_until <= now,
                    ),
                    or_(
                        WorkdayAccountGate.next_eligible_at.is_(None),
                        WorkdayAccountGate.next_eligible_at <= now,
                    ),
                    attempt_reclaimable,
                ),
            )
        )
    if application_id is not None:
        eligibility_filters.append(JobApplication.id == application_id)

    excluded_application_ids: list[uuid.UUID] = []
    gate_acquisition = None
    application = None
    candidate_limit = 1 if application_id is not None else WORKDAY_GATE_CANDIDATE_LIMIT
    try:
        for _ in range(candidate_limit):
            candidate_filters = list(eligibility_filters)
            if excluded_application_ids:
                candidate_filters.append(
                    JobApplication.id.notin_(excluded_application_ids)
                )
            statement = select(JobApplication).join(ApplicationAutomationBatch)
            if worker_kind == "local_playwright":
                statement = statement.outerjoin(
                    WorkdayAccountGate,
                    WorkdayAccountGate.id == JobApplication.workday_account_gate_id,
                ).outerjoin(
                    WorkdayAuthAttempt,
                    and_(
                        WorkdayAuthAttempt.gate_id == WorkdayAccountGate.id,
                        WorkdayAuthAttempt.status == "active",
                    ),
                )
            application = (
                await db.execute(
                    statement.where(*candidate_filters)
                    .order_by(JobApplication.created_at)
                    .with_for_update(of=JobApplication, skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if application is None:
                break
            if worker_kind != "local_playwright":
                break

            target = application.external_ats_url or application.job_url
            try:
                portal_scope = derive_workday_portal_scope(target or "")
            except PortalCredentialError:
                excluded_application_ids.append(application.id)
                application = None
                continue
            account_ref = await _resolve_workday_account_ref(
                user_id=user_id,
                portal_scope=portal_scope,
            )
            if account_ref is None:
                excluded_application_ids.append(application.id)
                application = None
                continue
            gate_acquisition = await create_workday_account_gate_store(
                db,
                lock_cooldown_hours=(
                    get_settings().workday_account_lock_cooldown_hours
                ),
                store_factory=SQLAlchemyWorkdayAccountGateStore,
            ).acquire_in_transaction(
                WorkdayGateAcquireRequest(
                    user_id=user_id,
                    application_id=application.id,
                    account_ref=account_ref,
                    portal_scope=portal_scope,
                )
            )
            if gate_acquisition.decision is WorkdayGateDecision.DEFER:
                excluded_application_ids.append(application.id)
                application = None
                gate_acquisition = None
                continue
            break

        if application is None:
            if excluded_application_ids:
                await db.commit()
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
    except WorkdayGateStoreError:
        await db.rollback()
        logger.warning("workday_gate_lease_rejected application_id=%s", application_id)
        return {"application": None}
    except Exception:
        await db.rollback()
        raise

    logger.info(
        "automation_application_leased application_id=%s batch_id=%s worker_kind=%s",
        application.id,
        application.automation_batch_id,
        worker_kind,
    )
    payload = {
        "id": str(application.id),
        "lease_id": str(lease_id),
        "portal": application.portal,
        "job_url": application.job_url,
        "external_ats_url": application.external_ats_url,
        "job_title": application.job_title,
        "company_name": application.company_name,
        "user_id": str(user_id),
    }
    if gate_acquisition is not None:
        payload.update(
            {
                "gate_id": str(gate_acquisition.gate_id),
                "gate_generation": gate_acquisition.generation,
                "gate_lease_token": gate_acquisition.lease_token,
                "gate_decision": gate_acquisition.decision.value,
                "gate_lease_expires_at": gate_acquisition.lease_expires_at,
                "gate_next_eligible_at": gate_acquisition.next_eligible_at,
            }
        )
    else:
        payload["account_email"] = current_user.get("email")
    return {"application": payload}


@router.post("/queue/{application_id}/reset-lease")
async def reset_application_lease(
    application_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Invalidate one owned preparing lease and enqueue an immediate retry."""
    application = await db.get(JobApplication, application_id)
    if (
        application is None
        or application.user_id != _user_id(current_user)
        or application.deleted_at is not None
    ):
        raise HTTPException(404, "Application not found.")

    lease_is_already_reset = (
        application.status == ApplicationStatus.RETRYING.value
        and application.automation_lease_id is None
        and application.automation_lease_expires_at is None
    )
    if lease_is_already_reset:
        return {
            "id": str(application.id),
            "status": application.status,
            "reset": False,
        }
    if application.status != ApplicationStatus.PREPARING.value:
        raise HTTPException(409, "Only a preparing application lease can be reset.")

    application.status = ApplicationStatus.RETRYING.value
    application.automation_lease_id = None
    application.automation_lease_expires_at = None
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="application_lease_reset",
            detail="user_requested",
        )
    )
    await db.commit()
    return {
        "id": str(application.id),
        "status": application.status,
        "reset": True,
    }


@router.post("/queue/{application_id}/account-state", status_code=202)
async def record_account_state(
    application_id: uuid.UUID,
    body: AccountStateRequest,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
):
    """Record one allowlisted account state and renew only its active lease."""
    application = await db.get(JobApplication, application_id)
    if application is None or application.user_id != _user_id(current_user):
        raise HTTPException(404, "Application not found.")
    now = datetime.now(UTC)
    if not _has_active_lease(application, body.lease_id, now=now):
        raise HTTPException(409, "Application lease is invalid or expired.")
    application.automation_lease_expires_at = now + timedelta(
        minutes=AUTOMATION_LEASE_MINUTES
    )
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="account_state_observed",
            detail=body.page_state.value,
        )
    )
    await db.commit()
    logger.info(
        "automation_account_state_recorded application_id=%s page_state=%s",
        application.id,
        body.page_state.value,
    )
    return {
        "application_id": str(application.id),
        "page_state": body.page_state.value,
        "lease_expires_at": application.automation_lease_expires_at,
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
    if not _has_active_lease(application, body.lease_id, now=datetime.now(UTC)):
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
    logger.info(
        "automation_application_result_recorded application_id=%s result=%s submitted_answer_count=%s",
        application.id,
        body.result,
        len(body.submitted_answers),
    )
    return {"id": str(application.id), "status": application.status}


@router.get("/worker/queue/next")
async def worker_lease_next_application(
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
    application_id: uuid.UUID | None = None,
):
    """Lease one Workday job using only a scoped worker-device credential."""
    return await lease_next_application(
        worker_kind="local_playwright",
        current_user=worker_user,
        db=db,
        application_id=application_id,
    )


@router.get("/worker/unit1/queue/next")
async def worker_lease_next_unit1_application(
    worker_user: dict[str, Any] = Depends(get_workday_application_worker_user),
    db: AsyncSession = Depends(get_database),
    application_id: uuid.UUID | None = None,
):
    """Lease Unit 1 only through an application-scope worker device."""
    return await lease_next_application(
        worker_kind="local_playwright",
        current_user=worker_user,
        db=db,
        application_id=application_id,
    )


@router.post(
    "/worker/unit1/applications/{application_id}/retry-latest-review",
    summary="Retry latest review hold with an application worker device",
)
async def worker_retry_latest_review_hold(
    application_id: uuid.UUID,
    worker_user: dict[str, Any] = Depends(get_workday_application_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Reopen one owned Unit 1 review hold using the scoped worker credential."""
    recovery_error: HTTPException | None = None
    try:
        return await retry_latest_review_hold(
            application_id,
            current_user=worker_user,
            db=db,
        )
    except HTTPException as exc:
        missing_review_hold = exc.status_code == 404 and exc.detail == (
            "No review hold found for this application."
        )
        already_recovered_review = exc.status_code == 409 and exc.detail in {
            "No review-required Workday gate attempt is available.",
            "The Workday gate is not awaiting review.",
        }
        if not missing_review_hold and not already_recovered_review:
            raise
        recovery_error = exc
    application = (
        await db.execute(
            select(JobApplication).where(
                JobApplication.id == application_id,
                JobApplication.user_id == _user_id(worker_user),
                JobApplication.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(404, "Application not found.")
    if recovery_error is not None and recovery_error.status_code == 409:
        gate = (
            await db.get(WorkdayAccountGate, application.workday_account_gate_id)
            if application.workday_account_gate_id is not None
            else None
        )
        if (
            application.status != ApplicationStatus.RETRYING.value
            or application.automation_lease_id is not None
            or application.automation_lease_expires_at is not None
            or gate is None
            or gate.user_id != _user_id(worker_user)
            or gate.state != "open"
        ):
            raise recovery_error
    return {
        "application_status": application.status,
        "retry_status": (
            "already_ready"
            if recovery_error is not None and recovery_error.status_code == 409
            else "not_needed"
        ),
    }


@router.post("/worker/queue/{application_id}/startup-release", status_code=202)
async def worker_release_startup_lease(
    application_id: uuid.UUID,
    body: ReleaseWorkdayStartupLeaseRequest,
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Atomically release server-issued gate/application startup authorities."""
    user_id = _user_id(worker_user)
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
        await db.rollback()
        raise HTTPException(404, "Application not found.")
    if not _has_active_lease(application, body.lease_id, now=datetime.now(UTC)):
        await db.rollback()
        raise HTTPException(409, "Application lease is invalid or expired.")
    if application.workday_account_gate_id != body.gate_id:
        await db.rollback()
        raise HTTPException(409, "Workday gate authority does not match.")

    mutation = await create_workday_account_gate_store(
        db,
        lock_cooldown_hours=get_settings().workday_account_lock_cooldown_hours,
        store_factory=SQLAlchemyWorkdayAccountGateStore,
    ).release_unsubmitted_in_transaction(
        WorkdayGateLease(
            gate_id=body.gate_id,
            application_id=application.id,
            generation=body.gate_generation,
            lease_token=body.gate_lease_token,
        )
    )
    if not mutation.applied:
        await db.rollback()
        raise HTTPException(409, "Workday gate startup lease cannot be released.")

    application.status = ApplicationStatus.RETRYING.value
    application.automation_lease_id = None
    application.automation_lease_expires_at = None
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="application_startup_lease_released",
            detail="local_playwright",
        )
    )
    await db.commit()
    logger.info(
        "workday_startup_leases_released application_id=%s gate_id=%s",
        application.id,
        body.gate_id,
    )
    return {"application_id": str(application.id), "status": application.status}


@router.post("/worker/queue/{application_id}/cooldown-release", status_code=202)
async def worker_release_cooldown_lease(
    application_id: uuid.UUID,
    body: ReleaseWorkdayCooldownLeaseRequest,
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Restore a queueable status while a private future gate blocks leasing."""
    user_id = _user_id(worker_user)
    now = datetime.now(UTC)
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
        await db.rollback()
        raise HTTPException(404, "Application not found.")
    if not _has_active_lease(application, body.lease_id, now=now):
        await db.rollback()
        raise HTTPException(409, "Application lease is invalid or expired.")
    if application.workday_account_gate_id != body.gate_id:
        await db.rollback()
        raise HTTPException(409, "Workday gate authority does not match.")

    gate = (
        await db.execute(
            select(WorkdayAccountGate)
            .where(
                WorkdayAccountGate.id == body.gate_id,
                WorkdayAccountGate.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    attempt = (
        await db.execute(
            select(WorkdayAuthAttempt)
            .where(
                WorkdayAuthAttempt.gate_id == body.gate_id,
                WorkdayAuthAttempt.application_id == application.id,
                WorkdayAuthAttempt.generation == body.gate_generation,
                WorkdayAuthAttempt.lease_token == body.gate_lease_token,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if gate is None or attempt is None:
        await db.rollback()
        raise HTTPException(409, "Workday gate authority is invalid.")

    if body.reason == "defer":
        authority_valid = (
            attempt.status == "active"
            and attempt.auth_submit_count == 0
            and gate.generation == body.gate_generation
            and attempt.lease_expires_at is not None
            and attempt.lease_expires_at > now
        )
    else:
        # Lock confirmation completes the submitted attempt and advances the
        # gate generation before this lifecycle release is requested.
        authority_valid = (
            attempt.status == "completed"
            and attempt.auth_submit_count == 1
            and gate.generation > body.gate_generation
        )
    wait_until = (
        max(
            value.astimezone(UTC)
            for value in (
                gate.cooldown_until,
                gate.user_min_until,
                gate.next_eligible_at,
            )
            if value is not None
        )
        if any(
            value is not None
            for value in (
                gate.cooldown_until,
                gate.user_min_until,
                gate.next_eligible_at,
            )
        )
        else None
    )
    if (
        not authority_valid
        or gate.state != "cooling_down"
        or wait_until is None
        or wait_until <= now
    ):
        await db.rollback()
        raise HTTPException(409, "The Workday cooldown/defer authority is stale.")

    # There is intentionally no public cooling-down application status.
    # RETRYING becomes leaseable only after the private gate's future wait
    # predicates pass in lease_next_application().
    application.status = ApplicationStatus.RETRYING.value
    application.automation_lease_id = None
    application.automation_lease_expires_at = None
    db.add(
        ApplicationAutomationEvent(
            application_id=application.id,
            batch_id=application.automation_batch_id,
            event_type="application_cooldown_deferred",
            detail=body.reason,
        )
    )
    await db.commit()
    return {
        "application_id": str(application.id),
        "status": application.status,
        "next_eligible_at": wait_until,
    }


@router.post("/worker/holds", status_code=201)
async def worker_create_hold(
    body: CreateHoldRequest,
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Create a leased hold using only a scoped worker-device credential."""
    if body.lease_id is None:
        raise HTTPException(422, "A worker hold requires an active lease.")
    return await create_hold(body=body, current_user=worker_user, db=db)


@router.post("/worker/queue/{application_id}/account-state", status_code=202)
async def worker_record_account_state(
    application_id: uuid.UUID,
    body: AccountStateRequest,
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Record an allowlisted account state from a scoped worker device."""
    return await record_account_state(
        application_id=application_id,
        body=body,
        current_user=worker_user,
        db=db,
    )


@router.post(
    "/worker/queue/{application_id}/autofill/map",
    response_model=AutofillMapResponse,
)
async def worker_map_approved_form_fields(
    application_id: uuid.UUID,
    body: WorkerAutofillMapRequest,
    worker_user: dict[str, Any] = Depends(get_workday_application_worker_user),
    db: AsyncSession = Depends(get_database),
) -> AutofillMapResponse:
    """Return only profile-backed or explicitly approved reusable answers."""
    user_id = _user_id(worker_user)
    application = await db.get(JobApplication, application_id)
    if (
        application is None
        or application.user_id != user_id
        or application.deleted_at is not None
    ):
        raise HTTPException(404, "Application not found.")
    if not _has_active_lease(application, body.lease_id, now=datetime.now(UTC)):
        raise HTTPException(409, "Application lease is invalid or expired.")
    if not re.match(WORKDAY_HTTPS_URL_PATTERN, body.page_url, re.IGNORECASE):
        raise HTTPException(422, "Only an HTTPS Workday application page is accepted.")
    result = await map_form_fields_from_approved_sources(
        AutofillMapRequest(
            fields=body.fields,
            page_url=body.page_url,
            application_id=application_id,
        ),
        user_id=user_id,
        db=db,
    )
    logger.info(
        "worker_autofill_mapping_completed application_id=%s field_count=%s assignment_count=%s",
        application_id,
        len(body.fields),
        len(result.assignments),
    )
    return result


@router.post("/worker/queue/{application_id}/unit1-complete", status_code=202)
async def worker_complete_unit1(
    application_id: uuid.UUID,
    body: CompleteWorkdayUnit1Request,
    worker_user: dict[str, Any] = Depends(get_workday_application_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Finalize Unit 1 only after one guarded gate/application transaction."""
    user_id = _user_id(worker_user)
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
        await db.rollback()
        raise HTTPException(404, "Application not found.")
    if not _has_active_lease(application, body.lease_id, now=datetime.now(UTC)):
        await db.rollback()
        raise HTTPException(409, "Application lease is invalid or expired.")

    if application.workday_account_gate_id != body.gate_id:
        await db.rollback()
        raise HTTPException(409, "Workday gate authority does not match.")

    gate_lease = WorkdayGateLease(
        gate_id=body.gate_id,
        application_id=application.id,
        generation=body.gate_generation,
        lease_token=body.gate_lease_token,
    )
    gate_store = create_workday_account_gate_store(
        db,
        lock_cooldown_hours=get_settings().workday_account_lock_cooldown_hours,
        store_factory=SQLAlchemyWorkdayAccountGateStore,
    )
    if body.outcome == "review":
        existing_hold = (
            await db.execute(
                select(ApplicationHold.id).where(
                    ApplicationHold.application_id == application.id,
                    ApplicationHold.status == "open",
                )
            )
        ).scalar_one_or_none()
        if existing_hold is not None:
            await db.rollback()
            raise HTTPException(409, "Application already has an open hold.")
        mutation = await gate_store.mark_review_required_in_transaction(
            gate_lease,
            authentication_submitted=body.authentication_submitted,
        )
        if not mutation.applied:
            await db.rollback()
            raise HTTPException(409, "Workday gate finalization is stale.")
        application.status = ApplicationStatus.BLOCKED.value
        application.automation_lease_id = None
        application.automation_lease_expires_at = None
        db.add(
            ApplicationHold(
                user_id=user_id,
                application_id=application.id,
                portal=application.portal,
                hold_code=body.hold_code,
                remediation=(
                    "Review the Workday page and gate outcome before retrying."
                ),
            )
        )
        db.add(
            ApplicationAutomationEvent(
                application_id=application.id,
                batch_id=application.automation_batch_id,
                event_type="workday_unit1_review_required",
                detail=(
                    "checkpoint_failed_after_submit"
                    if body.authentication_submitted
                    else "checkpoint_failed"
                ),
            )
        )
    else:
        mutation = await gate_store.complete_success_in_transaction(
            gate_lease,
            authentication_submitted=body.authentication_submitted,
        )
        if not mutation.applied:
            await db.rollback()
            raise HTTPException(409, "Workday gate finalization is stale.")
        application.status = ApplicationStatus.APPLYING.value
        application.automation_lease_id = None
        application.automation_lease_expires_at = None
        db.add(
            ApplicationAutomationEvent(
                application_id=application.id,
                batch_id=application.automation_batch_id,
                event_type="workday_unit1_completed",
                detail=(
                    "authenticated_application_ready_submitted"
                    if body.authentication_submitted
                    else "authenticated_application_ready_existing_session"
                ),
            )
        )
    await db.commit()
    return {
        "application_id": str(application.id),
        "status": application.status,
        "outcome": body.outcome,
    }


@router.post("/worker/queue/{application_id}/result")
async def worker_record_application_result(
    application_id: uuid.UUID,
    body: RecordResultRequest,
    worker_user: dict[str, Any] = Depends(get_workday_worker_user),
    db: AsyncSession = Depends(get_database),
):
    """Release one active lease from a scoped worker device."""
    allowed_results = {
        ApplicationStatus.RETRYING.value,
        ApplicationStatus.SKIPPED.value,
    }
    valid_skip_evidence = (
        body.result != ApplicationStatus.SKIPPED.value
        or body.confirmation_evidence == "workday_job_unavailable"
    )
    if (
        body.result not in allowed_results
        or body.submitted_answers
        or not valid_skip_evidence
    ):
        raise HTTPException(
            422,
            "The account-gate worker may only retry or skip a portal-confirmed "
            "unavailable job without answers.",
        )
    return await record_application_result(
        application_id=application_id,
        body=body,
        current_user=worker_user,
        db=db,
    )
