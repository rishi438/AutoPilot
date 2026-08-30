from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from services.portal_credentials import WorkerPortalCredential
from services.workday_account_gate_store import (
    WorkdayAuthGateBinding,
    WorkdayGateLease,
    WorkdayGateMutation,
)
from services.workday_auth_broker import (
    WorkdayAuthBroker,
    WorkdayAuthBrokerError,
    WorkdayAuthBrokerRequest,
    WorkdayPostSubmitObservation,
)
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionState,
)

USER_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_USER_ID = UUID("10000000-0000-0000-0000-000000000002")
ACCOUNT_REF = UUID("20000000-0000-0000-0000-000000000001")
APPLICATION_ID = UUID("30000000-0000-0000-0000-000000000001")
GATE_ID = UUID("40000000-0000-0000-0000-000000000001")
SCOPE = "workday:wf:wellsfargojobs"
FIXTURE_VALUE = "fixture-only-auth-value"


class SimulatedCrash(BaseException):
    pass


def _lease() -> WorkdayGateLease:
    return WorkdayGateLease(
        gate_id=GATE_ID,
        application_id=APPLICATION_ID,
        generation=7,
        lease_token="opaque-lease-authority",
    )


def _request(**overrides: object) -> WorkdayAuthBrokerRequest:
    values = {
        "user_id": USER_ID,
        "account_ref": ACCOUNT_REF,
        "portal_scope": SCOPE,
        "gate_lease": _lease(),
    }
    values.update(overrides)
    return WorkdayAuthBrokerRequest(**values)


def _state(
    state: WorkdayTransitionState = WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
    *,
    scope: str = SCOPE,
) -> WorkdayObservedState:
    return WorkdayObservedState(
        state=state,
        safe_signature="wds1:fixture",
        signature_version=1,
        portal_family="workday",
        tenant_scope=scope,
    )


@dataclass
class _Gate:
    expected_binding: WorkdayAuthGateBinding = field(
        default_factory=lambda: WorkdayAuthGateBinding(
            user_id=USER_ID, account_ref=ACCOUNT_REF, portal_scope=SCOPE
        )
    )
    events: list[str] = field(default_factory=list)
    secret_claimed: bool = False
    submit_claimed: bool = False
    crash_after_secret_claim: bool = False
    crash_after_submit_claim: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def verify_auth_lease(
        self, lease: WorkdayGateLease, binding: WorkdayAuthGateBinding
    ) -> bool:
        self.events.append("gate_verified")
        return lease == _lease() and binding == self.expected_binding

    async def mark_secret_accessed(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        assert lease == _lease()
        async with self._lock:
            if self.secret_claimed:
                return WorkdayGateMutation(applied=False, stale=True)
            self.secret_claimed = True
            self.events.append("secret_claimed")
            if self.crash_after_secret_claim:
                self.crash_after_secret_claim = False
                raise SimulatedCrash
            return WorkdayGateMutation(applied=True)

    async def claim_auth_submit(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        assert lease == _lease()
        async with self._lock:
            if self.submit_claimed:
                return WorkdayGateMutation(applied=False, stale=True)
            self.submit_claimed = True
            self.events.append("submit_claimed_pending")
            if self.crash_after_submit_claim:
                self.crash_after_submit_claim = False
                raise SimulatedCrash
            return WorkdayGateMutation(applied=True)

    async def complete_success(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        self.events.append("complete_success")
        return WorkdayGateMutation(applied=True)

    async def confirm_account_lock(
        self, lease: WorkdayGateLease, *, trusted_portal_until=None
    ) -> WorkdayGateMutation:
        del trusted_portal_until
        self.events.append("confirm_account_lock")
        return WorkdayGateMutation(applied=True)

    async def mark_review_required(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        self.events.append("review_required")
        return WorkdayGateMutation(applied=True)


@dataclass
class _CredentialReader:
    events: list[str]
    reads: int = 0
    crash: bool = False
    error: Exception | None = None

    async def credential_for_auth_broker(
        self, *, user_id: str, account_ref: UUID, portal_scope: str
    ) -> WorkerPortalCredential | None:
        assert user_id == str(USER_ID)
        assert account_ref == ACCOUNT_REF
        assert portal_scope == SCOPE
        self.reads += 1
        self.events.append("vault_read")
        if self.crash:
            self.crash = False
            raise SimulatedCrash
        if self.error is not None:
            raise self.error
        return WorkerPortalCredential(
            credential_id=str(ACCOUNT_REF),
            portal_scope=SCOPE,
            account_email="fixture@example.invalid",
            status="active",
            password=FIXTURE_VALUE,
        )


@dataclass
class _PageActions:
    events: list[str]
    unique: bool = True
    clicks: int = 0
    crash_after_click: bool = False

    async def verify_unique_auth_controls(self, *, expected_scope: str) -> bool:
        assert expected_scope == SCOPE
        self.events.append("controls_verified")
        return self.unique

    async def fill_verified_auth_controls(
        self, credential: WorkerPortalCredential
    ) -> None:
        assert credential.password == FIXTURE_VALUE
        self.events.append("controls_filled")

    async def click_verified_sign_in(self) -> None:
        self.clicks += 1
        self.events.append("physical_click")
        if self.crash_after_click:
            self.crash_after_click = False
            raise SimulatedCrash


@dataclass
class _StateObserver:
    observed: WorkdayObservedState = field(default_factory=_state)
    calls: int = 0

    async def observe(self, *, include_candidates: bool = False):
        assert include_candidates is False
        self.calls += 1
        return self.observed


@dataclass
class _OutcomeObserver:
    observations: list[WorkdayPostSubmitObservation]
    calls: int = 0

    async def observe_post_submit(self) -> WorkdayPostSubmitObservation:
        self.calls += 1
        return self.observations[min(self.calls - 1, len(self.observations) - 1)]


def _success() -> WorkdayPostSubmitObservation:
    return WorkdayPostSubmitObservation(
        frozenset({WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS})
    )


def _broker(
    *,
    gate: _Gate | None = None,
    events: list[str] | None = None,
    page: _PageActions | None = None,
    state: WorkdayObservedState | None = None,
    outcomes: list[WorkdayPostSubmitObservation] | None = None,
    max_observations: int = 3,
) -> tuple[WorkdayAuthBroker, _Gate, _CredentialReader, _PageActions, _OutcomeObserver]:
    shared_events = events if events is not None else []
    gate = gate or _Gate(events=shared_events)
    reader = _CredentialReader(shared_events)
    page = page or _PageActions(shared_events)
    outcome_observer = _OutcomeObserver(outcomes or [_success()])

    async def no_wait(_: float) -> None:
        return None

    broker = WorkdayAuthBroker(
        gate_store=gate,
        credential_reader=reader,
        page_actions=page,
        state_observer=_StateObserver(state or _state()),
        outcome_observer=outcome_observer,
        max_observations=max_observations,
        poll_interval_seconds=0,
        poll_wait=no_wait,
    )
    return broker, gate, reader, page, outcome_observer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,page_unique",
    [
        (_state(WorkdayTransitionState.LOGIN_FORM), True),
        (_state(scope="workday:other:site"), True),
        (_state(), False),
    ],
)
async def test_untrusted_or_nonunique_form_fails_before_vault(
    state: WorkdayObservedState, page_unique: bool
) -> None:
    events: list[str] = []
    page = _PageActions(events, unique=page_unique)
    broker, gate, reader, _, _ = _broker(events=events, page=page, state=state)

    with pytest.raises(WorkdayAuthBrokerError):
        await broker.authenticate(_request())

    assert reader.reads == 0
    assert gate.secret_claimed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        WorkdayAuthGateBinding(
            user_id=OTHER_USER_ID,
            account_ref=ACCOUNT_REF,
            portal_scope=SCOPE,
        ),
        WorkdayAuthGateBinding(
            user_id=USER_ID,
            account_ref=ACCOUNT_REF,
            portal_scope="workday:other:site",
        ),
    ],
)
async def test_wrong_owner_or_tenant_gate_binding_fails_before_vault(
    binding: WorkdayAuthGateBinding,
) -> None:
    gate = _Gate(expected_binding=binding)
    broker, gate, reader, _, _ = _broker(gate=gate)

    with pytest.raises(WorkdayAuthBrokerError, match="authority is stale"):
        await broker.authenticate(_request())

    assert reader.reads == 0
    assert gate.secret_claimed is False


@pytest.mark.asyncio
async def test_durable_claims_precede_one_vault_read_and_one_click() -> None:
    events: list[str] = []
    broker, gate, reader, page, _ = _broker(events=events)

    result = await broker.authenticate(_request())

    assert result.route.outcome is WorkdayFailureOutcome.COMPLETE_UNIT
    assert result.provisional_success is True
    assert result.account_binding is not None
    assert result.account_binding.account_ref == ACCOUNT_REF
    assert events == [
        "controls_verified",
        "gate_verified",
        "secret_claimed",
        "vault_read",
        "controls_filled",
        "submit_claimed_pending",
        "physical_click",
    ]
    assert reader.reads == page.clicks == 1
    assert gate.secret_claimed is gate.submit_claimed is True


@pytest.mark.asyncio
async def test_concurrent_invocation_reads_and_submits_at_most_once() -> None:
    broker, gate, reader, page, _ = _broker()

    results = await asyncio.gather(
        broker.authenticate(_request()),
        broker.authenticate(_request()),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert reader.reads == 1
    assert page.clicks == 1
    assert gate.submit_claimed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_point", ["after_secret_claim", "after_submit_claim", "after_click"]
)
async def test_crash_at_durable_boundaries_cannot_resubmit(crash_point: str) -> None:
    gate = _Gate()
    page = _PageActions(gate.events)
    gate.crash_after_secret_claim = crash_point == "after_secret_claim"
    gate.crash_after_submit_claim = crash_point == "after_submit_claim"
    page.crash_after_click = crash_point == "after_click"
    broker, gate, reader, page, _ = _broker(gate=gate, page=page)

    with pytest.raises(SimulatedCrash):
        await broker.authenticate(_request())
    first_clicks = page.clicks

    with pytest.raises(WorkdayAuthBrokerError):
        await broker.authenticate(_request())

    assert page.clicks == first_clicks
    assert page.clicks <= 1
    assert reader.reads <= 1
    if crash_point != "after_secret_claim":
        assert gate.submit_claimed is True


@pytest.mark.asyncio
async def test_crash_after_secret_claim_before_vault_blocks_later_read() -> None:
    broker, gate, reader, page, _ = _broker()
    reader.crash = True

    with pytest.raises(SimulatedCrash):
        await broker.authenticate(_request())
    with pytest.raises(WorkdayAuthBrokerError):
        await broker.authenticate(_request())

    assert reader.reads == 1
    assert page.clicks == 0
    assert gate.secret_claimed is True


@pytest.mark.asyncio
async def test_timeout_after_claim_routes_to_review_without_retry() -> None:
    pending = WorkdayPostSubmitObservation(frozenset(), terminal=False)
    broker, gate, _, page, observer = _broker(outcomes=[pending], max_observations=3)

    result = await broker.authenticate(_request())

    assert result.route.outcome is WorkdayFailureOutcome.REVIEW_REQUIRED
    assert result.route.authentication_resubmit_requested is False
    assert result.observations == observer.calls == 3
    assert gate.events[-1] == "review_required"
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_observation_window_has_no_page_mutation_surface() -> None:
    pending = WorkdayPostSubmitObservation(frozenset(), terminal=False)
    broker, _, _, page, observer = _broker(
        outcomes=[pending, _success()], max_observations=2
    )

    result = await broker.authenticate(_request())

    assert result.route.outcome is WorkdayFailureOutcome.COMPLETE_UNIT
    assert observer.calls == 2
    assert page.clicks == 1
    assert not any(
        hasattr(observer, name) for name in ("click", "reload", "navigate", "submit")
    )


@pytest.mark.asyncio
async def test_credential_value_never_enters_results_events_or_exceptions() -> None:
    events: list[str] = []
    broker, gate, _, page, _ = _broker(events=events)

    result = await broker.authenticate(_request())
    rendered = repr((result, events, gate, page))

    assert FIXTURE_VALUE not in rendered
    assert FIXTURE_VALUE not in repr(_request())

    failing_broker, _, failing_reader, _, _ = _broker()
    failing_reader.error = RuntimeError(FIXTURE_VALUE)
    with pytest.raises(WorkdayAuthBrokerError) as exc_info:
        await failing_broker.authenticate(_request())
    assert FIXTURE_VALUE not in str(exc_info.value)
    assert exc_info.value.__context__ is None


def test_existing_worker_has_no_direct_login_fill_or_submit_bypass() -> None:
    from services import workday_playwright_worker

    worker_source = inspect.getsource(
        workday_playwright_worker.WorkdayAccountGateWorker
    )
    browser_protocol = inspect.getsource(workday_playwright_worker.WorkdayBrowser)

    assert "_auth_broker.authenticate" in worker_source
    assert "fill_login" not in worker_source
    assert "submit_login" not in worker_source
    assert "fill_login" not in browser_protocol
    assert "submit_login" not in browser_protocol
