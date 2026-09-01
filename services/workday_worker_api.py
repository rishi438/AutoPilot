"""Credential-redacted backend transport for the local Workday worker."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from services.portal_account_automation import NativeAccountPageState
from services.workday_playwright_worker import (
    WorkdayFormAssignment,
    WorkdayFormField,
    WorkdayLease,
    WorkdayStateEmitter,
    WorkdayWorkerError,
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
logger = logging.getLogger(__name__)


class WorkdayWorkerTransportError(RuntimeError):
    """Sanitized local-worker transport failure without request credentials."""


class _LeasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    lease_id: uuid.UUID
    portal: str = Field(min_length=1, max_length=50)
    job_url: str = Field(min_length=1, max_length=4000)
    external_ats_url: str | None = Field(default=None, max_length=4000)
    job_title: str | None = Field(default=None, max_length=500)
    company_name: str | None = Field(default=None, max_length=500)
    user_id: uuid.UUID
    gate_id: uuid.UUID
    gate_generation: int = Field(ge=0)
    gate_lease_token: str = Field(min_length=1, max_length=64)
    gate_decision: str = Field(pattern=r"^(allow|one_probe|observe_only)$")
    gate_lease_expires_at: datetime | None = None
    gate_next_eligible_at: datetime | None = None


class _LeaseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application: _LeasePayload | None


class _AutofillAssignmentPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field_uid: str = Field(pattern=r"^\d+$")
    value: str = Field(max_length=8000)
    answer_source: str
    review_reasons: list[str] = Field(default_factory=list)


class _AutofillEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assignments: list[_AutofillAssignmentPayload] = Field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class LeasedWorkdayApplication:
    """Validated safe metadata plus the backend lease capability."""

    application_id: uuid.UUID
    lease_id: uuid.UUID
    user_id: uuid.UUID
    portal: str
    job_url: str
    external_ats_url: str | None
    job_title: str | None
    company_name: str | None
    gate_id: uuid.UUID
    gate_generation: int
    gate_lease_token: str = field(repr=False)
    gate_decision: str
    gate_lease_expires_at: datetime | None
    gate_next_eligible_at: datetime | None

    def to_workday_lease(self) -> WorkdayLease:
        return WorkdayLease(
            application_id=str(self.application_id),
            user_id=str(self.user_id),
            portal=self.portal,
            job_url=self.job_url,
            external_ats_url=self.external_ats_url,
        )


def _validate_api_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise WorkdayWorkerTransportError("A valid worker API base URL is required.")
    if parsed.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise WorkdayWorkerTransportError(
            "Worker API HTTP is allowed only on the loopback interface."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkdayWorkerTransportError(
            "Worker API credentials and query parameters are not allowed in the URL."
        )
    return normalized


class WorkdayWorkerApi:
    """Call worker-neutral APIs without persisting or exposing the bearer token."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15.0,
    ):
        if not bearer_token.strip():
            raise WorkdayWorkerTransportError("A worker bearer token is required.")
        self._base_url = _validate_api_base_url(base_url)
        self._token = SecretStr(bearer_token)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    async def __aenter__(self) -> WorkdayWorkerApi:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            logger.info("worker_api_client_closed")

    async def lease_next(
        self, *, application_id: uuid.UUID | None = None
    ) -> LeasedWorkdayApplication | None:
        """Lease through the legacy account-gate worker endpoint."""
        return await self._lease_next(
            path="/api/v1/automation/worker/queue/next",
            application_id=application_id,
        )

    async def lease_next_unit1(
        self, *, application_id: uuid.UUID | None = None
    ) -> LeasedWorkdayApplication | None:
        """Lease Unit 1 through the application-scope worker endpoint."""
        return await self._lease_next(
            path="/api/v1/automation/worker/unit1/queue/next",
            application_id=application_id,
        )

    async def _lease_next(
        self, *, path: str, application_id: uuid.UUID | None
    ) -> LeasedWorkdayApplication | None:
        params = (
            {"application_id": str(application_id)}
            if application_id is not None
            else None
        )
        payload = await self._request_json(
            "GET",
            path,
            params=params,
        )
        try:
            envelope = _LeaseEnvelope.model_validate(payload)
        except ValueError as exc:
            raise WorkdayWorkerTransportError(
                "The worker lease response was invalid."
            ) from exc
        if envelope.application is None:
            logger.info("worker_api_lease_empty")
            return None
        item = envelope.application
        lease = LeasedWorkdayApplication(
            application_id=item.id,
            lease_id=item.lease_id,
            user_id=item.user_id,
            portal=item.portal,
            job_url=item.job_url,
            external_ats_url=item.external_ats_url,
            job_title=item.job_title,
            company_name=item.company_name,
            gate_id=item.gate_id,
            gate_generation=item.gate_generation,
            gate_lease_token=item.gate_lease_token,
            gate_decision=item.gate_decision,
            gate_lease_expires_at=item.gate_lease_expires_at,
            gate_next_eligible_at=item.gate_next_eligible_at,
        )
        try:
            lease.to_workday_lease().target_url
        except WorkdayWorkerError as exc:
            raise WorkdayWorkerTransportError(
                "The worker lease did not contain an approved Workday target."
            ) from exc
        logger.info(
            "worker_api_lease_validated application_id=%s portal=%s",
            lease.application_id,
            lease.portal,
        )
        return lease

    def state_emitter(self, lease: LeasedWorkdayApplication) -> WorkdayStateEmitter:
        return _LeaseStateEmitter(self, lease)

    async def release_startup_lease(self, lease: LeasedWorkdayApplication) -> None:
        """Release both authorities only before any browser action starts."""
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/startup-release",
            json={
                "lease_id": str(lease.lease_id),
                "gate_id": str(lease.gate_id),
                "gate_generation": lease.gate_generation,
                "gate_lease_token": lease.gate_lease_token,
            },
        )

    async def map_approved_form_fields(
        self,
        lease: LeasedWorkdayApplication,
        *,
        page_url: str,
        fields: list[WorkdayFormField],
    ) -> list[WorkdayFormAssignment]:
        payload = await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/autofill/map",
            json={
                "lease_id": str(lease.lease_id),
                "page_url": page_url,
                "fields": [field.to_payload() for field in fields],
            },
        )
        try:
            envelope = _AutofillEnvelope.model_validate(payload)
        except ValueError as exc:
            raise WorkdayWorkerTransportError(
                "The worker autofill response was invalid."
            ) from exc
        assignments = [
            WorkdayFormAssignment(
                field_uid=item.field_uid,
                value=item.value,
                answer_source=item.answer_source,
                review_reasons=tuple(item.review_reasons),
            )
            for item in envelope.assignments
        ]
        logger.info(
            "worker_autofill_mapping_received application_id=%s field_count=%s assignment_count=%s",
            lease.application_id,
            len(fields),
            len(assignments),
        )
        return assignments

    async def create_hold(
        self,
        lease: LeasedWorkdayApplication,
        *,
        hold_code: str,
        remediation: str,
        question: str | None = None,
    ) -> None:
        await self._request_json(
            "POST",
            "/api/v1/automation/worker/holds",
            json={
                "application_id": str(lease.application_id),
                "lease_id": str(lease.lease_id),
                "hold_code": hold_code,
                "remediation": remediation,
                "question": question,
                "portal": lease.portal,
            },
        )

    async def record_retry(
        self, lease: LeasedWorkdayApplication, *, safe_reason: str
    ) -> None:
        if not safe_reason or len(safe_reason) > 120:
            raise WorkdayWorkerTransportError("A bounded retry reason is required.")
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/result",
            json={
                "lease_id": str(lease.lease_id),
                "result": "retrying",
                "confirmation_evidence": safe_reason,
                "submitted_answers": [],
            },
        )

    async def release_cooldown_or_defer(
        self, lease: LeasedWorkdayApplication, *, reason: str
    ) -> None:
        """Release a lease while the private gate keeps it out of the queue."""
        if reason not in {"start_or_refresh_cooldown", "defer"}:
            raise WorkdayWorkerTransportError("The cooldown/defer reason is invalid.")
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/cooldown-release",
            json={
                "lease_id": str(lease.lease_id),
                "gate_id": str(lease.gate_id),
                "gate_generation": lease.gate_generation,
                "gate_lease_token": lease.gate_lease_token,
                "reason": reason,
            },
        )

    async def record_skip(self, lease: LeasedWorkdayApplication) -> None:
        """Mark a portal-confirmed unavailable Workday job as terminally skipped."""
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/result",
            json={
                "lease_id": str(lease.lease_id),
                "result": "skipped",
                "confirmation_evidence": "workday_job_unavailable",
                "submitted_answers": [],
            },
        )

    async def record_unit1_complete(
        self,
        lease: LeasedWorkdayApplication,
        *,
        authentication_submitted: bool,
        outcome: str = "complete",
        hold_code: str = "unknown_page_state",
    ) -> None:
        """Finalize or review Unit 1 through the guarded server transaction."""
        if outcome not in {"complete", "review"}:
            raise WorkdayWorkerTransportError("The Unit 1 outcome is invalid.")
        if hold_code not in {
            "unknown_page_state",
            "native_credentials_required",
            "existing_account_credentials_required",
            "account_discovery_retry_exhausted",
            "captcha",
            "otp",
        }:
            raise WorkdayWorkerTransportError("The Unit 1 hold code is invalid.")
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/unit1-complete",
            json={
                "lease_id": str(lease.lease_id),
                "gate_id": str(lease.gate_id),
                "gate_generation": lease.gate_generation,
                "gate_lease_token": lease.gate_lease_token,
                "authentication_submitted": authentication_submitted,
                "outcome": outcome,
                "hold_code": hold_code,
            },
        )

    async def record_unit1_review(
        self,
        lease: LeasedWorkdayApplication,
        *,
        authentication_submitted: bool,
        hold_code: str = "unknown_page_state",
    ) -> None:
        """Atomically require gate review and block only this application."""
        await self.record_unit1_complete(
            lease,
            authentication_submitted=authentication_submitted,
            outcome="review",
            hold_code=hold_code,
        )

    async def _emit_state(
        self,
        lease: LeasedWorkdayApplication,
        page_state: NativeAccountPageState,
    ) -> None:
        logger.info(
            "worker_account_state_emit application_id=%s page_state=%s",
            lease.application_id,
            page_state.value,
        )
        await self._request_json(
            "POST",
            f"/api/v1/automation/worker/queue/{lease.application_id}/account-state",
            json={
                "lease_id": str(lease.lease_id),
                "page_state": page_state.value,
            },
        )

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        started_at = perf_counter()
        logger.info("worker_api_request_started method=%s path=%s", method, path)
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
            logger.info(
                "worker_api_request_completed method=%s path=%s status_code=%s duration_ms=%.2f",
                method,
                path,
                response.status_code,
                (perf_counter() - started_at) * 1000,
                extra={
                    "method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": (perf_counter() - started_at) * 1000,
                },
            )
            return payload
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "worker_api_request_rejected method=%s path=%s status_code=%s duration_ms=%.2f",
                method,
                path,
                exc.response.status_code,
                (perf_counter() - started_at) * 1000,
                extra={
                    "method": method,
                    "path": path,
                    "status_code": exc.response.status_code,
                    "duration_ms": (perf_counter() - started_at) * 1000,
                },
            )
            raise WorkdayWorkerTransportError(
                f"Worker API returned HTTP {exc.response.status_code}."
            ) from None
        except (httpx.RequestError, ValueError) as exc:
            logger.warning(
                "worker_api_request_failed method=%s path=%s error_type=%s duration_ms=%.2f",
                method,
                path,
                type(exc).__name__,
                (perf_counter() - started_at) * 1000,
                extra={
                    "method": method,
                    "path": path,
                    "duration_ms": (perf_counter() - started_at) * 1000,
                },
            )
            raise WorkdayWorkerTransportError(
                "Worker API request or response processing failed."
            ) from None


class _LeaseStateEmitter:
    """Bind state events to exactly one active application lease."""

    def __init__(self, api: WorkdayWorkerApi, lease: LeasedWorkdayApplication):
        self._api = api
        self._lease = lease

    async def emit(
        self, application_id: str, page_state: NativeAccountPageState
    ) -> None:
        if application_id != str(self._lease.application_id):
            raise WorkdayWorkerTransportError(
                "Account state application does not match the active lease."
            )
        await self._api._emit_state(self._lease, page_state)
