from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID

import pytest

from services.portal_account_automation import NativeAccountPageState
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_auth_broker import WorkdayAuthAccountBinding
from services.workday_page_condition import WorkdayUnit1PageCondition
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionState,
)
from services.workday_transition_engine import (
    TransitionReplayOutcome,
    TransitionReplayStatus,
)
from services.workday_unit1_orchestrator import (
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1Orchestrator,
    WorkdayUnit1Status,
)
from services.workday_worker_api import LeasedWorkdayApplication

BASE_URL = "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs"
SCOPE = "workday:wf:wellsfargojobs"


def _lease() -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=UUID("20000000-0000-0000-0000-000000000001"),
        lease_id=UUID("20000000-0000-0000-0000-000000000002"),
        user_id=UUID("20000000-0000-0000-0000-000000000003"),
        portal="workday",
        job_url=BASE_URL + "/job/Engineer_R-1",
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=UUID("20000000-0000-0000-0000-000000000004"),
        gate_generation=1,
        gate_lease_token="opaque-gate-token",
        gate_decision="allow",
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


def _observed(state: WorkdayTransitionState, signature: str = "stable"):
    return WorkdayObservedState(
        state=state,
        safe_signature=f"wds1:{signature}",
        signature_version=1,
        portal_family="workday",
        tenant_scope=SCOPE,
    )


def _condition(
    state: NativeAccountPageState = NativeAccountPageState.UNKNOWN,
    failure: WorkdayFailureClass | None = None,
    *,
    authenticated: bool = False,
) -> WorkdayUnit1PageCondition:
    return WorkdayUnit1PageCondition(
        native_state=state,
        pre_submit_failure=failure,
        already_authenticated=authenticated,
    )


@dataclass
class _Page:
    conditions: list[WorkdayUnit1PageCondition] = field(default_factory=list)
    opens: int = 0
    waits: list[int] = field(default_factory=list)

    async def open_approved_job(self, target_url: str):
        del target_url
        self.opens += 1
        return None

    async def capture_page_condition(self):
        return self.conditions.pop(0)

    async def wait_for_hydration(self, milliseconds: int):
        self.waits.append(milliseconds)


@dataclass
class _Observer:
    states: list[WorkdayObservedState]
    calls: int = 0

    async def observe(self, *, include_candidates: bool = False):
        del include_candidates
        self.calls += 1
        return self.states.pop(0)


@dataclass
class _Replay:
    status: TransitionReplayStatus = TransitionReplayStatus.CURRENT_SUCCEEDED
    next_state: WorkdayTransitionState = WorkdayTransitionState.APPLY_CHOICES
    calls: int = 0

    async def replay(self, *, key, expected_from_state):
        del key, expected_from_state
        self.calls += 1
        return TransitionReplayOutcome(
            status=self.status,
            version_id=UUID(int=1),
            observed_state=self.next_state,
        )


@dataclass
class _Broker:
    calls: int = 0

    async def authenticate(self, request):
        del request
        self.calls += 1
        return SimpleNamespace(
            route=SimpleNamespace(outcome=WorkdayFailureOutcome.COMPLETE_UNIT),
            provisional_success=True,
            account_binding=WorkdayAuthAccountBinding(
                UUID("20000000-0000-0000-0000-000000000005")
            ),
        )


@dataclass
class _Context:
    facts: WorkdayUnit1CheckpointFacts
    vault_calls: int = 0

    async def auth_request(self, *, lease, portal_scope):
        del lease, portal_scope
        self.vault_calls += 1
        return object()

    async def checkpoint_facts(self, *, lease, observation):
        del lease, observation
        return self.facts

    async def accept_auth_binding(self, binding):
        assert binding.account_ref == UUID("20000000-0000-0000-0000-000000000005")


@dataclass
class _Persistence:
    routes: list[object] = field(default_factory=list)
    completions: int = 0
    completion_modes: list[bool] = field(default_factory=list)

    async def apply_route(self, *, lease, route):
        del lease
        self.routes.append(route)

    async def complete_unit(self, *, lease, authentication_submitted):
        del lease
        self.completions += 1
        self.completion_modes.append(authentication_submitted)

    async def review_unit(self, *, lease, authentication_submitted):
        del lease, authentication_submitted


class _UnusedRepair:
    async def repair(self, request):
        raise AssertionError(f"repair must not run: {request}")


class _Events:
    async def emit(self, event):
        del event


def _facts(*, bound: bool = True) -> WorkdayUnit1CheckpointFacts:
    return WorkdayUnit1CheckpointFacts(
        approved_https_origin=True,
        canonical_tenant_verified=True,
        leased_job_context_matches=True,
        leased_application_context_matches=True,
        external_account_matches=bound,
        no_login_or_auth_error=True,
        no_captcha_or_otp_or_lock=True,
        basic_information_control_hydrated=True,
    )


def _orchestrator(page, observer, replay, broker, context, persistence):
    return WorkdayUnit1Orchestrator(
        page=page,
        observer=observer,
        replay=replay,
        repair=_UnusedRepair(),
        auth_broker=broker,
        private_context=context,
        persistence=persistence,
        events=_Events(),
    )


@pytest.mark.asyncio
async def test_existing_authenticated_session_skips_authentication_and_vault():
    page = _Page([_condition(NativeAccountPageState.AUTHENTICATED, authenticated=True)])
    observer = _Observer(
        [
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
        ]
    )
    broker = _Broker()
    context = _Context(_facts())
    persistence = _Persistence()

    result = await _orchestrator(
        page, observer, _Replay(), broker, context, persistence
    ).run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert broker.calls == context.vault_calls == 0
    assert persistence.completions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_state",
    (WorkdayTransitionState.JOB_PAGE, WorkdayTransitionState.APPLY_CHOICES),
)
async def test_direct_authenticated_session_after_navigation_is_terminal(
    initial_state: WorkdayTransitionState,
):
    page = _Page(
        [
            _condition(),
            _condition(NativeAccountPageState.AUTHENTICATED, authenticated=True),
        ]
    )
    observer = _Observer(
        [
            _observed(initial_state),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
        ]
    )
    replay = _Replay(
        status=TransitionReplayStatus.DIRECT_AUTHENTICATED_SESSION,
        next_state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
    )
    broker = _Broker()
    context = _Context(_facts())
    persistence = _Persistence()

    result = await _orchestrator(
        page, observer, replay, broker, context, persistence
    ).run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert replay.calls == 1
    assert broker.calls == context.vault_calls == 0
    assert persistence.completions == 1


@pytest.mark.asyncio
async def test_login_form_hydrates_read_only_then_authenticates_once():
    page = _Page([_condition(), _condition()])
    observer = _Observer(
        [
            _observed(WorkdayTransitionState.LOGIN_FORM),
            _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
        ]
    )
    broker = _Broker()
    context = _Context(_facts())
    persistence = _Persistence()

    result = await _orchestrator(
        page, observer, _Replay(), broker, context, persistence
    ).run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert broker.calls == context.vault_calls == 1
    assert page.waits == [250]


@pytest.mark.asyncio
async def test_login_form_timeout_is_one_safe_hold_without_action():
    page = _Page([_condition()] + [_condition() for _ in range(4)])
    observer = _Observer(
        [_observed(WorkdayTransitionState.LOGIN_FORM) for _ in range(5)]
    )
    replay = _Replay()
    broker = _Broker()
    context = _Context(_facts())
    persistence = _Persistence()

    result = await _orchestrator(
        page, observer, replay, broker, context, persistence
    ).run(_lease())

    assert result.status is WorkdayUnit1Status.HELD
    assert result.route is not None
    assert result.route.outcome is WorkdayFailureOutcome.SAFE_HOLD
    assert replay.calls == broker.calls == context.vault_calls == 0
    assert len(persistence.routes) == 1
    assert page.waits == [250, 250, 250]


@pytest.mark.asyncio
async def test_hydration_challenge_uses_exact_route_without_authentication():
    page = _Page(
        [
            _condition(),
            _condition(
                NativeAccountPageState.CAPTCHA,
                WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            ),
        ]
    )
    observer = _Observer([_observed(WorkdayTransitionState.LOGIN_FORM)])
    broker = _Broker()
    context = _Context(_facts())
    persistence = _Persistence()

    result = await _orchestrator(
        page, observer, _Replay(), broker, context, persistence
    ).run(_lease())

    assert result.route is not None
    assert result.route.outcome is WorkdayFailureOutcome.USER_HOLD
    assert broker.calls == context.vault_calls == 0
