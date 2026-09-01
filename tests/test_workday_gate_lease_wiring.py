from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from api.automation import (
    ReleaseWorkdayStartupLeaseRequest,
    lease_next_application,
    worker_release_startup_lease,
)
from services.portal_credentials import PortalAccountMetadataRepository
from services.workday_account_gate_store import (
    WorkdayGateAcquisition,
    WorkdayGateDecision,
    WorkdayGateMutation,
    WorkdayGateOwnershipError,
)


USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_REF = uuid.UUID("00000000-0000-0000-0000-000000000002")
GATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
LEASE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
NOW = datetime.now(UTC)
WORKDAY_URL = (
    "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Engineer_R-1"
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _LeaseDatabase:
    def __init__(self, application):
        self.application = application
        self.execute_count = 0
        self.added = []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        self.execute_count += 1
        if self.execute_count == 1:
            return _Result(self.application)
        return _Result(None)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _application(*, application_id=None, status="queued", worker_kind="local"):
    del worker_kind
    return SimpleNamespace(
        id=application_id or uuid.uuid4(),
        user_id=USER_ID,
        deleted_at=None,
        status=status,
        retry_count=2,
        portal="workday",
        job_url=WORKDAY_URL,
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        created_at=NOW,
        automation_batch_id=uuid.uuid4(),
        automation_lease_id=None,
        automation_lease_expires_at=None,
        workday_account_gate_id=GATE_ID,
    )


def _acquisition(decision: WorkdayGateDecision) -> WorkdayGateAcquisition:
    eligible = decision is not WorkdayGateDecision.DEFER
    return WorkdayGateAcquisition(
        decision=decision,
        gate_id=GATE_ID,
        generation=3,
        lease_token="opaque-gate-token" if eligible else None,
        lease_expires_at=NOW + timedelta(minutes=10) if eligible else None,
        next_eligible_at=NOW + timedelta(hours=1) if not eligible else None,
    )


class _GateStore:
    def __init__(self, decision=WorkdayGateDecision.ALLOW):
        self.decision = decision
        self.requests = []

    async def acquire_in_transaction(self, request):
        self.requests.append(request)
        return _acquisition(self.decision)


def _patch_gate_dependencies(monkeypatch, gate_store):
    async def account_ref(**kwargs):
        assert kwargs["user_id"] == USER_ID
        assert kwargs["portal_scope"] == "workday:wf:wellsfargojobs"
        return ACCOUNT_REF

    monkeypatch.setattr("api.automation._resolve_workday_account_ref", account_ref)
    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore",
        lambda db: gate_store,
    )


@pytest.mark.asyncio
async def test_queue_query_excludes_future_cooldown_and_review_before_worker_work() -> (
    None
):
    database = _LeaseDatabase(None)

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": str(USER_ID)},
        db=database,
    )

    compiled = str(
        database.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert response == {"application": None}
    assert "workday_account_gates.state = 'open'" in compiled
    assert "workday_account_gates.state = 'cooling_down'" in compiled
    assert "workday_account_gates.state = 'probe_in_progress'" in compiled
    assert "workday_account_gates.state = 'auth_outcome_pending'" in compiled
    assert "workday_auth_attempts.status = 'active'" in compiled
    assert "workday_auth_attempts.lease_expires_at" in compiled
    assert "cooldown_until" in compiled
    assert "user_min_until" in compiled
    assert "next_eligible_at" in compiled
    assert "review_required" not in compiled


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [WorkdayGateDecision.DEFER])
async def test_cooldown_or_review_defers_without_lifecycle_or_retry_change(
    monkeypatch, decision
) -> None:
    application = _application()
    original = (application.status, application.retry_count)
    database = _LeaseDatabase(application)
    gate_store = _GateStore(decision)
    _patch_gate_dependencies(monkeypatch, gate_store)

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": str(USER_ID)},
        db=database,
        application_id=application.id,
    )

    assert response == {"application": None}
    assert (application.status, application.retry_count) == original
    assert application.automation_lease_id is None
    assert database.added == []
    assert database.commits == 1


@pytest.mark.asyncio
async def test_owned_gate_metadata_is_non_secret_and_server_derived(
    monkeypatch,
) -> None:
    application = _application()
    database = _LeaseDatabase(application)
    gate_store = _GateStore()
    _patch_gate_dependencies(monkeypatch, gate_store)

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": str(USER_ID), "email": "candidate@example.com"},
        db=database,
        application_id=application.id,
    )

    payload = response["application"]
    assert payload is not None
    assert payload["gate_id"] == str(GATE_ID)
    assert payload["gate_generation"] == 3
    assert payload["gate_lease_token"] == "opaque-gate-token"
    assert payload["gate_decision"] == "allow"
    assert "account_email" not in payload
    assert "account_ref" not in payload
    assert "credential" not in payload
    request = gate_store.requests[0]
    assert request.user_id == USER_ID
    assert request.account_ref == ACCOUNT_REF
    assert request.application_id == application.id


@pytest.mark.asyncio
async def test_wrong_user_gate_binding_fails_closed(monkeypatch) -> None:
    application = _application()
    database = _LeaseDatabase(application)

    class RejectingGateStore:
        async def acquire_in_transaction(self, request):
            del request
            raise WorkdayGateOwnershipError("wrong owner")

    _patch_gate_dependencies(monkeypatch, RejectingGateStore())
    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": str(USER_ID)},
        db=database,
        application_id=application.id,
    )

    assert response == {"application": None}
    assert database.rollbacks == 1
    assert application.automation_lease_id is None


@pytest.mark.asyncio
async def test_exactly_one_sibling_wins_expired_cooldown_probe(monkeypatch) -> None:
    lock = asyncio.Lock()
    probe_granted = False

    class ConcurrentGateStore:
        async def acquire_in_transaction(self, request):
            nonlocal probe_granted
            del request
            async with lock:
                if probe_granted:
                    return _acquisition(WorkdayGateDecision.DEFER)
                probe_granted = True
                return _acquisition(WorkdayGateDecision.ONE_PROBE)

    gate_store = ConcurrentGateStore()
    _patch_gate_dependencies(monkeypatch, gate_store)
    databases = [_LeaseDatabase(_application()) for _ in range(2)]

    responses = await asyncio.gather(
        *(
            lease_next_application(
                worker_kind="local_playwright",
                current_user={"id": str(USER_ID)},
                db=database,
            )
            for database in databases
        )
    )

    leased = [item["application"] for item in responses if item["application"]]
    assert len(leased) == 1
    assert leased[0]["gate_decision"] == "one_probe"
    assert sum(database.commits for database in databases) == 2


@pytest.mark.asyncio
async def test_extension_lease_remains_gate_independent(monkeypatch) -> None:
    application = _application()
    database = _LeaseDatabase(application)

    async def forbidden_resolver(**kwargs):
        del kwargs
        raise AssertionError("extension leasing must not read Workday vault metadata")

    monkeypatch.setattr(
        "api.automation._resolve_workday_account_ref", forbidden_resolver
    )
    response = await lease_next_application(
        worker_kind="extension",
        current_user={"id": str(USER_ID), "email": "candidate@example.com"},
        db=database,
    )

    payload = response["application"]
    assert payload is not None
    assert payload["account_email"] == "candidate@example.com"
    assert "gate_id" not in payload


@pytest.mark.asyncio
async def test_startup_failure_releases_gate_and_application_atomically(
    monkeypatch,
) -> None:
    application = _application(status="preparing")
    application.automation_lease_id = LEASE_ID
    application.automation_lease_expires_at = NOW + timedelta(minutes=5)
    database = _LeaseDatabase(application)
    released = []

    class ReleaseGateStore:
        async def release_unsubmitted_in_transaction(self, lease):
            released.append(lease)
            return WorkdayGateMutation(applied=True)

    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore",
        lambda db: ReleaseGateStore(),
    )
    response = await worker_release_startup_lease(
        application_id=application.id,
        body=ReleaseWorkdayStartupLeaseRequest(
            lease_id=LEASE_ID,
            gate_id=GATE_ID,
            gate_generation=3,
            gate_lease_token="opaque-gate-token",
        ),
        worker_user={"id": str(USER_ID)},
        db=database,
    )

    assert response["status"] == "retrying"
    assert application.automation_lease_id is None
    assert application.automation_lease_expires_at is None
    assert released[0].application_id == application.id
    assert database.commits == 1


@pytest.mark.asyncio
async def test_startup_release_rejects_wrong_gate_authority(monkeypatch) -> None:
    application = _application(status="preparing")
    application.automation_lease_id = LEASE_ID
    application.automation_lease_expires_at = NOW + timedelta(minutes=5)
    database = _LeaseDatabase(application)

    def forbidden_store(db):
        del db
        raise AssertionError("wrong gate must fail before store mutation")

    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore", forbidden_store
    )
    with pytest.raises(HTTPException) as exc_info:
        await worker_release_startup_lease(
            application_id=application.id,
            body=ReleaseWorkdayStartupLeaseRequest(
                lease_id=LEASE_ID,
                gate_id=uuid.uuid4(),
                gate_generation=3,
                gate_lease_token="wrong-gate-token",
            ),
            worker_user={"id": str(USER_ID)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert database.rollbacks == 1
    assert application.automation_lease_id == LEASE_ID


@pytest.mark.asyncio
async def test_account_reference_lookup_reads_only_safe_owned_metadata() -> None:
    class Credentials:
        def __init__(self):
            self.filter = None
            self.projection = None

        async def find_one(self, filter_value, projection):
            self.filter = filter_value
            self.projection = projection
            return {
                "_id": str(ACCOUNT_REF),
                "user_id": str(USER_ID),
                "portal_scope": "workday:wf:wellsfargojobs",
                "password_encrypted": "must-not-be-read",
                "account_email": "must-not-be-read@example.com",
            }

    credentials = Credentials()
    resolved = await PortalAccountMetadataRepository(credentials).resolve_account_ref(
        user_id=str(USER_ID),
        portal_scope="workday:wf:wellsfargojobs",
    )

    assert resolved == ACCOUNT_REF
    assert credentials.filter == {
        "user_id": str(USER_ID),
        "portal_scope": "workday:wf:wellsfargojobs",
    }
    assert credentials.projection == {"_id": 1, "user_id": 1, "portal_scope": 1}


@pytest.mark.asyncio
async def test_unit1_queue_query_excludes_unit1_completed_applications() -> None:
    database = _LeaseDatabase(None)

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": str(USER_ID)},
        db=database,
    )

    compiled = str(
        database.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert response == {"application": None}
    assert "workday_unit1_completed" in compiled
    assert "not (exists" in compiled or "not exists" in compiled
