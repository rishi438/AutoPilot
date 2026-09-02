from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import UUID

import pytest

from services.portal_control_resolver import PortalControlIntent
from services.portal_account_automation import NativeAccountPageState
from services.workday_account_gate_store import WorkdayGateLease
from services.workday_auth_broker import (
    WorkdayAuthAccountBinding,
    WorkdayAuthBrokerRequest,
    WorkdayAuthBrokerResult,
)
from services.workday_playwright_worker import PlaywrightWorkdayBrowser
from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureOutcome,
    route_workday_failure,
)
from services.workday_state_observer import WorkdayStateSecurityError
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionKey,
    WorkdayTransitionState,
)
from services.workday_transition_engine import (
    TransitionRepairRequired,
    TransitionReplayOutcome,
    TransitionReplayStatus,
)
from services.workday_transition_repair import (
    WorkdayTransitionRepairOutcome,
    WorkdayTransitionRepairStatus,
)
from services.workday_unit1_orchestrator import (
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1Orchestrator,
    WorkdayUnit1Status,
)
from services.workday_unit1_runtime import _Persistence as _ProductionPersistence
from services.workday_worker_api import LeasedWorkdayApplication

APP_ID = UUID("10000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("10000000-0000-0000-0000-000000000002")
USER_ID = UUID("10000000-0000-0000-0000-000000000003")
GATE_ID = UUID("10000000-0000-0000-0000-000000000004")
ACCOUNT_ID = UUID("10000000-0000-0000-0000-000000000005")
VERSION_ID = UUID("10000000-0000-0000-0000-000000000006")
SCOPE = "workday:wf:wellsfargojobs"


def _lease(*, decision: str = "allow") -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=APP_ID,
        lease_id=LEASE_ID,
        user_id=USER_ID,
        portal="workday",
        job_url=(
            "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=GATE_ID,
        gate_generation=1,
        gate_lease_token="opaque-gate-token",
        gate_decision=decision,
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


def _observed(state: WorkdayTransitionState, signature: str | None = None):
    return WorkdayObservedState(
        state=state,
        safe_signature=signature or f"wds1:{state.value}",
        signature_version=1,
        portal_family="workday",
        tenant_scope=SCOPE,
    )


@dataclass
class _Page:
    opens: list[str] = field(default_factory=list)
    next_clicks: int = 0
    final_submits: int = 0

    async def open_approved_job(self, target_url: str) -> None:
        self.opens.append(target_url)


@dataclass
class _BrowserPage:
    url: str = ""
    waits: int = 0

    def __post_init__(self) -> None:
        self.context = type("Context", (), {"pages": [self]})()

    async def goto(self, url: str, **kwargs) -> None:
        assert kwargs == {"wait_until": "domcontentloaded", "timeout": 15_000}
        self.url = url

    async def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 250
        self.waits += 1


@dataclass
class _ResolverSpy:
    select_calls: int = 0
    select_repair_calls: int = 0

    async def select(self, intent, candidates):
        self.select_calls += 1
        return None

    async def select_repair(self, **kwargs):
        self.select_repair_calls += 1
        return None


@dataclass
class _Observer:
    states: list[WorkdayObservedState | Exception]
    calls: int = 0

    async def observe(self, *, include_candidates: bool = False):
        del include_candidates
        self.calls += 1
        value = self.states.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@dataclass
class _Replay:
    next_states: list[WorkdayTransitionState]
    status: TransitionReplayStatus = TransitionReplayStatus.CURRENT_SUCCEEDED
    calls: list[tuple[WorkdayTransitionState, PortalControlIntent]] = field(
        default_factory=list
    )
    repair_ticket: object | None = None

    async def replay(self, *, key, expected_from_state):
        self.calls.append((expected_from_state, key.action_intent))
        if self.repair_ticket is not None:
            ticket = self.repair_ticket
            self.repair_ticket = None
            self.next_states.pop(0)
            raise TransitionRepairRequired("mismatch", ticket=ticket)
        return TransitionReplayOutcome(
            status=self.status,
            version_id=VERSION_ID,
            observed_state=self.next_states.pop(0),
        )


@dataclass
class _Repair:
    outcome: WorkdayTransitionRepairOutcome = WorkdayTransitionRepairOutcome(
        WorkdayTransitionRepairStatus.SAFE_HOLD
    )
    calls: list[object] = field(default_factory=list)
    llm_calls: int = 0

    async def repair(self, request):
        self.calls.append(request)
        self.llm_calls += 1
        return self.outcome


@dataclass
class _Broker:
    outcome: WorkdayFailureOutcome = WorkdayFailureOutcome.COMPLETE_UNIT
    calls: list[WorkdayAuthBrokerRequest] = field(default_factory=list)
    submit_count: int = 0

    async def authenticate(self, request):
        self.calls.append(request)
        self.submit_count += 1
        failure_class = {
            WorkdayFailureOutcome.COMPLETE_UNIT: (
                WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS
            ),
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN: (
                WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED
            ),
            WorkdayFailureOutcome.CREDENTIAL_HOLD: (
                WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED
            ),
            WorkdayFailureOutcome.USER_HOLD: (
                WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP
            ),
            WorkdayFailureOutcome.REVIEW_REQUIRED: (
                WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN
            ),
        }[self.outcome]
        route = route_workday_failure(
            WorkdayFailureFacts(frozenset({failure_class}), auth_submit_count=1)
        )
        return WorkdayAuthBrokerResult(
            route=route,
            observations=1,
            provisional_success=self.outcome is WorkdayFailureOutcome.COMPLETE_UNIT,
            account_binding=(
                WorkdayAuthAccountBinding(ACCOUNT_ID)
                if self.outcome is WorkdayFailureOutcome.COMPLETE_UNIT
                else None
            ),
        )


@dataclass
class _PrivateContext:
    facts: WorkdayUnit1CheckpointFacts = WorkdayUnit1CheckpointFacts(
        approved_https_origin=True,
        canonical_tenant_verified=True,
        leased_job_context_matches=True,
        leased_application_context_matches=True,
        external_account_matches=True,
        no_login_or_auth_error=True,
        no_captcha_or_otp_or_lock=True,
        basic_information_control_hydrated=True,
    )
    vault_calls: int = 0

    async def auth_request(self, *, lease, portal_scope):
        self.vault_calls += 1
        return WorkdayAuthBrokerRequest(
            user_id=lease.user_id,
            account_ref=ACCOUNT_ID,
            portal_scope=portal_scope,
            gate_lease=WorkdayGateLease(
                gate_id=lease.gate_id,
                application_id=lease.application_id,
                generation=lease.gate_generation,
                lease_token=lease.gate_lease_token,
            ),
        )

    async def checkpoint_facts(self, *, lease, observation):
        del lease, observation
        return self.facts

    async def accept_auth_binding(self, binding):
        assert binding.account_ref == ACCOUNT_ID


@dataclass
class _Persistence:
    routes: list[object] = field(default_factory=list)
    completions: list[LeasedWorkdayApplication] = field(default_factory=list)

    async def apply_route(self, *, lease, route):
        self.routes.append((lease, route))

    async def complete_unit(self, *, lease, authentication_submitted):
        assert authentication_submitted is True
        self.completions.append(lease)

    async def review_unit(self, *, lease, authentication_submitted):
        del lease, authentication_submitted


@dataclass
class _HoldApi:
    holds: list[tuple[LeasedWorkdayApplication, str, str]] = field(default_factory=list)

    async def create_hold(self, lease, *, hold_code, remediation):
        self.holds.append((lease, hold_code, remediation))


@dataclass
class _AlreadyReviewedGate:
    review_mutation_calls: int = 0

    async def mark_review_required(self, lease):
        del lease
        self.review_mutation_calls += 1
        raise AssertionError(
            "The repair coordinator already applied the review mutation."
        )


@dataclass
class _Events:
    items: list[dict[str, object]] = field(default_factory=list)

    async def emit(self, event):
        self.items.append(event)


def _states(*, account_state=WorkdayTransitionState.ACCOUNT_PAGE):
    ready = _observed(
        WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY, "wds1:stable"
    )
    return [
        _observed(WorkdayTransitionState.JOB_PAGE),
        _observed(WorkdayTransitionState.APPLY_CHOICES),
        _observed(account_state),
        _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
        ready,
        ready,
    ]


def _harness(
    *,
    page=None,
    states=None,
    decision="allow",
    replay_status=TransitionReplayStatus.CURRENT_SUCCEEDED,
    broker_outcome=WorkdayFailureOutcome.COMPLETE_UNIT,
    facts=None,
    persistence=None,
):
    page = page or _Page()
    observer = _Observer(states or _states())
    replay = _Replay(
        [
            WorkdayTransitionState.APPLY_CHOICES,
            WorkdayTransitionState.ACCOUNT_PAGE,
            WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        ],
        status=replay_status,
    )
    repair = _Repair()
    broker = _Broker(broker_outcome)
    private = _PrivateContext(facts or _PrivateContext().facts)
    persistence = persistence or _Persistence()
    events = _Events()
    orchestrator = WorkdayUnit1Orchestrator(
        page=page,
        observer=observer,
        replay=replay,
        repair=repair,
        auth_broker=broker,
        private_context=private,
        persistence=persistence,
        events=events,
    )
    return (
        orchestrator,
        _lease(decision=decision),
        page,
        observer,
        replay,
        repair,
        broker,
        private,
        persistence,
        events,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replay_status",
    (
        TransitionReplayStatus.CURRENT_SUCCEEDED,
        TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_UPDATED,
    ),
)
async def test_real_browser_orchestrator_known_paths_never_call_resolver(
    monkeypatch: pytest.MonkeyPatch,
    replay_status: TransitionReplayStatus,
) -> None:
    browser_page = _BrowserPage()
    resolver = _ResolverSpy()
    browser = PlaywrightWorkdayBrowser(
        browser_page,
        control_resolver=resolver,
    )

    async def detect_account_state() -> NativeAccountPageState:
        return NativeAccountPageState.UNKNOWN

    async def first_visible_role(roles, name):
        assert roles == ("button", "link")
        assert name.fullmatch("Apply") is not None
        return object()

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    harness = _harness(page=browser, replay_status=replay_status)
    result = await harness[0].run(harness[1])

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert resolver.select_calls == 0
    assert resolver.select_repair_calls == 0


@pytest.mark.asyncio
async def test_current_path_reaches_checkpoint_without_llm_and_one_submit():
    orchestrator, lease, page, _, replay, repair, broker, _, persistence, events = (
        _harness()
    )

    result = await orchestrator.run(lease)

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert result.final_state is WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY
    assert result.stable_observations == 2
    assert repair.llm_calls == 0
    assert broker.submit_count == 1
    assert persistence.completions == [lease]
    assert [intent for _, intent in replay.calls] == [
        PortalControlIntent.APPLY,
        PortalControlIntent.APPLY_MANUALLY,
        PortalControlIntent.SIGN_IN,
    ]
    assert page.next_clicks == page.final_submits == 0
    assert len(events.items) == 3


@pytest.mark.asyncio
async def test_signup_page_uses_typed_existing_sign_in_transition():
    harness = _harness()
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.COMPLETE
    assert harness[4].calls[-1] == (
        WorkdayTransitionState.ACCOUNT_PAGE,
        PortalControlIntent.SIGN_IN,
    )


@pytest.mark.asyncio
async def test_previous_verified_success_is_accepted_and_emitted_for_next_user():
    harness = _harness(
        replay_status=TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_UPDATED
    )
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.COMPLETE
    assert all(
        item["decision"] == "previous_succeeded_current_updated"
        for item in harness[9].items
    )


@pytest.mark.asyncio
async def test_all_mismatches_use_one_validated_repair_then_continue():
    from services.workday_transition_contracts import WorkdayTransitionRisk
    from services.workday_transition_engine import TransitionRepairTicket

    harness = _harness()
    first_observed = _observed(WorkdayTransitionState.JOB_PAGE)
    harness[4].repair_ticket = TransitionRepairTicket(
        key=WorkdayTransitionKey(
            portal_family="workday",
            tenant_scope=SCOPE,
            task_type="open_apply",
            from_state_signature=first_observed.safe_signature,
            action_intent=PortalControlIntent.APPLY,
            signature_version=1,
            executor_policy_version=1,
        ),
        expected_from_state=WorkdayTransitionState.JOB_PAGE,
        family_id=VERSION_ID,
        expected_to_state=WorkdayTransitionState.APPLY_CHOICES,
        risk=WorkdayTransitionRisk.NAVIGATION_ONLY,
        stored_version_count=1,
    )
    harness[5].outcome = WorkdayTransitionRepairOutcome(
        WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED,
        observed_state=WorkdayTransitionState.APPLY_CHOICES,
    )

    result = await harness[0].run(harness[1])

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert harness[5].llm_calls == 1
    assert harness[9].items[0]["transition_version"] == "learned"


@pytest.mark.asyncio
async def test_repair_safe_hold_does_not_repeat_preapplied_gate_mutation():
    from services.workday_transition_contracts import WorkdayTransitionRisk
    from services.workday_transition_engine import TransitionRepairTicket

    api = _HoldApi()
    gate = _AlreadyReviewedGate()
    persistence = _ProductionPersistence(api=api, gate_store=gate)
    harness = _harness(persistence=persistence)
    first_observed = _observed(WorkdayTransitionState.JOB_PAGE)
    harness[4].repair_ticket = TransitionRepairTicket(
        key=WorkdayTransitionKey(
            portal_family="workday",
            tenant_scope=SCOPE,
            task_type="open_apply",
            from_state_signature=first_observed.safe_signature,
            action_intent=PortalControlIntent.APPLY,
            signature_version=1,
            executor_policy_version=1,
        ),
        expected_from_state=WorkdayTransitionState.JOB_PAGE,
        family_id=VERSION_ID,
        expected_to_state=WorkdayTransitionState.APPLY_CHOICES,
        risk=WorkdayTransitionRisk.NAVIGATION_ONLY,
        stored_version_count=0,
    )
    harness[5].outcome = WorkdayTransitionRepairOutcome(
        WorkdayTransitionRepairStatus.SAFE_HOLD,
        hold_applied=True,
    )

    result = await harness[0].run(harness[1])

    assert result.status is WorkdayUnit1Status.HELD
    assert result.route.outcome is WorkdayFailureOutcome.SAFE_HOLD
    assert harness[9].items[0]["transition_version"] == "not_learned"
    assert gate.review_mutation_calls == 0
    assert api.holds == [
        (
            harness[1],
            "unknown_page_state",
            "Review this bounded Workday Unit 1 blocker before retrying.",
        )
    ]


@pytest.mark.asyncio
async def test_private_identity_never_enters_shared_events():
    harness = _harness()
    await harness[0].run(harness[1])
    serialized = str(harness[9].items)
    assert str(USER_ID) not in serialized
    assert str(ACCOUNT_ID) not in serialized
    assert "opaque-gate-token" not in serialized
    assert set(harness[9].items[0]) == {
        "event",
        "transition_id",
        "transition_version",
        "from_state",
        "to_state",
        "decision",
        "duration_ms",
    }


@pytest.mark.asyncio
async def test_active_lock_defers_before_browser_vault_model_and_no_requeue():
    harness = _harness(decision="observe_only")
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert harness[2].opens == []
    assert harness[5].llm_calls == harness[7].vault_calls == 0
    assert harness[8].routes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN,
            WorkdayUnit1Status.HELD,
        ),
        (WorkdayFailureOutcome.CREDENTIAL_HOLD, WorkdayUnit1Status.HELD),
        (WorkdayFailureOutcome.USER_HOLD, WorkdayUnit1Status.HELD),
        (WorkdayFailureOutcome.REVIEW_REQUIRED, WorkdayUnit1Status.REVIEW_REQUIRED),
    ],
)
async def test_post_submit_routes_stop_without_resubmit(outcome, expected):
    harness = _harness(broker_outcome=outcome)
    result = await harness[0].run(harness[1])
    assert result.status is expected
    assert harness[6].submit_count == 1
    assert harness[8].completions == []


@pytest.mark.asyncio
async def test_wrong_origin_routes_security_hold_without_catalog_mutation(caplog):
    caplog.set_level("INFO", logger="services.workday_unit1_orchestrator")
    harness = _harness(states=[WorkdayStateSecurityError()])
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.HELD
    assert result.route.outcome is WorkdayFailureOutcome.SECURITY_HOLD
    assert harness[4].calls == []
    assert harness[5].llm_calls == 0
    assert (
        "workday_unit1_failure_routed failure_class=wrong_origin_or_tenant "
        "outcome=security_hold gate_operation=mark_review_required"
    ) in caplog.messages


@pytest.mark.asyncio
async def test_persistent_login_form_logs_each_value_free_hydration_state(caplog):
    caplog.set_level("INFO", logger="services.workday_unit1_orchestrator")
    login_form = _observed(WorkdayTransitionState.LOGIN_FORM)
    harness = _harness(states=[login_form] * 5)

    result = await harness[0].run(harness[1])

    assert result.status is WorkdayUnit1Status.HELD
    assert "workday_unit1_state_ready phase=initial state=login_form" in caplog.messages
    hydration_messages = [
        message
        for message in caplog.messages
        if "phase=login_form_hydration" in message
    ]
    assert len(hydration_messages) == 4
    assert all(message.endswith("state=login_form") for message in hydration_messages)


@pytest.mark.asyncio
async def test_account_page_uses_sign_in_transition_without_job_skip():
    harness = _harness(
        states=[
            _observed(WorkdayTransitionState.ACCOUNT_PAGE),
            _observed(WorkdayTransitionState.APPLY_CHOICES),
            _observed(WorkdayTransitionState.ACCOUNT_PAGE),
            _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
            _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY),
        ]
    )
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.COMPLETE
    assert harness[4].calls[-1] == (
        WorkdayTransitionState.ACCOUNT_PAGE,
        PortalControlIntent.SIGN_IN,
    )
    assert harness[5].llm_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_fact",
    [
        "leased_job_context_matches",
        "external_account_matches",
        "no_login_or_auth_error",
        "basic_information_control_hydrated",
    ],
)
async def test_incomplete_context_cannot_satisfy_final_checkpoint(failed_fact):
    facts = asdict(_PrivateContext().facts)
    facts[failed_fact] = False
    harness = _harness(facts=WorkdayUnit1CheckpointFacts(**facts))
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert harness[8].completions == []


@pytest.mark.asyncio
async def test_unstable_hydration_cannot_satisfy_final_checkpoint():
    states = _states()
    states[-1] = _observed(
        WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY, "wds1:changed"
    )
    harness = _harness(states=states)
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert harness[8].completions == []


@pytest.mark.asyncio
async def test_evolving_hydration_signatures_update_baseline_and_succeed_when_stable():
    states = _states()
    # Replace final state with 3 states: sig1 -> sig2 -> sig2
    states[-1] = _observed(
        WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY, "wds1:partial"
    )
    states.append(
        _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY, "wds1:full")
    )
    states.append(
        _observed(WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY, "wds1:full")
    )
    harness = _harness(states=states)
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.COMPLETE
    assert harness[8].completions == [harness[1]]


@pytest.mark.asyncio
async def test_checkpoint_retries_transient_observation_error_until_hydrated():
    from services.workday_state_observer import WorkdayStateObservationError

    states = _states()
    # Introduce a transient observation error between observations
    states.insert(
        -1,
        WorkdayStateObservationError("Transitory hydration state"),
    )
    states.append(
        _observed(
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            "wds1:stable",
        )
    )
    harness = _harness(states=states)
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.COMPLETE
    assert harness[8].completions == [harness[1]]


@pytest.mark.asyncio
async def test_checkpoint_security_error_fails_immediately():
    states = _states()
    states[-1] = WorkdayStateSecurityError()
    harness = _harness(states=states)
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert harness[8].completions == []


@pytest.mark.asyncio
async def test_checkpoint_unexpected_exception_fails_immediately():
    states = _states()
    states[-1] = RuntimeError("Unexpected DOM explosion")
    harness = _harness(states=states)
    result = await harness[0].run(harness[1])
    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert harness[8].completions == []
