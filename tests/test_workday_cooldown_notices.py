import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.automation import _cooldown_notice_response
from models.database import (
    JobApplication,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
    WorkdayCooldownNotice,
)
from services.workday_account_gate_store import (
    SQLAlchemyWorkdayAccountGateStore,
    WorkdayGateLease,
)
from services.workday_cooldown_notices import (
    SQLAlchemyWorkdayCooldownNoticeStore,
    WorkdayCooldownDecision,
    WorkdayCooldownNoticeNotFoundError,
)
from services.workday_transition_contracts import WorkdayGateState


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, value=None, rows=None) -> None:
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.value

    def all(self):
        return self.rows


class _Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock = lock

    async def __aenter__(self) -> None:
        await self.lock.acquire()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.lock.release()


class _Database:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.applications: dict[UUID, JobApplication] = {}
        self.gates: dict[UUID, WorkdayAccountGate] = {}
        self.attempts: dict[UUID, WorkdayAuthAttempt] = {}
        self.notices: dict[UUID, WorkdayCooldownNotice] = {}
        self.profile = SimpleNamespace(id=uuid4())
        self.resume = SimpleNamespace(id=uuid4())
        self.batch = SimpleNamespace(id=uuid4())
        self.catalog = SimpleNamespace(id=uuid4())


class _Session:
    def __init__(self, database: _Database) -> None:
        self.database = database

    def begin(self) -> _Transaction:
        return _Transaction(self.database.lock)

    async def execute(self, statement):
        params = statement.compile().params
        entities = [item["entity"] for item in statement.column_descriptions]
        if entities == [WorkdayCooldownNotice, WorkdayAccountGate]:
            user_id = params["user_id_1"]
            rows = []
            for notice in self.database.notices.values():
                gate = self.database.gates[notice.gate_id]
                application = self.database.applications[notice.application_id]
                if (
                    gate.user_id == user_id
                    and application.user_id == user_id
                    and application.deleted_at is None
                    and notice.status == "pending"
                ):
                    rows.append((notice, gate))
            return _Result(rows=rows)

        entity = entities[0]
        if entity is WorkdayCooldownNotice:
            return _Result(self.database.notices.get(params["id_1"]))
        if entity is JobApplication:
            application = self.database.applications.get(params["id_1"])
            if application is not None and "user_id_1" in params:
                if (
                    application.user_id != params["user_id_1"]
                    or application.deleted_at is not None
                ):
                    application = None
            return _Result(application)
        if entity is WorkdayAccountGate:
            gate = self.database.gates.get(params["id_1"])
            if gate is not None and "user_id_1" in params:
                if gate.user_id != params["user_id_1"]:
                    gate = None
            return _Result(gate)
        if entity is WorkdayAuthAttempt:
            candidates = list(self.database.attempts.values())
            if "application_id_1" in params:
                attempt = next(
                    (
                        item
                        for item in candidates
                        if item.gate_id == params["gate_id_1"]
                        and item.application_id == params["application_id_1"]
                        and item.generation == params["generation_1"]
                        and item.lease_token == params["lease_token_1"]
                    ),
                    None,
                )
            else:
                attempt = next(
                    (
                        item
                        for item in candidates
                        if item.gate_id == params["gate_id_1"]
                        and item.status == "active"
                    ),
                    None,
                )
            return _Result(attempt)
        raise AssertionError(f"Unexpected entity: {entity}")

    def add(self, _value) -> None:
        return None

    async def flush(self) -> None:
        return None


def _fixture(*, submitted: bool = False):
    database = _Database()
    user_id = uuid4()
    application = JobApplication(
        id=uuid4(),
        user_id=user_id,
        status="preparing",
        deleted_at=None,
        session_id=None,
        automation_lease_id=uuid4(),
        automation_lease_expires_at=NOW + timedelta(minutes=10),
    )
    application.retry_count = 2
    application.requeue_count = 3
    gate = WorkdayAccountGate(
        id=uuid4(),
        user_id=user_id,
        account_ref=str(uuid4()),
        portal_scope="workday:wf:wellsfargojobs",
        state=(
            WorkdayGateState.AUTH_OUTCOME_PENDING.value
            if submitted
            else WorkdayGateState.PROBE_IN_PROGRESS.value
        ),
        generation=4,
        cooldown_until=NOW + timedelta(hours=6),
        user_min_until=None,
        next_eligible_at=NOW + timedelta(hours=6),
        created_at=NOW,
        updated_at=NOW,
    )
    application.workday_account_gate_id = gate.id
    attempt = WorkdayAuthAttempt(
        id=uuid4(),
        gate_id=gate.id,
        application_id=application.id,
        generation=gate.generation,
        lease_token="opaque-test-authority",
        lease_expires_at=NOW + timedelta(minutes=10),
        secret_accessed=submitted,
        auth_submit_count=1 if submitted else 0,
        llm_repair_count=0,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    notice = WorkdayCooldownNotice(
        id=uuid4(),
        gate_id=gate.id,
        application_id=application.id,
        gate_generation=gate.generation,
        status="pending",
        created_at=NOW,
        updated_at=NOW,
    )
    database.applications[application.id] = application
    database.gates[gate.id] = gate
    database.attempts[attempt.id] = attempt
    database.notices[notice.id] = notice
    session = cast(AsyncSession, _Session(database))
    store = SQLAlchemyWorkdayCooldownNoticeStore(session, clock=lambda: NOW)
    return database, store, user_id, application, gate, attempt, notice, session


@pytest.mark.asyncio
async def test_only_owner_lists_and_decides_secret_free_notice() -> None:
    database, store, user_id, _application, _gate, _attempt, notice, _session = (
        _fixture()
    )
    payload = _cooldown_notice_response((await store.list_pending(user_id=user_id))[0])

    assert set(payload) == {
        "id",
        "application_id",
        "status",
        "message",
        "safe_next_attempt_at",
        "created_at",
    }
    assert payload["safe_next_attempt_at"].tzinfo is UTC
    assert {
        "account_ref",
        "portal_scope",
        "email",
        "credential",
        "lease_token",
    }.isdisjoint(payload)
    assert await store.list_pending(user_id=uuid4()) == []
    with pytest.raises(WorkdayCooldownNoticeNotFoundError):
        await store.decide(
            user_id=uuid4(),
            notice_id=notice.id,
            decision=WorkdayCooldownDecision.KEEP,
        )
    assert database.notices[notice.id].status == "pending"


@pytest.mark.asyncio
async def test_keep_never_shortens_wait_or_requeues() -> None:
    _database, store, user_id, application, gate, attempt, notice, _session = _fixture()
    before = (
        gate.generation,
        gate.cooldown_until,
        gate.next_eligible_at,
        application.status,
        application.automation_lease_id,
        application.retry_count,
        application.requeue_count,
        attempt.status,
    )

    result = await store.decide(
        user_id=user_id,
        notice_id=notice.id,
        decision=WorkdayCooldownDecision.KEEP,
    )

    assert notice.status == "kept"
    assert result.notice.safe_next_attempt_at == NOW + timedelta(hours=6)
    assert before == (
        gate.generation,
        gate.cooldown_until,
        gate.next_eligible_at,
        application.status,
        application.automation_lease_id,
        application.retry_count,
        application.requeue_count,
        attempt.status,
    )


@pytest.mark.asyncio
async def test_extend_requires_future_time_and_revokes_pre_submit_probe() -> None:
    _database, store, user_id, application, gate, attempt, notice, _session = _fixture()
    with pytest.raises(ValueError):
        await store.decide(
            user_id=user_id,
            notice_id=notice.id,
            decision=WorkdayCooldownDecision.EXTEND,
            extend_until=NOW,
        )

    selected = NOW + timedelta(hours=12)
    await store.decide(
        user_id=user_id,
        notice_id=notice.id,
        decision=WorkdayCooldownDecision.EXTEND,
        extend_until=selected,
    )

    assert (gate.user_min_until, gate.cooldown_until, gate.next_eligible_at) == (
        selected,
        selected,
        selected,
    )
    assert gate.generation == 5
    assert gate.state == WorkdayGateState.COOLING_DOWN.value
    assert attempt.status == "abandoned"
    assert application.automation_lease_id is None
    assert application.status == "preparing"
    assert (application.retry_count, application.requeue_count) == (2, 3)


@pytest.mark.asyncio
async def test_post_submit_observer_cannot_clear_extended_wait() -> None:
    _database, store, user_id, application, gate, attempt, notice, session = _fixture(
        submitted=True
    )
    selected = NOW + timedelta(hours=12)
    await store.decide(
        user_id=user_id,
        notice_id=notice.id,
        decision=WorkdayCooldownDecision.EXTEND,
        extend_until=selected,
    )

    assert gate.generation == 4
    assert gate.state == WorkdayGateState.AUTH_OUTCOME_PENDING.value
    assert attempt.status == "active"
    mutation = await SQLAlchemyWorkdayAccountGateStore(
        session, clock=lambda: NOW
    ).complete_success(
        WorkdayGateLease(
            gate_id=gate.id,
            application_id=application.id,
            generation=4,
            lease_token=attempt.lease_token,
        )
    )
    assert mutation.applied is True
    assert gate.state == WorkdayGateState.COOLING_DOWN.value
    assert gate.cooldown_until == selected
    assert gate.user_min_until == selected


@pytest.mark.asyncio
async def test_delete_requires_confirmation_and_preserves_all_other_scope() -> None:
    database, store, user_id, application, gate, attempt, notice, _session = _fixture(
        submitted=True
    )
    other = JobApplication(
        id=uuid4(), user_id=user_id, status="queued", deleted_at=None, session_id=None
    )
    database.applications[other.id] = other
    preserved = (database.profile, database.resume, database.batch, database.catalog)
    with pytest.raises(ValueError):
        await store.decide(
            user_id=user_id,
            notice_id=notice.id,
            decision=WorkdayCooldownDecision.DELETE,
        )

    await store.decide(
        user_id=user_id,
        notice_id=notice.id,
        decision=WorkdayCooldownDecision.DELETE,
        confirm_delete=True,
    )

    assert application.deleted_at == NOW
    assert other.deleted_at is None
    assert gate.cooldown_until == NOW + timedelta(hours=6)
    assert gate.next_eligible_at == NOW + timedelta(hours=6)
    assert gate.account_ref
    assert gate.state == WorkdayGateState.REVIEW_REQUIRED.value
    assert attempt.status == "review_required"
    assert notice.status == "deleted"
    assert preserved == (
        database.profile,
        database.resume,
        database.batch,
        database.catalog,
    )
    assert (application.retry_count, application.requeue_count) == (2, 3)
