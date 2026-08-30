from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from api.automation import (
    RecordResultRequest,
    WorkerAutofillMapRequest,
    worker_map_approved_form_fields,
    worker_record_application_result,
)
from api.extension_autofill import AutofillFieldIn, AutofillMapResponse
from services.portal_account_automation import NativeAccountPageState
from services.workday_worker_api import (
    WorkdayWorkerApi,
    WorkdayWorkerTransportError,
)

_LEASE = {
    "id": "00000000-0000-0000-0000-000000000001",
    "lease_id": "00000000-0000-0000-0000-000000000002",
    "portal": "workday",
    "job_url": (
        "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
        "WellsFargoJobs/job/Engineer_R-1"
    ),
    "external_ats_url": None,
    "job_title": "Engineer",
    "company_name": "Wells Fargo",
    "user_id": "00000000-0000-0000-0000-000000000003",
    "gate_id": "00000000-0000-0000-0000-000000000004",
    "gate_generation": 7,
    "gate_lease_token": "opaque-gate-token",
    "gate_decision": "allow",
    "gate_lease_expires_at": "2026-08-28T12:10:00Z",
    "gate_next_eligible_at": None,
}


@pytest.mark.asyncio
async def test_transport_leases_emits_holds_and_retries_without_token_payload(
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="services.workday_worker_api")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"application": _LEASE})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WorkdayWorkerApi(
        base_url="http://localhost:8000",
        bearer_token="dummy-worker-token",
        client=client,
    )

    lease = await api.lease_next(application_id=uuid.UUID(_LEASE["id"]))
    assert lease is not None
    await api.state_emitter(lease).emit(
        str(lease.application_id), NativeAccountPageState.LOGIN_REQUIRED
    )
    await api.create_hold(
        lease,
        hold_code="captcha",
        remediation="Complete the CAPTCHA in the dedicated browser.",
        question="Complete the verification challenge",
    )
    await api.record_retry(lease, safe_reason="transient_account_page")
    await api.record_skip(lease)

    assert requests[0].url.path.endswith("/automation/worker/queue/next")
    assert requests[0].url.params["application_id"] == _LEASE["id"]
    assert requests[1].url.path.endswith("/account-state")
    assert requests[2].url.path.endswith("/automation/worker/holds")
    assert requests[3].url.path.endswith("/result")
    assert requests[4].url.path.endswith("/result")
    bodies = [json.loads(request.content) for request in requests[1:]]
    assert bodies[0] == {
        "lease_id": _LEASE["lease_id"],
        "page_state": "login_required",
    }
    assert bodies[1]["lease_id"] == _LEASE["lease_id"]
    assert bodies[1]["question"] == "Complete the verification challenge"
    assert bodies[2]["result"] == "retrying"
    assert bodies[3]["result"] == "skipped"
    assert bodies[3]["confirmation_evidence"] == "workday_job_unavailable"
    assert all(
        "dummy-worker-token" not in request.content.decode() for request in requests
    )
    assert "dummy-worker-token" not in repr(api)
    assert "opaque-gate-token" not in repr(lease)
    assert "worker_api_request_started" in caplog.text
    assert "worker_api_request_completed" in caplog.text
    assert "worker_account_state_emit" in caplog.text
    assert "dummy-worker-token" not in caplog.text
    assert "candidate@example.com" not in caplog.text
    assert "opaque-gate-token" not in caplog.text
    await client.aclose()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:8000",
        "ftp://localhost:8000",
        "http://user:password@localhost:8000",
        "http://localhost:8000?token=secret",
    ],
)
def test_transport_rejects_unsafe_api_urls(base_url: str) -> None:
    with pytest.raises(WorkdayWorkerTransportError):
        WorkdayWorkerApi(base_url=base_url, bearer_token="dummy-worker-token")


@pytest.mark.asyncio
async def test_transport_rejects_non_workday_lease() -> None:
    invalid = dict(_LEASE, job_url="https://example.com/job-1")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"application": invalid})
        )
    )
    api = WorkdayWorkerApi(
        base_url="http://127.0.0.1:8000",
        bearer_token="dummy-worker-token",
        client=client,
    )

    with pytest.raises(WorkdayWorkerTransportError):
        await api.lease_next()
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_releases_only_server_issued_startup_authorities() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"application": _LEASE})
        return httpx.Response(200, json={"status": "retrying"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = WorkdayWorkerApi(
        base_url="http://localhost:8000",
        bearer_token="dummy-worker-token",
        client=client,
    )
    lease = await api.lease_next()
    assert lease is not None

    await api.release_startup_lease(lease)

    assert requests[1].url.path.endswith("/startup-release")
    assert json.loads(requests[1].content) == {
        "lease_id": _LEASE["lease_id"],
        "gate_id": _LEASE["gate_id"],
        "gate_generation": _LEASE["gate_generation"],
        "gate_lease_token": _LEASE["gate_lease_token"],
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_worker_result_allows_only_evidenced_unavailable_skip(
    monkeypatch,
) -> None:
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    lease_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    captured = {}

    async def record_result(**kwargs):
        captured.update(kwargs)
        return {"status": kwargs["body"].result}

    monkeypatch.setattr("api.automation.record_application_result", record_result)

    response = await worker_record_application_result(
        application_id=application_id,
        body=RecordResultRequest(
            lease_id=lease_id,
            result="skipped",
            confirmation_evidence="workday_job_unavailable",
        ),
        worker_user={"id": "user-1"},
        db=None,
    )

    assert response == {"status": "skipped"}
    assert captured["application_id"] == application_id

    for body in (
        RecordResultRequest(lease_id=lease_id, result="applied"),
        RecordResultRequest(
            lease_id=lease_id,
            result="skipped",
            confirmation_evidence="unverified_reason",
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await worker_record_application_result(
                application_id=application_id,
                body=body,
                worker_user={"id": "user-1"},
                db=None,
            )
        assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_worker_autofill_mapping_is_lease_bound_and_workday_only(
    monkeypatch,
) -> None:
    application_id = uuid.UUID(_LEASE["id"])
    lease_id = uuid.UUID(_LEASE["lease_id"])
    user_id = uuid.UUID(_LEASE["user_id"])
    application = SimpleNamespace(
        id=application_id,
        user_id=user_id,
        deleted_at=None,
        automation_lease_id=lease_id,
        automation_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    class Database:
        async def get(self, model, key):
            del model
            return application if key == application_id else None

    async def approved_mapper(request, **kwargs):
        assert request.application_id == application_id
        assert kwargs["user_id"] == user_id
        return AutofillMapResponse(application_id=application_id)

    monkeypatch.setattr(
        "api.automation.map_form_fields_from_approved_sources", approved_mapper
    )
    field = AutofillFieldIn(field_uid="0", tag="input", label_text="First name")
    body = WorkerAutofillMapRequest(
        lease_id=lease_id,
        fields=[field],
        page_url="https://wd1.myworkdaysite.com/recruiting/wf/site/job/R-1/apply",
    )

    response = await worker_map_approved_form_fields(
        application_id=application_id,
        body=body,
        worker_user={"id": str(user_id)},
        db=Database(),
    )
    assert response.application_id == application_id

    body.page_url = "https://example.com/apply"
    with pytest.raises(HTTPException) as exc_info:
        await worker_map_approved_form_fields(
            application_id=application_id,
            body=body,
            worker_user={"id": str(user_id)},
            db=Database(),
        )
    assert exc_info.value.status_code == 422
