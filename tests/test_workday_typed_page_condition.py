from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import json
from uuid import UUID

import pytest

from services.portal_account_automation import NativeAccountPageState
from services.portal_control_resolver import PortalControlIntent
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_page_condition import WorkdayUnit1PageCondition
from services.workday_playwright_worker import PlaywrightWorkdayBrowser
from services.workday_state_observer import (
    WorkdayControlScope,
    WorkdayPageStructure,
    WorkdaySemanticControl,
    WorkdayStateObserver,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayTransitionState,
)
from services.workday_transition_engine import (
    TransitionReplayOutcome,
    TransitionReplayStatus,
)
from services.workday_unit1_orchestrator import WorkdayUnit1Orchestrator
from services.workday_worker_api import LeasedWorkdayApplication

SCOPE = "workday:wf:wellsfargojobs"
BASE_URL = "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs"


def _control(
    role: str,
    name: str = "",
    *,
    input_type: str = "",
    busy: bool = False,
    enabled: bool = True,
    scope_kind: WorkdayControlScope = WorkdayControlScope.PAGE,
) -> WorkdaySemanticControl:
    return WorkdaySemanticControl(
        role=role,
        semantic_name=name,
        input_type=input_type,
        busy=busy,
        enabled=enabled,
        scope_kind=scope_kind,
    )


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported test value: {type(value).__name__}")


@dataclass
class _StructureNodes:
    nodes: list["_StructureNode"]

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> "_StructureNode":
        return self.nodes[index]


@dataclass
class _StructureNode:
    details: dict[str, object]
    visible: bool = True
    enabled: bool = True

    async def evaluate(self, script: str) -> dict[str, object]:
        del script
        return self.details

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return self.enabled


@dataclass
class _StructureScope:
    nodes: list[_StructureNode]

    def locator(self, selector: str) -> _StructureNodes:
        del selector
        return _StructureNodes(self.nodes)


@pytest.mark.asyncio
async def test_visible_login_dialog_scopes_structure_to_dialog_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covered = _StructureNode(
        {"tag": "input", "inputType": "text", "name": "covered application field"}
    )
    dialog = _StructureScope(
        [
            _StructureNode({"tag": "input", "inputType": "email", "name": "Email"}),
            _StructureNode(
                {"tag": "input", "inputType": "password", "name": "Password"}
            ),
            _StructureNode({"tag": "button", "inputType": "", "name": "Sign In"}),
        ]
    )

    class _Page:
        url = BASE_URL

        def locator(self, selector: str) -> _StructureNodes:
            assert selector != "covered"
            return _StructureNodes([covered])

    browser = PlaywrightWorkdayBrowser(_Page())

    async def visible_dialog() -> _StructureScope:
        return dialog

    monkeypatch.setattr(browser, "_visible_account_dialog", visible_dialog)
    structure = await browser.capture_structure(limit=80)

    assert len(structure.controls) == 3
    assert all(
        control.scope_kind is WorkdayControlScope.ACTIVE_DIALOG
        for control in structure.controls
    )
    assert all(
        control.semantic_name != "covered application field"
        for control in structure.controls
    )

    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return structure

    observed = await WorkdayStateObserver(
        _Adapter(), expected_tenant_scope=SCOPE
    ).observe(include_candidates=True)
    assert observed.state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
    assert observed.candidate_metadata[0].scope_key == "active_dialog"


@pytest.mark.asyncio
async def test_disabled_dialog_sign_in_is_structure_not_executable_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _StructureScope(
        [
            _StructureNode({"tag": "input", "inputType": "email", "name": "Email"}),
            _StructureNode(
                {"tag": "input", "inputType": "password", "name": "Password"}
            ),
            _StructureNode(
                {"tag": "button", "inputType": "", "name": "Sign In"},
                enabled=False,
            ),
        ]
    )

    class _Page:
        url = BASE_URL

        def locator(self, selector: str) -> _StructureNodes:
            del selector
            return _StructureNodes([])

    browser = PlaywrightWorkdayBrowser(_Page())

    async def visible_dialog() -> _StructureScope:
        return dialog

    monkeypatch.setattr(browser, "_visible_account_dialog", visible_dialog)
    structure = await browser.capture_structure(limit=80)

    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return structure

    observed = await WorkdayStateObserver(
        _Adapter(), expected_tenant_scope=SCOPE
    ).observe(include_candidates=True)

    assert observed.state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
    assert observed.candidate_metadata == ()

    page_scoped = WorkdayPageStructure(
        BASE_URL,
        (
            _control("textbox", input_type="email"),
            _control("textbox", input_type="password"),
            _control("button", "Sign In", enabled=False),
        ),
    )

    class _PageAdapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return page_scoped

    page_observed = await WorkdayStateObserver(
        _PageAdapter(), expected_tenant_scope=SCOPE
    ).observe()
    assert page_observed.state is WorkdayTransitionState.LOGIN_FORM


@pytest.mark.asyncio
async def test_unique_full_page_login_scope_is_structurally_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [
        _StructureNode({"tag": "input", "inputType": "email", "name": "Email"}),
        _StructureNode({"tag": "input", "inputType": "password", "name": "Password"}),
        _StructureNode(
            {"tag": "button", "inputType": "", "name": "Sign In"},
            enabled=False,
        ),
    ]

    class _Page(_StructureScope):
        url = BASE_URL

    page = _Page(nodes)
    browser = PlaywrightWorkdayBrowser(page)

    async def account_scope():
        return page, WorkdayControlScope.ACTIVE_ACCOUNT_FORM

    monkeypatch.setattr(browser, "_visible_account_control_scope", account_scope)
    structure = await browser.capture_structure(limit=80)

    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return structure

    observed = await WorkdayStateObserver(
        _Adapter(), expected_tenant_scope=SCOPE
    ).observe(include_candidates=True)

    assert observed.state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
    assert observed.candidate_metadata == ()
    assert all(
        control.scope_kind is WorkdayControlScope.ACTIVE_ACCOUNT_FORM
        for control in structure.controls
    )


@pytest.mark.asyncio
async def test_exact_apply_is_prioritized_ahead_of_bounded_generic_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crowded = [
        _StructureNode({"tag": "form", "inputType": "", "name": ""}) for _ in range(80)
    ]
    apply = _StructureNode({"tag": "button", "inputType": "", "name": "Apply"})

    class _Page:
        url = BASE_URL

        def locator(self, selector: str) -> _StructureNodes:
            del selector
            return _StructureNodes(crowded)

        def get_by_role(self, role: str, *, name) -> _StructureNodes:
            if role == "button" and name.fullmatch("Apply"):
                return _StructureNodes([apply])
            return _StructureNodes([])

    page = _Page()
    browser = PlaywrightWorkdayBrowser(page)

    async def account_scope():
        return None, WorkdayControlScope.PAGE

    monkeypatch.setattr(browser, "_visible_account_control_scope", account_scope)
    structure = await browser.capture_structure(limit=80)

    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return structure

    observed = await WorkdayStateObserver(
        _Adapter(), expected_tenant_scope=SCOPE
    ).observe(include_candidates=True)

    assert len(structure.controls) == 80
    assert observed.state is WorkdayTransitionState.JOB_PAGE
    assert len(observed.candidate_metadata) == 1
    assert observed.candidate_metadata[0].intent_key == PortalControlIntent.APPLY.value


@pytest.mark.asyncio
async def test_post_sign_in_structure_can_be_pending_only_while_dialog_is_busy() -> (
    None
):
    ready_controls = (
        _control(
            "textbox",
            input_type="email",
            scope_kind=WorkdayControlScope.ACTIVE_DIALOG,
        ),
        _control(
            "textbox",
            input_type="password",
            scope_kind=WorkdayControlScope.ACTIVE_DIALOG,
        ),
        _control(
            "button",
            "Sign In",
            scope_kind=WorkdayControlScope.ACTIVE_DIALOG,
        ),
    )
    structures = [
        WorkdayPageStructure(
            BASE_URL,
            (
                *ready_controls,
                _control(
                    "status",
                    busy=True,
                    scope_kind=WorkdayControlScope.ACTIVE_DIALOG,
                ),
            ),
        ),
        WorkdayPageStructure(BASE_URL, ready_controls),
    ]

    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return structures.pop(0)

    observer = WorkdayStateObserver(_Adapter(), expected_tenant_scope=SCOPE)

    pending = await observer.observe()
    ready = await observer.observe()

    assert pending.state is WorkdayTransitionState.AUTH_OUTCOME_PENDING
    assert ready.state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        (
            (_control("textbox", input_type="one-time-code"),),
            WorkdayTransitionState.AUTH_OUTCOME_PENDING,
        ),
        (
            (_control("checkbox", "I consent to the terms and conditions"),),
            WorkdayTransitionState.AUTH_OUTCOME_PENDING,
        ),
        (
            (_control("combobox"), _control("button", "Next")),
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        ),
    ],
)
async def test_challenge_and_consent_controls_cannot_be_authenticated_ready(
    controls: tuple[WorkdaySemanticControl, ...],
    expected: WorkdayTransitionState,
) -> None:
    class _Adapter:
        async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
            del limit
            return WorkdayPageStructure(BASE_URL, controls)

    observed = await WorkdayStateObserver(
        _Adapter(), expected_tenant_scope=SCOPE
    ).observe()
    assert observed.state is expected


def test_condition_is_frozen_typed_and_private_data_free() -> None:
    condition = WorkdayUnit1PageCondition(
        native_state=NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED,
        confirmed_account_lock=True,
        trusted_unlock_time=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    payload = json.dumps(asdict(condition), default=_json_default)
    assert {
        "raw_message",
        "url",
        "email",
        "account_ref",
        "user_id",
        "selector",
        "dom_handle",
        "screenshot",
    }.isdisjoint(asdict(condition))
    assert "account_ref" not in payload


@dataclass
class _ConditionPage:
    conditions: list[WorkdayUnit1PageCondition]
    condition_calls: int = 0

    async def open_approved_job(self, target_url: str) -> None:
        del target_url

    async def capture_page_condition(self) -> WorkdayUnit1PageCondition:
        self.condition_calls += 1
        return self.conditions.pop(0)


@dataclass
class _Observed:
    states: list[object]

    async def observe(self, *, include_candidates: bool = False):
        del include_candidates
        value = self.states.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _Replay:
    async def replay(self, *, key, expected_from_state):
        del key, expected_from_state
        return TransitionReplayOutcome(
            status=TransitionReplayStatus.CURRENT_SUCCEEDED,
            version_id=UUID(int=1),
            observed_state=WorkdayTransitionState.APPLY_CHOICES,
        )


class _Unused:
    async def repair(self, request):
        raise AssertionError("repair must not run")

    async def authenticate(self, request):
        raise AssertionError("authentication must not run")


class _Persistence:
    def __init__(self) -> None:
        self.routes = []

    async def apply_route(self, *, lease, route) -> None:
        self.routes.append(route)

    async def complete_unit(self, *, lease, authentication_submitted) -> None:
        raise AssertionError("completion must not run")

    async def review_unit(self, *, lease, authentication_submitted) -> None:
        raise AssertionError("review must not run")


class _Context:
    async def auth_request(self, *, lease, portal_scope):
        raise AssertionError("vault access must not run")

    async def checkpoint_facts(self, *, lease, observation):
        raise AssertionError("checkpoint must not run")


class _Events:
    async def emit(self, event):
        raise AssertionError("events must not run")


def _lease() -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=UUID("10000000-0000-0000-0000-000000000001"),
        lease_id=UUID("10000000-0000-0000-0000-000000000002"),
        user_id=UUID("10000000-0000-0000-0000-000000000003"),
        portal="workday",
        job_url=BASE_URL + "/job/Engineer_R-1",
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=UUID("10000000-0000-0000-0000-000000000004"),
        gate_generation=1,
        gate_lease_token="opaque",
        gate_decision="allow",
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_outcome"),
    [
        (
            NativeAccountPageState.JOB_UNAVAILABLE,
            WorkdayFailureOutcome.SKIP_APPLICATION,
        ),
        (NativeAccountPageState.CAPTCHA, WorkdayFailureOutcome.USER_HOLD),
        (NativeAccountPageState.OTP, WorkdayFailureOutcome.USER_HOLD),
        (
            NativeAccountPageState.TRANSIENT_FAILURE,
            WorkdayFailureOutcome.BOUNDED_BACKOFF,
        ),
    ],
)
async def test_native_pre_submit_condition_routes_before_structural_work(
    state: NativeAccountPageState,
    expected_outcome: WorkdayFailureOutcome,
) -> None:
    page = _ConditionPage(
        [
            WorkdayUnit1PageCondition(
                native_state=state,
                pre_submit_failure={
                    NativeAccountPageState.JOB_UNAVAILABLE: WorkdayFailureClass.JOB_UNAVAILABLE,
                    NativeAccountPageState.CAPTCHA: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
                    NativeAccountPageState.OTP: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
                    NativeAccountPageState.TRANSIENT_FAILURE: WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
                }[state],
            )
        ]
    )
    observed = _Observed([])
    persistence = _Persistence()
    orchestrator = WorkdayUnit1Orchestrator(
        page=page,
        observer=observed,
        replay=_Replay(),
        repair=_Unused(),
        auth_broker=_Unused(),
        private_context=_Context(),
        persistence=persistence,
        events=_Events(),
    )

    result = await orchestrator.run(_lease())

    assert result.route is not None
    assert result.route.outcome is expected_outcome
    assert observed.states == []
    assert page.condition_calls == 1
