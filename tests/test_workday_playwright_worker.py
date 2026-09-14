from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import re
from types import SimpleNamespace
from typing import Literal
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from api.automation import (
    AccountStateRequest,
    lease_next_application,
    record_account_state,
    reset_application_lease,
)
from models.database import ApplicationAutomationEvent, JobApplication
from services.portal_control_resolver import (
    PortalControlCandidate,
    PortalControlIntent,
    PortalControlSelection,
)
from services.portal_account_automation import (
    NativeAccountAction,
    NativeAccountPageState,
    NativeAccountPlan,
)
from services.portal_credentials import WorkerPortalCredential
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_playwright_worker import (
    PlaywrightWorkdayBrowser,
    WorkdayAccountGateStatus,
    WorkdayAccountGateWorker,
    WorkdayAccountSignals,
    WorkdayFormAssignment,
    WorkdayLease,
    WorkdayWorkerError,
    classify_workday_account_state,
    extract_trusted_workday_unlock_time,
)
from services.workday_state_observer import WorkdayControlScope


@dataclass
class _ApplyAction:
    clicks: int = 0

    async def click(self, *, timeout: int) -> None:
        assert timeout == 15_000
        self.clicks += 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True


@dataclass
class _CaptchaWidget:
    active: bool
    visible: bool = True

    async def is_visible(self) -> bool:
        return self.visible

    async def evaluate(self, script: str) -> bool:
        assert "grecaptcha-badge" in script
        assert "data-size='invisible'" in script
        return self.active


@dataclass
class _CaptchaWidgets:
    items: list[_CaptchaWidget]

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> _CaptchaWidget:
        return self.items[index]


@dataclass
class _SelectionResolver:
    selection: PortalControlSelection | None

    async def select(self, intent, candidates):
        return self.selection


@dataclass
class _SequenceResolver:
    selections: list[PortalControlSelection | None]
    calls: int = 0

    async def select(self, intent, candidates):
        self.calls += 1
        return self.selections.pop(0)


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
class _HydratingPage:
    url: str = ""
    waits: int = 0

    def __post_init__(self) -> None:
        self.context = type("Context", (), {"pages": [self]})()

    async def goto(self, url: str, **kwargs) -> None:
        assert kwargs == {"wait_until": "domcontentloaded", "timeout": 15_000}
        self.url = url

    async def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds in {250, 500}
        self.waits += 1


@pytest.mark.asyncio
async def test_playwright_browser_waits_for_workday_spa_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage()
    action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)
    apply_checks = 0

    async def detect_account_state() -> NativeAccountPageState:
        return (
            NativeAccountPageState.LOGIN_REQUIRED
            if action.clicks
            else NativeAccountPageState.UNKNOWN
        )

    async def first_visible_role(*args) -> _ApplyAction | None:
        nonlocal apply_checks
        apply_checks += 1
        return action if apply_checks == 3 else None

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    await browser.open_and_start_apply(
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Test_R-1"
    )

    assert apply_checks == 3
    assert page.waits == 3
    assert action.clicks == 1


@pytest.mark.asyncio
async def test_open_approved_job_uses_deterministic_readiness_with_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage()
    resolver = _ResolverSpy()
    browser = PlaywrightWorkdayBrowser(page, control_resolver=resolver)
    action = _ApplyAction()

    async def detect_account_state() -> NativeAccountPageState:
        return NativeAccountPageState.UNKNOWN

    async def first_visible_role(roles, name):
        assert roles == ("button", "link")
        assert name.fullmatch("Apply") is not None
        return action

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    await browser.open_approved_job(
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Test_R-1"
    )

    assert resolver.select_calls == 0
    assert resolver.select_repair_calls == 0


@pytest.mark.asyncio
async def test_ambient_captcha_widget_is_not_an_active_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = _CaptchaWidget(active=False)

    class _Page:
        def locator(self, selector: str) -> _CaptchaWidgets:
            assert selector == PlaywrightWorkdayBrowser._CAPTCHA_WIDGET_SELECTOR
            return _CaptchaWidgets([widget])

    browser = PlaywrightWorkdayBrowser(_Page())

    async def has_visible(selector: str) -> bool:
        assert selector == PlaywrightWorkdayBrowser._CAPTCHA_INPUT_SELECTOR
        return False

    monkeypatch.setattr(browser, "_has_visible", has_visible)

    assert await browser._has_active_captcha() is False


@pytest.mark.asyncio
async def test_rendered_captcha_surface_remains_an_active_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_widget = _CaptchaWidget(active=False)
    challenge = _CaptchaWidget(active=True)

    class _Page:
        def locator(self, selector: str) -> _CaptchaWidgets:
            assert selector == PlaywrightWorkdayBrowser._CAPTCHA_WIDGET_SELECTOR
            return _CaptchaWidgets([hidden_widget, challenge])

    browser = PlaywrightWorkdayBrowser(_Page())

    async def has_visible(selector: str) -> bool:
        assert selector == PlaywrightWorkdayBrowser._CAPTCHA_INPUT_SELECTOR
        return False

    monkeypatch.setattr(browser, "_has_visible", has_visible)

    assert await browser._has_active_captcha() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (
        NativeAccountPageState.CAPTCHA,
        NativeAccountPageState.OTP,
        NativeAccountPageState.LOGIN_REQUIRED,
    ),
)
async def test_open_approved_job_terminal_readiness_never_invokes_resolver(
    monkeypatch: pytest.MonkeyPatch,
    state: NativeAccountPageState,
) -> None:
    page = _HydratingPage()
    resolver = _ResolverSpy()
    browser = PlaywrightWorkdayBrowser(page, control_resolver=resolver)

    async def detect_account_state() -> NativeAccountPageState:
        return state

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    await browser.open_approved_job(
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Test_R-1"
    )

    assert resolver.select_calls == 0
    assert resolver.select_repair_calls == 0


@pytest.mark.asyncio
async def test_application_form_scan_waits_for_post_login_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/recruiting/wf/site/apply")
    browser = PlaywrightWorkdayBrowser(page)
    checks = 0

    async def has_visible(selector: str) -> bool:
        nonlocal checks
        assert "input:not([type='hidden'])" in selector
        checks += 1
        return checks == 3

    monkeypatch.setattr(browser, "_has_visible", has_visible)

    ready = await browser._wait_for_application_form_controls(
        "input:not([type='hidden'])"
    )

    assert ready is True
    assert checks == 3
    assert page.waits == 2


@pytest.mark.asyncio
async def test_open_login_waits_for_confirmed_existing_account_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(
        url="https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/apply"
    )
    action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)
    states = [
        NativeAccountPageState.REGISTRATION_REQUIRED,
        NativeAccountPageState.LOGIN_REQUIRED,
    ]

    async def resolve_action(intent, roles, deterministic_name):
        assert intent is PortalControlIntent.SIGN_IN
        assert roles == ("button", "link")
        assert deterministic_name.fullmatch("Sign In")
        return action

    async def detect_account_state() -> NativeAccountPageState:
        return states.pop(0)

    monkeypatch.setattr(browser, "_resolve_action", resolve_action)
    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    await browser.open_login()

    assert action.clicks == 1
    assert states == []
    assert page.waits == 1


@pytest.mark.asyncio
async def test_sign_in_candidate_waits_past_transient_account_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(
        url="https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/apply"
    )
    action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)
    raw_candidate_id = "candidate-0"
    candidate = SimpleNamespace(
        accessible_name="Sign In",
        text="",
        role="button",
        tag="button",
        candidate_id=raw_candidate_id,
    )
    states = [
        NativeAccountPageState.REGISTRATION_REQUIRED,
        NativeAccountPageState.UNKNOWN,
        NativeAccountPageState.LOGIN_REQUIRED,
    ]

    async def visible_account_dialog():
        return None

    async def visible_action_candidates(scope):
        assert scope is None
        return [candidate], {raw_candidate_id: action}

    async def detect_account_state() -> NativeAccountPageState:
        return states.pop(0)

    monkeypatch.setattr(browser, "_visible_account_dialog", visible_account_dialog)
    monkeypatch.setattr(
        browser, "_visible_action_candidates", visible_action_candidates
    )
    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    digest = hashlib.sha256(
        f"button:{PortalControlIntent.SIGN_IN.value}:0".encode("ascii")
    ).hexdigest()[:16]
    await browser.execute_candidate(
        candidate_id=f"wdc-{digest}",
        action_intent=PortalControlIntent.SIGN_IN,
    )

    assert action.clicks == 1
    assert states == []
    assert page.waits == 2


@pytest.mark.asyncio
async def test_playwright_browser_uses_apply_manually_before_account_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage()
    apply_action = _ApplyAction()
    manual_action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)

    async def detect_account_state() -> NativeAccountPageState:
        return (
            NativeAccountPageState.LOGIN_REQUIRED
            if manual_action.clicks
            else NativeAccountPageState.UNKNOWN
        )

    async def first_visible_role(roles, name) -> _ApplyAction:
        return manual_action if "manually" in name.pattern else apply_action

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    await browser.open_and_start_apply(
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Test_R-1"
    )

    assert apply_action.clicks == 1
    assert manual_action.clicks == 1


@pytest.mark.asyncio
async def test_playwright_browser_rejects_delayed_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(url="https://community.workday.com/maintenance-page")
    browser = PlaywrightWorkdayBrowser(page, timeout_ms=1)

    async def detect_account_state() -> NativeAccountPageState:
        return NativeAccountPageState.UNKNOWN

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    with pytest.raises(WorkdayWorkerError, match="approved HTTPS origin"):
        await browser._wait_for_account_state()


@pytest.mark.asyncio
async def test_registration_submit_waits_for_confirmed_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(
        url=(
            "https://wd1.myworkdaysite.com/recruiting/wf/"
            "WellsFargoJobs/job/Test_R-1/apply/applyManually"
        )
    )
    action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)
    states = [
        NativeAccountPageState.REGISTRATION_REQUIRED,
        NativeAccountPageState.REGISTRATION_REQUIRED,
        NativeAccountPageState.AUTHENTICATED,
    ]

    async def first_visible_role(roles, name) -> _ApplyAction:
        return action

    async def detect_account_state() -> NativeAccountPageState:
        return states.pop(0)

    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)
    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    await browser.submit_registration()

    assert action.clicks == 1
    assert states == []
    assert page.waits == 2


class _NavigationContextError(Exception):
    __module__ = "playwright._impl._errors"


@pytest.mark.asyncio
async def test_account_state_wait_retries_destroyed_navigation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(
        url=(
            "https://wd1.myworkdaysite.com/recruiting/wf/"
            "WellsFargoJobs/job/Test_R-1/apply/applyManually"
        )
    )
    browser = PlaywrightWorkdayBrowser(page)
    calls = 0

    async def detect_account_state() -> NativeAccountPageState:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _NavigationContextError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        return NativeAccountPageState.AUTHENTICATED

    monkeypatch.setattr(browser, "detect_account_state", detect_account_state)

    state = await browser._wait_for_account_state_change(
        NativeAccountPageState.REGISTRATION_REQUIRED
    )

    assert state is NativeAccountPageState.AUTHENTICATED
    assert calls == 2
    assert page.waits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "confidence", "expected"),
    [
        ("Apply", 0.8, True),
        ("Unrelated", 0.8, False),
        ("Unrelated", 0.95, True),
    ],
)
async def test_llm_action_uses_semantic_confirmation_below_high_confidence(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    confidence: float,
    expected: bool,
) -> None:
    action = _ApplyAction()
    candidate = PortalControlCandidate(
        "control-0", "BUTTON", "button", label, label, "", ""
    )
    browser = PlaywrightWorkdayBrowser(
        _HydratingPage(url="https://wd1.myworkdaysite.com/job/Test_R-1"),
        control_resolver=_SelectionResolver(
            PortalControlSelection("control-0", confidence)
        ),
    )

    async def candidates():
        return [candidate], {"control-0": action}

    monkeypatch.setattr(browser, "_visible_action_candidates", candidates)

    result = await browser._resolve_action(
        PortalControlIntent.APPLY,
        ("button", "link"),
        re.compile(r"^\s*apply(?:\s+now)?\s*$", re.IGNORECASE),
    )

    assert (result is action) is expected


@pytest.mark.asyncio
async def test_application_next_click_requires_confirmed_form_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/recruiting/wf/site/apply")
    action = _ApplyAction()
    browser = PlaywrightWorkdayBrowser(page)
    signatures = [("first-step",), ("second-step",)]

    async def resolve_action(intent, roles, name):
        assert intent is PortalControlIntent.NEXT_APPLICATION_STEP
        assert roles == ("button",)
        assert name.fullmatch("Next") is not None
        assert name.fullmatch("Submit") is None
        return action

    async def form_signature():
        return signatures.pop(0)

    monkeypatch.setattr(browser, "_resolve_action", resolve_action)
    monkeypatch.setattr(browser, "_application_field_signature", form_signature)

    await browser.advance_to_next_application_step()

    assert action.clicks == 1
    assert signatures == []


@pytest.mark.asyncio
async def test_application_next_never_accepts_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = PlaywrightWorkdayBrowser(
        _HydratingPage(url="https://wd1.myworkdaysite.com/recruiting/wf/site/apply")
    )

    async def form_signature():
        return ("review-step",)

    async def no_next(intent, roles, name):
        assert intent is PortalControlIntent.NEXT_APPLICATION_STEP
        assert name.fullmatch("Submit") is None
        return None

    monkeypatch.setattr(browser, "_application_field_signature", form_signature)
    monkeypatch.setattr(browser, "_resolve_action", no_next)

    with pytest.raises(WorkdayWorkerError) as exc_info:
        await browser.advance_to_next_application_step()

    assert exc_info.value.safe_code == "workday_application_next_action_not_selected"


@pytest.mark.asyncio
async def test_llm_action_recaptures_controls_for_one_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _ApplyAction()
    candidate = PortalControlCandidate(
        "control-0", "BUTTON", "button", "Apply", "Apply", "", ""
    )
    resolver = _SequenceResolver([None, PortalControlSelection("control-0", 0.8)])
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/job/Test_R-1")
    decision_events: list[str] = []
    browser = PlaywrightWorkdayBrowser(
        page,
        control_resolver=resolver,
        decision_reporter=decision_events.append,
    )
    captures = 0

    async def candidates():
        nonlocal captures
        captures += 1
        return [candidate], {"control-0": action}

    monkeypatch.setattr(browser, "_visible_action_candidates", candidates)

    result = await browser._resolve_action(
        PortalControlIntent.APPLY,
        ("button", "link"),
        re.compile(r"^\s*apply(?:\s+now)?\s*$", re.IGNORECASE),
    )

    assert result is action
    assert captures == 2
    assert resolver.calls == 2
    assert page.waits == 1
    assert '"outcome":"no_selection"' in decision_events[0]
    assert '"outcome":"accepted"' in decision_events[1]
    assert '"candidate_id":"control-0"' in decision_events[1]


@pytest.mark.asyncio
async def test_empty_llm_selection_uses_one_exact_semantic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = _ApplyAction()
    candidate = PortalControlCandidate(
        "control-0", "BUTTON", "button", "Apply", "Apply", "", ""
    )
    resolver = _SequenceResolver([None, None])
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/job/Test_R-1")
    decision_events: list[str] = []
    browser = PlaywrightWorkdayBrowser(
        page,
        control_resolver=resolver,
        decision_reporter=decision_events.append,
    )

    async def candidates():
        return [candidate], {"control-0": action}

    monkeypatch.setattr(browser, "_visible_action_candidates", candidates)

    result = await browser._resolve_action(
        PortalControlIntent.APPLY,
        ("button", "link"),
        re.compile(r"^\s*apply(?:\s+now)?\s*$", re.IGNORECASE),
    )

    assert result is action
    assert resolver.calls == 2
    assert page.waits == 1
    assert '"outcome":"unique_semantic_fallback_accepted"' in decision_events[-1]
    assert '"semantic_match_count":1' in decision_events[-1]


@pytest.mark.asyncio
async def test_empty_llm_selection_rejects_ambiguous_semantic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _ApplyAction()
    second = _ApplyAction()
    candidates = [
        PortalControlCandidate(
            "control-0", "BUTTON", "button", "Apply", "Apply", "", ""
        ),
        PortalControlCandidate(
            "control-1", "BUTTON", "button", "Apply Now", "Apply Now", "", ""
        ),
    ]
    resolver = _SequenceResolver([None, None])
    decision_events: list[str] = []
    browser = PlaywrightWorkdayBrowser(
        _HydratingPage(url="https://wd1.myworkdaysite.com/job/Test_R-1"),
        control_resolver=resolver,
        decision_reporter=decision_events.append,
    )

    async def visible_candidates():
        return candidates, {"control-0": first, "control-1": second}

    monkeypatch.setattr(browser, "_visible_action_candidates", visible_candidates)

    result = await browser._resolve_action(
        PortalControlIntent.APPLY,
        ("button", "link"),
        re.compile(r"^\s*apply(?:\s+now)?\s*$", re.IGNORECASE),
    )

    assert result is None
    assert '"outcome":"semantic_fallback_rejected"' in decision_events[-1]
    assert '"semantic_match_count":2' in decision_events[-1]


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (
            WorkdayAccountSignals(
                has_email_input=True,
                password_input_count=1,
                has_login_action=True,
            ),
            NativeAccountPageState.LOGIN_REQUIRED,
        ),
        (
            WorkdayAccountSignals(
                has_email_input=True,
                password_input_count=2,
                has_create_account_action=True,
            ),
            NativeAccountPageState.REGISTRATION_REQUIRED,
        ),
        (
            WorkdayAccountSignals(
                has_email_input=True,
                password_input_count=3,
                has_login_action=True,
                has_create_account_action=True,
                has_account_dialog=True,
                dialog_has_email_input=True,
                dialog_password_input_count=1,
                dialog_has_login_action=True,
            ),
            NativeAccountPageState.LOGIN_REQUIRED,
        ),
        (
            WorkdayAccountSignals(account_alert_text="No account was found."),
            NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
        ),
        (
            WorkdayAccountSignals(account_alert_text="Invalid email or password."),
            NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
        ),
        (
            WorkdayAccountSignals(account_alert_text="Account already exists."),
            NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS,
        ),
        (
            WorkdayAccountSignals(has_captcha=True, has_otp_input=True),
            NativeAccountPageState.CAPTCHA,
        ),
        (
            WorkdayAccountSignals(has_otp_input=True),
            NativeAccountPageState.OTP,
        ),
        (
            WorkdayAccountSignals(
                account_alert_text="Service temporarily unavailable."
            ),
            NativeAccountPageState.TRANSIENT_FAILURE,
        ),
        (
            WorkdayAccountSignals(
                trusted_account_message_text=(
                    "Your account has been temporarily locked. Try again in 30 minutes."
                )
            ),
            NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED,
        ),
        (
            WorkdayAccountSignals(
                has_application_action=True,
                url=(
                    "https://wd1.myworkdaysite.com/recruiting/wf/site/"
                    "job/Test_R-1/apply/applyManually"
                ),
            ),
            NativeAccountPageState.AUTHENTICATED,
        ),
        (
            WorkdayAccountSignals(
                has_application_action=True,
                url=(
                    "https://wd1.myworkdaysite.com/recruiting/wf/site/" "job/Test_R-1"
                ),
            ),
            NativeAccountPageState.UNKNOWN,
        ),
        (
            WorkdayAccountSignals(has_job_unavailable_notice=True),
            NativeAccountPageState.JOB_UNAVAILABLE,
        ),
        (WorkdayAccountSignals(), NativeAccountPageState.UNKNOWN),
    ],
)
def test_classify_workday_account_state(
    signals: WorkdayAccountSignals,
    expected: NativeAccountPageState,
) -> None:
    assert classify_workday_account_state(signals) is expected


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (
            WorkdayAccountSignals(account_alert_text="Please try again later."),
            NativeAccountPageState.TRANSIENT_FAILURE,
        ),
        (
            WorkdayAccountSignals(
                trusted_account_message_text="Invalid email or password.",
                account_alert_text="Invalid email or password.",
            ),
            NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
        ),
        (
            WorkdayAccountSignals(
                has_captcha=True,
                trusted_account_message_text="Your account is temporarily locked.",
            ),
            NativeAccountPageState.CAPTCHA,
        ),
        (
            WorkdayAccountSignals(
                has_otp_input=True,
                trusted_account_message_text="Your account is temporarily locked.",
            ),
            NativeAccountPageState.OTP,
        ),
        (
            WorkdayAccountSignals(
                account_alert_text="Your account has been temporarily locked."
            ),
            NativeAccountPageState.UNKNOWN,
        ),
    ],
)
def test_only_trusted_visible_lock_evidence_maps_to_account_lock(
    signals: WorkdayAccountSignals,
    expected: NativeAccountPageState,
) -> None:
    assert classify_workday_account_state(signals) is expected


def test_extract_trusted_workday_unlock_time_from_bounded_formats() -> None:
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    relative = WorkdayAccountSignals(
        trusted_account_message_text=(
            "Your account has been temporarily locked. Try again in 30 minutes."
        )
    )
    absolute = WorkdayAccountSignals(
        trusted_account_message_text=(
            "Your account is temporarily locked until 2026-08-28T12:00:00Z."
        )
    )
    unspecified = WorkdayAccountSignals(
        trusted_account_message_text="Your account has been temporarily locked."
    )

    assert extract_trusted_workday_unlock_time(
        relative, observed_at=observed_at
    ) == datetime(2026, 8, 28, 10, 30, tzinfo=UTC)
    assert extract_trusted_workday_unlock_time(
        absolute, observed_at=observed_at
    ) == datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    assert (
        extract_trusted_workday_unlock_time(unspecified, observed_at=observed_at)
        is None
    )


def test_workday_email_selector_supports_registration_autocomplete() -> None:
    assert "input[autocomplete='email']" in PlaywrightWorkdayBrowser._EMAIL_SELECTOR


@dataclass
class _FakeBrowser:
    states: list[NativeAccountPageState]
    calls: list[str] = field(default_factory=list)

    async def open_and_start_apply(self, target_url: str) -> None:
        self.calls.append(f"open:{target_url}")

    async def detect_account_state(self) -> NativeAccountPageState:
        self.calls.append("detect")
        return self.states.pop(0)

    async def open_registration(self) -> None:
        self.calls.append("open_registration")

    async def open_login(self) -> None:
        self.calls.append("open_login")

    async def fill_registration(self, credential: WorkerPortalCredential) -> None:
        assert credential.password == "Secret!Portal9Password"
        self.calls.append("fill_registration")

    async def submit_registration(self) -> None:
        self.calls.append("submit_registration")


class _FakeCoordinator:
    def __init__(self, plans: list[NativeAccountPlan]):
        self.plans = plans
        self.states: list[NativeAccountPageState] = []

    async def plan_workday_action(self, **kwargs) -> NativeAccountPlan:
        self.states.append(kwargs["page_state"])
        return self.plans.pop(0)


@dataclass
class _FakeAuthBroker:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def authenticate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            route=SimpleNamespace(outcome=WorkdayFailureOutcome.COMPLETE_UNIT)
        )


@dataclass
class _FakeEmitter:
    states: list[tuple[str, NativeAccountPageState]] = field(default_factory=list)

    async def emit(
        self, application_id: str, page_state: NativeAccountPageState
    ) -> None:
        self.states.append((application_id, page_state))


def _credential(
    *, status: Literal["active", "pending_registration"] = "active"
) -> WorkerPortalCredential:
    return WorkerPortalCredential(
        credential_id="credential-1",
        portal_scope="workday:wf:wellsfargojobs",
        account_email="candidate@example.com",
        status=status,
        password="Secret!Portal9Password",
    )


@dataclass
class _FormTarget:
    control_type: str = "text"
    value: str = ""
    checked: bool = False

    async def evaluate(self, script: str):
        if "selectedOptions" in script:
            return {"value": self.value, "text": self.value}
        return self.control_type

    async def fill(self, value: str, *, timeout: int) -> None:
        assert timeout == 15_000
        self.value = value

    async def input_value(self) -> str:
        return self.value

    async def check(self, *, timeout: int) -> None:
        assert timeout == 15_000
        self.checked = True

    async def is_checked(self) -> bool:
        return self.checked


@pytest.mark.asyncio
async def test_login_fill_and_submit_stay_inside_confirmed_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/recruiting/wf/site/apply")
    browser = PlaywrightWorkdayBrowser(page)
    dialog = object()
    email = _FormTarget()
    password = _FormTarget()
    action = _ApplyAction()

    async def visible_account_dialog():
        return dialog

    async def first_visible_locator_in(scope, selector):
        assert scope is dialog
        return password if "password" in selector else email

    async def resolve_action(intent, roles, deterministic_name, *, scope=None):
        assert intent is PortalControlIntent.SIGN_IN
        assert roles == ("button",)
        assert deterministic_name.fullmatch("Sign In")
        assert scope is dialog
        return action

    async def visible_count_in(scope, selector):
        assert scope is dialog
        return 1

    async def visible_role_count_in(scope, roles, name):
        assert scope is dialog
        assert roles == ("button",)
        assert name.fullmatch("Sign In")
        return 1

    monkeypatch.setattr(browser, "_visible_account_dialog", visible_account_dialog)
    monkeypatch.setattr(browser, "_first_visible_locator_in", first_visible_locator_in)
    monkeypatch.setattr(browser, "_resolve_action", resolve_action)
    monkeypatch.setattr(browser, "_visible_count_in", visible_count_in)
    monkeypatch.setattr(browser, "_visible_role_count_in", visible_role_count_in)

    assert await browser.verify_unique_auth_controls(expected_scope="workday:wf:site")
    await browser.fill_verified_auth_controls(_credential())
    await browser.click_verified_sign_in()

    assert email.value == "candidate@example.com"
    assert password.value == "Secret!Portal9Password"
    assert action.clicks == 1


@pytest.mark.asyncio
async def test_login_fill_and_submit_accept_unique_full_page_account_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _HydratingPage(url="https://wd1.myworkdaysite.com/recruiting/wf/site/apply")
    browser = PlaywrightWorkdayBrowser(page)
    email = _FormTarget()
    password = _FormTarget()
    action = _ApplyAction()

    async def visible_account_dialog():
        return None

    async def visible_count_in(scope, selector):
        assert scope is page
        return 1

    async def visible_role_count_in(scope, roles, name):
        assert scope is page
        assert roles == ("button",)
        assert name.fullmatch("Sign In")
        return 1

    async def first_visible_locator_in(scope, selector):
        assert scope is page
        return password if "password" in selector else email

    async def resolve_action(intent, roles, deterministic_name, *, scope=None):
        assert intent is PortalControlIntent.SIGN_IN
        assert roles == ("button",)
        assert deterministic_name.fullmatch("Sign In")
        assert scope is page
        return action

    monkeypatch.setattr(browser, "_visible_account_dialog", visible_account_dialog)
    monkeypatch.setattr(browser, "_visible_count_in", visible_count_in)
    monkeypatch.setattr(browser, "_visible_role_count_in", visible_role_count_in)
    monkeypatch.setattr(browser, "_first_visible_locator_in", first_visible_locator_in)
    monkeypatch.setattr(browser, "_resolve_action", resolve_action)

    account_scope, scope_kind = await browser._visible_account_control_scope()
    assert account_scope is page
    assert scope_kind is WorkdayControlScope.ACTIVE_ACCOUNT_FORM
    assert await browser.verify_unique_auth_controls(expected_scope="workday:wf:site")
    await browser.fill_verified_auth_controls(_credential())
    await browser.click_verified_sign_in()

    assert email.value == "candidate@example.com"
    assert password.value == "Secret!Portal9Password"
    assert action.clicks == 1


@pytest.mark.asyncio
async def test_form_fill_writes_only_approved_sources_and_verifies_values() -> None:
    approved = _FormTarget()
    manual = _FormTarget()
    browser = PlaywrightWorkdayBrowser(object())
    browser._form_locators = {"0": approved, "1": manual}

    result = await browser.fill_and_verify_application_fields(
        [
            WorkdayFormAssignment(
                field_uid="0", value="Candidate", answer_source="profile"
            ),
            WorkdayFormAssignment(
                field_uid="1", value="invented", answer_source="manual"
            ),
        ]
    )

    assert result.filled_count == result.verified_count == 1
    assert approved.value == "Candidate"
    assert manual.value == ""


@pytest.mark.asyncio
async def test_form_fill_commits_exact_combobox_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    combobox = _FormTarget(control_type="combobox")
    browser = PlaywrightWorkdayBrowser(object())
    browser._form_locators = {"0": combobox}

    class _Option:
        async def click(self, *, timeout: int) -> None:
            assert timeout == 15_000
            combobox.value = "India (+91)"

    async def first_visible_role(roles, name):
        assert roles == ("option",)
        assert name.fullmatch("India (+91)") is not None
        assert name.fullmatch("Submit") is None
        return _Option()

    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    result = await browser.fill_and_verify_application_fields(
        [
            WorkdayFormAssignment(
                field_uid="0",
                value="India (+91)",
                answer_source="profile",
            )
        ]
    )

    assert result.filled_count == result.verified_count == 1
    assert result.unsupported_field_uids == ()
    assert combobox.value == "India (+91)"


@dataclass
class _RegistrationTarget:
    values: list[str] = field(default_factory=list)
    checked: bool = False

    async def is_visible(self) -> bool:
        return True

    async def fill(self, value: str, *, timeout: int) -> None:
        assert timeout == 15_000
        self.values.append(value)

    async def is_checked(self) -> bool:
        return self.checked

    async def check(self, *, timeout: int) -> None:
        assert timeout == 15_000
        self.checked = True


class _RegistrationTargets:
    def __init__(self, targets: list[_RegistrationTarget]):
        self._targets = targets

    async def count(self) -> int:
        return len(self._targets)

    def nth(self, index: int) -> _RegistrationTarget:
        return self._targets[index]


class _RegistrationPage:
    def __init__(self, passwords: list[_RegistrationTarget]):
        self._passwords = passwords

    def locator(self, selector: str) -> _RegistrationTargets:
        assert selector == "input[type='password']"
        return _RegistrationTargets(self._passwords)


@pytest.mark.asyncio
async def test_registration_checks_recognized_terms_with_explicit_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = _RegistrationTarget()
    passwords = [_RegistrationTarget(), _RegistrationTarget()]
    consent = _RegistrationTarget()
    browser = PlaywrightWorkdayBrowser(
        _RegistrationPage(passwords), accept_account_terms=True
    )

    async def first_visible_locator(selector: str) -> _RegistrationTarget:
        return email

    async def first_visible_role(roles, name) -> _RegistrationTarget:
        assert roles == ("checkbox",)
        assert "terms" in name.pattern
        return consent

    monkeypatch.setattr(browser, "_first_visible_locator", first_visible_locator)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    await browser.fill_registration(_credential())

    assert consent.checked is True
    assert email.values == ["candidate@example.com"]
    assert all(len(target.values) == 1 for target in passwords)


@pytest.mark.asyncio
async def test_registration_requires_explicit_terms_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = _RegistrationTarget()
    passwords = [_RegistrationTarget(), _RegistrationTarget()]
    consent = _RegistrationTarget()
    browser = PlaywrightWorkdayBrowser(_RegistrationPage(passwords))

    async def first_visible_locator(selector: str) -> _RegistrationTarget:
        return email

    async def first_visible_role(roles, name) -> _RegistrationTarget:
        return consent

    monkeypatch.setattr(browser, "_first_visible_locator", first_visible_locator)
    monkeypatch.setattr(browser, "_first_visible_role", first_visible_role)

    with pytest.raises(WorkdayWorkerError, match="Explicit approval"):
        await browser.fill_registration(_credential())

    assert email.values == []
    assert all(target.values == [] for target in passwords)


def _lease(**overrides) -> WorkdayLease:
    values = {
        "application_id": "application-1",
        "user_id": "user-1",
        "portal": "workday",
        "job_url": (
            "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
    }
    values.update(overrides)
    return WorkdayLease(**values)


@pytest.mark.asyncio
async def test_worker_starts_apply_then_completes_saved_login() -> None:
    browser = _FakeBrowser(
        [NativeAccountPageState.LOGIN_REQUIRED, NativeAccountPageState.AUTHENTICATED]
    )
    coordinator = _FakeCoordinator(
        [
            NativeAccountPlan(
                NativeAccountAction.ATTEMPT_LOGIN,
                "workday:wf:wellsfargojobs",
                account_ref=uuid.UUID("20000000-0000-0000-0000-000000000001"),
            ),
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            ),
        ]
    )
    emitter = _FakeEmitter()
    auth_broker = _FakeAuthBroker()

    outcome = await WorkdayAccountGateWorker(
        browser, coordinator, emitter, auth_broker
    ).run(
        lease=_lease(),
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
    )

    assert browser.calls == [
        f"open:{_lease().job_url}",
        "detect",
    ]
    assert coordinator.states == [
        NativeAccountPageState.LOGIN_REQUIRED,
        NativeAccountPageState.LOGIN_COMPLETE,
    ]
    assert outcome.status is WorkdayAccountGateStatus.ACCOUNT_READY
    assert emitter.states == [
        ("application-1", NativeAccountPageState.LOGIN_REQUIRED),
        ("application-1", NativeAccountPageState.LOGIN_COMPLETE),
    ]
    assert "Secret!Portal9Password" not in repr(outcome)
    assert len(auth_broker.calls) == 1


@pytest.mark.asyncio
async def test_worker_opens_login_for_active_account_on_registration_page() -> None:
    browser = _FakeBrowser(
        [
            NativeAccountPageState.REGISTRATION_REQUIRED,
            NativeAccountPageState.LOGIN_REQUIRED,
            NativeAccountPageState.AUTHENTICATED,
        ]
    )
    coordinator = _FakeCoordinator(
        [
            NativeAccountPlan(
                NativeAccountAction.ATTEMPT_LOGIN,
                "workday:wf:wellsfargojobs",
                account_ref=uuid.UUID("20000000-0000-0000-0000-000000000001"),
            ),
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            ),
        ]
    )
    emitter = _FakeEmitter()
    auth_broker = _FakeAuthBroker()

    outcome = await WorkdayAccountGateWorker(
        browser, coordinator, emitter, auth_broker
    ).run(
        lease=_lease(),
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
    )

    assert browser.calls == [
        f"open:{_lease().job_url}",
        "detect",
        "open_login",
        "detect",
    ]
    assert coordinator.states == [
        NativeAccountPageState.REGISTRATION_REQUIRED,
        NativeAccountPageState.LOGIN_COMPLETE,
    ]
    assert outcome.status is WorkdayAccountGateStatus.ACCOUNT_READY
    assert emitter.states == [
        ("application-1", NativeAccountPageState.REGISTRATION_REQUIRED),
        ("application-1", NativeAccountPageState.LOGIN_REQUIRED),
        ("application-1", NativeAccountPageState.LOGIN_COMPLETE),
    ]
    assert len(auth_broker.calls) == 1


@pytest.mark.asyncio
async def test_worker_opens_registration_and_stops_on_captcha() -> None:
    browser = _FakeBrowser(
        [
            NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
            NativeAccountPageState.CAPTCHA,
        ]
    )
    coordinator = _FakeCoordinator(
        [
            NativeAccountPlan(
                NativeAccountAction.REGISTER_ACCOUNT,
                "workday:wf:wellsfargojobs",
                credential=_credential(),
            ),
            NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD,
                "workday:wf:wellsfargojobs",
                hold_code="captcha",
            ),
        ]
    )
    emitter = _FakeEmitter()

    outcome = await WorkdayAccountGateWorker(browser, coordinator, emitter).run(
        lease=_lease(),
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
    )

    assert browser.calls == [
        f"open:{_lease().job_url}",
        "detect",
        "open_registration",
        "detect",
    ]
    assert outcome.status is WorkdayAccountGateStatus.HOLD
    assert outcome.hold_code == "captcha"
    assert emitter.states == [
        ("application-1", NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND),
        ("application-1", NativeAccountPageState.CAPTCHA),
    ]


@pytest.mark.asyncio
async def test_worker_skips_portal_confirmed_unavailable_job() -> None:
    browser = _FakeBrowser([NativeAccountPageState.JOB_UNAVAILABLE])
    coordinator = _FakeCoordinator([])
    emitter = _FakeEmitter()

    outcome = await WorkdayAccountGateWorker(browser, coordinator, emitter).run(
        lease=_lease(),
        portal_name="Wells Fargo Workday",
        account_email="candidate@example.com",
    )

    assert outcome.status is WorkdayAccountGateStatus.SKIPPED
    assert coordinator.states == []
    assert emitter.states == [("application-1", NativeAccountPageState.JOB_UNAVAILABLE)]


def test_lease_prefers_workday_handoff_and_rejects_other_targets() -> None:
    handoff = _lease(
        job_url="https://www.naukri.com/job-1",
        external_ats_url=(
            "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
    )
    assert "myworkdaysite.com" in handoff.target_url

    with pytest.raises(WorkdayWorkerError):
        _lease(job_url="https://example.com/job-1").target_url


class _NoApplicationResult:
    def scalar_one_or_none(self):
        return None


class _CapturingDatabase:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _NoApplicationResult()


@pytest.mark.asyncio
async def test_local_worker_lease_is_limited_to_active_workday_urls() -> None:
    database = _CapturingDatabase()

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": "00000000-0000-0000-0000-000000000001"},
        db=database,
    )

    compiled = str(
        database.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert response == {"application": None}
    assert "job_applications.deleted_at is null" in compiled
    assert "myworkdayjobs|myworkdaysite" in compiled
    assert "~*" in compiled


@pytest.mark.asyncio
async def test_local_worker_lease_can_target_one_owned_eligible_application() -> None:
    database = _CapturingDatabase()
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000009")

    response = await lease_next_application(
        worker_kind="local_playwright",
        current_user={"id": "00000000-0000-0000-0000-000000000001"},
        db=database,
        application_id=application_id,
    )

    compiled = str(
        database.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert response == {"application": None}
    assert str(application_id) in compiled


class _StateDatabase:
    def __init__(self, application: JobApplication):
        self.application = application
        self.added = []
        self.committed = False

    async def get(self, model, object_id):
        assert model is JobApplication
        return self.application if self.application.id == object_id else None

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_user_can_reset_owned_preparing_lease_immediately() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    application = JobApplication(
        id=application_id,
        user_id=user_id,
        status="preparing",
        automation_lease_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        automation_lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    database = _StateDatabase(application)

    response = await reset_application_lease(
        application_id=application_id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response == {
        "id": str(application_id),
        "status": "retrying",
        "reset": True,
    }
    assert application.automation_lease_id is None
    assert application.automation_lease_expires_at is None
    assert database.committed is True
    event = next(
        value
        for value in database.added
        if isinstance(value, ApplicationAutomationEvent)
    )
    assert event.event_type == "application_lease_reset"
    assert event.detail == "user_requested"


@pytest.mark.asyncio
async def test_reset_owned_lease_is_idempotent_after_first_reset() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    application = JobApplication(
        id=application_id,
        user_id=user_id,
        status="retrying",
        automation_lease_id=None,
        automation_lease_expires_at=None,
    )
    database = _StateDatabase(application)

    response = await reset_application_lease(
        application_id=application_id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response == {
        "id": str(application_id),
        "status": "retrying",
        "reset": False,
    }
    assert database.committed is False
    assert database.added == []


@pytest.mark.asyncio
async def test_reset_lease_hides_another_users_application() -> None:
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    application = JobApplication(
        id=application_id,
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        status="preparing",
    )

    with pytest.raises(HTTPException) as exc_info:
        await reset_application_lease(
            application_id=application_id,
            current_user={"id": "00000000-0000-0000-0000-000000000001"},
            db=_StateDatabase(application),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reset_lease_does_not_requeue_a_terminal_application() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    application = JobApplication(
        id=application_id,
        user_id=user_id,
        status="applied",
    )

    with pytest.raises(HTTPException) as exc_info:
        await reset_application_lease(
            application_id=application_id,
            current_user={"id": str(user_id)},
            db=_StateDatabase(application),
        )

    assert exc_info.value.status_code == 409
    assert application.status == "applied"


@pytest.mark.asyncio
async def test_account_state_event_is_allowlisted_and_renews_active_lease() -> None:
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    lease_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    old_expiry = datetime.now(UTC) + timedelta(minutes=1)
    application = JobApplication(
        id=application_id,
        user_id=user_id,
        status="preparing",
        automation_lease_id=lease_id,
        automation_lease_expires_at=old_expiry,
    )
    database = _StateDatabase(application)

    response = await record_account_state(
        application_id=application_id,
        body=AccountStateRequest(
            lease_id=lease_id,
            page_state=NativeAccountPageState.LOGIN_REQUIRED,
        ),
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response["page_state"] == "login_required"
    assert application.automation_lease_expires_at > old_expiry
    assert database.committed is True
    event = next(
        value
        for value in database.added
        if isinstance(value, ApplicationAutomationEvent)
    )
    assert event.event_type == "account_state_observed"
    assert event.detail == "login_required"


def test_account_state_event_rejects_unknown_or_extra_values() -> None:
    lease_id = "00000000-0000-0000-0000-000000000003"
    with pytest.raises(ValidationError):
        AccountStateRequest(lease_id=lease_id, page_state="made_up_state")
    with pytest.raises(ValidationError):
        AccountStateRequest(
            lease_id=lease_id,
            page_state="login_required",
            password="must-not-be-accepted",
        )


def test_navigation_context_error_recognition_narrowed() -> None:
    from services.workday_playwright_worker import (
        _is_playwright_navigation_context_error,
    )

    class _PlaywrightError(Exception):
        __module__ = "playwright._impl._errors"

    class _NonPlaywrightError(Exception):
        __module__ = "builtins"

    # Genuine navigation / context destruction / detached frames -> True
    assert _is_playwright_navigation_context_error(
        _PlaywrightError(
            "Execution context was destroyed, most likely because of a navigation."
        )
    )
    assert _is_playwright_navigation_context_error(
        _PlaywrightError("Cannot find context with specified id")
    )
    assert _is_playwright_navigation_context_error(
        _PlaywrightError("Frame was detached")
    )
    assert _is_playwright_navigation_context_error(
        _PlaywrightError("frame has been detached")
    )
    assert _is_playwright_navigation_context_error(
        _PlaywrightError("detached frame error occurred")
    )

    # Generic Timeout or Target closed or non-playwright -> False
    assert not _is_playwright_navigation_context_error(
        _PlaywrightError("Timeout 30000ms exceeded.")
    )
    assert not _is_playwright_navigation_context_error(
        _PlaywrightError("Target closed")
    )
    assert not _is_playwright_navigation_context_error(
        _PlaywrightError("Protocol error: page crashed")
    )
    assert not _is_playwright_navigation_context_error(
        _NonPlaywrightError("Execution context was destroyed")
    )


@pytest.mark.asyncio
async def test_capture_checkpoint_evidence_excludes_volatile_control_count_from_signature() -> (
    None
):
    page = _HydratingPage(
        url="https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Engineer_R-1"
    )
    browser = PlaywrightWorkdayBrowser(page)

    app_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    target_url = (
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Engineer_R-1"
    )
    tenant_scope = "workday:wf:wellsfargojobs"

    evidence1 = await browser.capture_checkpoint_evidence(
        target_url=target_url,
        expected_tenant_scope=tenant_scope,
        application_id=app_id,
        account_binding_verified=True,
        application_context_matches=True,
    )

    evidence2 = await browser.capture_checkpoint_evidence(
        target_url=target_url,
        expected_tenant_scope=tenant_scope,
        application_id=app_id,
        account_binding_verified=True,
        application_context_matches=True,
    )

    assert evidence1.safe_signature == evidence2.safe_signature
    assert evidence1.safe_signature.startswith("wdcp1:")


def test_next_application_step_action_regex_matches_strictly_without_arbitrary_suffixes() -> (
    None
):
    from services.workday_playwright_worker import (
        _NEXT_APPLICATION_STEP_ACTION,
        _SAVE_AND_CONTINUE_ACTION,
    )

    # Exact matches -> True
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Next") is not None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("next") is not None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Save and Continue") is not None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Save & Continue") is not None
    assert _SAVE_AND_CONTINUE_ACTION.fullmatch("Save and Continue") is not None
    assert _SAVE_AND_CONTINUE_ACTION.fullmatch("Save & Continue") is not None

    # Arbitrary suffixes -> False
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Next Steps") is None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Next Question") is None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Save and Continue Later") is None
    assert _NEXT_APPLICATION_STEP_ACTION.fullmatch("Save & Continue Later") is None
    assert _SAVE_AND_CONTINUE_ACTION.fullmatch("Save and Continue Later") is None
    assert _SAVE_AND_CONTINUE_ACTION.fullmatch("Save & Continue to exit") is None
