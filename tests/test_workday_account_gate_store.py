import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from models.database import (
    JobApplication,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
    WorkdayCooldownNotice,
)
from services.workday_account_gate_store import (
    SQLAlchemyWorkdayAccountGateStore,
    WorkdayGateAcquireRequest,
    WorkdayGateDecision,
    WorkdayGateOwnershipError,
    WorkdayGateStoreError,
)
from services.workday_transition_contracts import WorkdayGateState


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SCOPE = "workday:wf:wellsfargojobs"


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._lock.release()


class _MemoryDatabase:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.applications: dict[UUID, SimpleNamespace] = {}
        self.gates: dict[UUID, WorkdayAccountGate] = {}
        self.attempts: dict[UUID, WorkdayAuthAttempt] = {}
        self.notices: dict[UUID, WorkdayCooldownNotice] = {}
        self.statements: list[object] = []


class _MemorySession:
    """Small locked SQLAlchemy-session double for deterministic policy tests."""

    def __init__(self, database: _MemoryDatabase) -> None:
        self.database = database

    def begin(self) -> _Transaction:
        return _Transaction(self.database.lock)

    async def execute(self, statement):
        self.database.statements.append(statement)
        params = statement.compile().params
        if isinstance(statement, Insert):
            identity = (
                params["user_id"],
                params["account_ref"],
                params["portal_scope"],
            )
            if not any(
                (gate.user_id, gate.account_ref, gate.portal_scope) == identity
                for gate in self.database.gates.values()
            ):
                gate = WorkdayAccountGate(**params)
                self.database.gates[gate.id] = gate
            return _ScalarResult(None)

        entity = statement.column_descriptions[0]["entity"]
        if entity is JobApplication:
            application = self.database.applications.get(params["id_1"])
            if (
                application is None
                or application.user_id != params["user_id_1"]
                or application.deleted_at is not None
            ):
                application = None
            return _ScalarResult(application)
        if entity is WorkdayAccountGate:
            if "account_ref_1" in params:
                gate = next(
                    (
                        candidate
                        for candidate in self.database.gates.values()
                        if candidate.user_id == params["user_id_1"]
                        and candidate.account_ref == params["account_ref_1"]
                        and candidate.portal_scope == params["portal_scope_1"]
                    ),
                    None,
                )
            else:
                gate = self.database.gates.get(params["id_1"])
            return _ScalarResult(gate)
        if entity is WorkdayAuthAttempt:
            attempts = list(self.database.attempts.values())
            if "application_id_1" in params:
                attempt = next(
                    (
                        candidate
                        for candidate in attempts
                        if candidate.gate_id == params["gate_id_1"]
                        and candidate.application_id == params["application_id_1"]
                        and candidate.generation == params["generation_1"]
                        and candidate.lease_token == params["lease_token_1"]
                    ),
                    None,
                )
            else:
                attempt = next(
                    (
                        candidate
                        for candidate in attempts
                        if candidate.gate_id == params["gate_id_1"]
                        and candidate.status == "active"
                    ),
                    None,
                )
            return _ScalarResult(attempt)
        if entity is WorkdayCooldownNotice:
            notice = next(
                (
                    candidate
                    for candidate in self.database.notices.values()
                    if candidate.gate_id == params["gate_id_1"]
                    and candidate.gate_generation == params["gate_generation_1"]
                ),
                None,
            )
            return _ScalarResult(notice)
        raise AssertionError(f"Unexpected statement entity: {entity}")

    def add(self, value: object) -> None:
        if isinstance(value, WorkdayAuthAttempt):
            self.database.attempts[value.id] = value
        elif isinstance(value, WorkdayCooldownNotice):
            self.database.notices[value.id] = value
        else:
            raise AssertionError(f"Unexpected added entity: {type(value)}")

    async def flush(self) -> None:
        return None


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _application(
    database: _MemoryDatabase,
    *,
    user_id: UUID,
    application_id: UUID | None = None,
    status: str = "queued",
) -> SimpleNamespace:
    row = SimpleNamespace(
        id=application_id or uuid4(),
        user_id=user_id,
        deleted_at=None,
        workday_account_gate_id=None,
        status=status,
    )
    database.applications[row.id] = row
    return row


def _request(
    application: SimpleNamespace,
    *,
    account_ref: UUID,
    portal_scope: str = SCOPE,
) -> WorkdayGateAcquireRequest:
    return WorkdayGateAcquireRequest(
        user_id=application.user_id,
        application_id=application.id,
        account_ref=account_ref,
        portal_scope=portal_scope,
    )


def _store(
    database: _MemoryDatabase, clock: _Clock
) -> SQLAlchemyWorkdayAccountGateStore:
    return SQLAlchemyWorkdayAccountGateStore(
        cast(AsyncSession, _MemorySession(database)),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_concurrent_open_acquisition_grants_exactly_one_active_lease() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    account_ref = uuid4()
    first = _application(database, user_id=user_id)
    second = _application(database, user_id=user_id)

    results = await asyncio.gather(
        _store(database, clock).acquire(_request(first, account_ref=account_ref)),
        _store(database, clock).acquire(_request(second, account_ref=account_ref)),
    )

    assert [result.decision for result in results].count(WorkdayGateDecision.ALLOW) == 1
    assert [result.decision for result in results].count(WorkdayGateDecision.DEFER) == 1
    assert (
        len(
            [
                attempt
                for attempt in database.attempts.values()
                if attempt.status == "active"
            ]
        )
        == 1
    )
    assert first.workday_account_gate_id == second.workday_account_gate_id
    rendered = [str(statement) for statement in database.statements]
    assert any("ON CONFLICT" in statement for statement in rendered)
    assert any("FOR UPDATE" in statement for statement in rendered)


@pytest.mark.asyncio
async def test_concurrent_expired_cooldown_grants_exactly_one_probe() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    account_ref = uuid4()
    first = _application(database, user_id=user_id)
    second = _application(database, user_id=user_id)
    initial = await _store(database, clock).acquire(
        _request(first, account_ref=account_ref)
    )
    lease = initial.lease_for(first.id)
    assert lease is not None
    locked = await _store(database, clock).confirm_account_lock(lease)
    assert locked.applied
    assert locked.lock_event is not None
    clock.value = locked.lock_event.cooldown_until + timedelta(seconds=1)

    results = await asyncio.gather(
        _store(database, clock).acquire(_request(first, account_ref=account_ref)),
        _store(database, clock).acquire(_request(second, account_ref=account_ref)),
    )

    decisions = [result.decision for result in results]
    assert decisions.count(WorkdayGateDecision.ONE_PROBE) == 1
    assert decisions.count(WorkdayGateDecision.DEFER) == 1


@pytest.mark.asyncio
async def test_closed_and_live_gate_states_defer_siblings() -> None:
    for state in (
        WorkdayGateState.COOLING_DOWN,
        WorkdayGateState.PROBE_IN_PROGRESS,
        WorkdayGateState.AUTH_OUTCOME_PENDING,
        WorkdayGateState.REVIEW_REQUIRED,
    ):
        database = _MemoryDatabase()
        clock = _Clock()
        user_id = uuid4()
        account_ref = uuid4()
        owner = _application(database, user_id=user_id)
        sibling = _application(database, user_id=user_id)
        acquired = await _store(database, clock).acquire(
            _request(owner, account_ref=account_ref)
        )
        gate = database.gates[acquired.gate_id]
        gate.state = state.value
        if state is WorkdayGateState.COOLING_DOWN:
            gate.cooldown_until = NOW + timedelta(hours=1)
            next(iter(database.attempts.values())).status = "completed"

        result = await _store(database, clock).acquire(
            _request(sibling, account_ref=account_ref)
        )

        assert result.decision is WorkdayGateDecision.DEFER


@pytest.mark.asyncio
async def test_stale_authority_cannot_clear_or_shorten_gate() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    application = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(application, account_ref=uuid4())
    )
    lease = acquired.lease_for(application.id)
    assert lease is not None
    lock_result = await _store(database, clock).confirm_account_lock(lease)
    assert lock_result.lock_event is not None
    until = lock_result.lock_event.cooldown_until

    stale_success = await _store(database, clock).complete_success(lease)

    gate = database.gates[lease.gate_id]
    assert stale_success.applied is False
    assert stale_success.stale is True
    assert gate.state == WorkdayGateState.COOLING_DOWN.value
    assert gate.cooldown_until == until


@pytest.mark.asyncio
async def test_stale_confirmed_lock_can_extend_but_never_shorten_wait() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    application = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(application, account_ref=uuid4())
    )
    lease = acquired.lease_for(application.id)
    assert lease is not None
    first = await _store(database, clock).confirm_account_lock(lease)
    assert first.lock_event is not None
    first_until = first.lock_event.cooldown_until

    clock.value += timedelta(hours=1)
    extended = await _store(database, clock).confirm_account_lock(lease)
    assert extended.lock_event is not None
    extended_until = extended.lock_event.cooldown_until
    unchanged = await _store(database, clock).confirm_account_lock(
        lease, trusted_portal_until=clock.value + timedelta(minutes=5)
    )

    assert extended.applied and extended.stale
    assert extended_until == first_until + timedelta(hours=1)
    assert unchanged.applied is False and unchanged.stale
    assert database.gates[lease.gate_id].cooldown_until == extended_until


@pytest.mark.asyncio
async def test_concurrent_lock_evidence_creates_one_notice_per_generation() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    application = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(application, account_ref=uuid4())
    )
    lease = acquired.lease_for(application.id)
    assert lease is not None

    await asyncio.gather(
        _store(database, clock).confirm_account_lock(lease),
        _store(database, clock).confirm_account_lock(lease),
    )

    assert len(database.notices) == 1
    notice = next(iter(database.notices.values()))
    assert (notice.gate_id, notice.gate_generation, notice.application_id) == (
        lease.gate_id,
        acquired.generation + 1,
        application.id,
    )


@pytest.mark.asyncio
async def test_auth_submit_claim_is_atomic_zero_to_one() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    application = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(application, account_ref=uuid4())
    )
    lease = acquired.lease_for(application.id)
    assert lease is not None

    results = await asyncio.gather(
        _store(database, clock).claim_auth_submit(lease),
        _store(database, clock).claim_auth_submit(lease),
    )

    assert [result.applied for result in results].count(True) == 1
    attempt = next(iter(database.attempts.values()))
    assert attempt.auth_submit_count == 1
    assert (
        database.gates[lease.gate_id].state
        == WorkdayGateState.AUTH_OUTCOME_PENDING.value
    )


@pytest.mark.asyncio
async def test_submitted_attempt_survives_expiry_and_only_owner_can_observe() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    account_ref = uuid4()
    owner = _application(database, user_id=user_id)
    sibling = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(owner, account_ref=account_ref)
    )
    lease = acquired.lease_for(owner.id)
    assert lease is not None
    assert (await _store(database, clock).claim_auth_submit(lease)).applied
    assert acquired.lease_expires_at is not None
    clock.value = acquired.lease_expires_at + timedelta(seconds=1)

    owner_result = await _store(database, clock).acquire(
        _request(owner, account_ref=account_ref)
    )
    sibling_result = await _store(database, clock).acquire(
        _request(sibling, account_ref=account_ref)
    )

    attempt = next(iter(database.attempts.values()))
    assert owner_result.decision is WorkdayGateDecision.OBSERVE_ONLY
    assert owner_result.lease_token == lease.lease_token
    assert sibling_result.decision is WorkdayGateDecision.DEFER
    assert attempt.status == "active"
    assert attempt.auth_submit_count == 1


@pytest.mark.asyncio
async def test_expired_unsubmitted_attempt_cannot_submit_and_is_reclaimed() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    account_ref = uuid4()
    owner = _application(database, user_id=user_id)
    successor = _application(database, user_id=user_id)
    acquired = await _store(database, clock).acquire(
        _request(owner, account_ref=account_ref)
    )
    lease = acquired.lease_for(owner.id)
    assert lease is not None
    assert acquired.lease_expires_at is not None
    clock.value = acquired.lease_expires_at + timedelta(seconds=1)

    expired_submit = await _store(database, clock).claim_auth_submit(lease)
    replacement = await _store(database, clock).acquire(
        _request(successor, account_ref=account_ref)
    )

    assert expired_submit.applied is False
    assert replacement.decision is WorkdayGateDecision.ALLOW
    attempts = list(database.attempts.values())
    assert [attempt.status for attempt in attempts].count("abandoned") == 1
    assert [attempt.status for attempt in attempts].count("active") == 1


@pytest.mark.asyncio
async def test_cooldown_deferral_does_not_change_application_lifecycle() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    user_id = uuid4()
    account_ref = uuid4()
    owner = _application(database, user_id=user_id)
    sibling = _application(database, user_id=user_id, status="retrying")
    acquired = await _store(database, clock).acquire(
        _request(owner, account_ref=account_ref)
    )
    lease = acquired.lease_for(owner.id)
    assert lease is not None
    await _store(database, clock).confirm_account_lock(lease)

    result = await _store(database, clock).acquire(
        _request(sibling, account_ref=account_ref)
    )

    assert result.decision is WorkdayGateDecision.DEFER
    assert sibling.status == "retrying"


@pytest.mark.asyncio
async def test_user_account_and_tenant_isolation_and_canonical_input() -> None:
    database = _MemoryDatabase()
    clock = _Clock()
    first_user = uuid4()
    second_user = uuid4()
    first = _application(database, user_id=first_user)
    second = _application(database, user_id=second_user)
    account_ref = uuid4()

    first_gate = await _store(database, clock).acquire(
        _request(first, account_ref=account_ref)
    )
    second_gate = await _store(database, clock).acquire(
        _request(second, account_ref=account_ref)
    )
    other_account_app = _application(database, user_id=first_user)
    other_account_gate = await _store(database, clock).acquire(
        _request(other_account_app, account_ref=uuid4())
    )
    other_tenant_app = _application(database, user_id=first_user)
    other_tenant_gate = await _store(database, clock).acquire(
        _request(
            other_tenant_app,
            account_ref=account_ref,
            portal_scope="workday:other:tenant",
        )
    )

    assert (
        len(
            {
                first_gate.gate_id,
                second_gate.gate_id,
                other_account_gate.gate_id,
                other_tenant_gate.gate_id,
            }
        )
        == 4
    )
    with pytest.raises(WorkdayGateOwnershipError):
        await _store(database, clock).acquire(
            WorkdayGateAcquireRequest(
                user_id=second_user,
                application_id=first.id,
                account_ref=account_ref,
                portal_scope=SCOPE,
            )
        )
    with pytest.raises(WorkdayGateStoreError):
        await _store(database, clock).acquire(
            _request(first, account_ref=account_ref, portal_scope=" Workday:wf:x ")
        )
