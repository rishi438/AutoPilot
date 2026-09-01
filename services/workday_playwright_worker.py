"""Local, credential-safe Workday account-gate Playwright adapter."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from time import monotonic
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlsplit
from uuid import UUID

from services.portal_control_resolver import (
    PortalControlCandidate,
    PortalControlIntent,
    PortalControlResolver,
)
from services.portal_account_automation import (
    NativeAccountAction,
    NativeAccountPageState,
    NativePortalAccountCoordinator,
    derive_workday_portal_scope,
)
from services.portal_credentials import PortalCredentialError, WorkerPortalCredential
from services.workday_auth_broker import (
    WorkdayAuthBrokerResult,
    WorkdayPostSubmitObservation,
)
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_state_observer import (
    WORKDAY_ACCOUNT_TERMS_CONSENT,
    WorkdayControlScope,
    WorkdayPageStructure,
    WorkdaySemanticControl,
    WorkdayStateSecurityError,
)
from services.workday_page_condition import WorkdayUnit1PageCondition
from services.workday_unit1_checkpoint import WorkdayPrivateCheckpointEvidence
from services.workday_transition_contracts import WorkdayFailureClass

_WORKDAY_HOST = re.compile(r"(^|\.)(myworkdayjobs|myworkdaysite)\.com$")
_APPLY_ACTION = re.compile(r"^\s*apply(?:\s+now)?\s*$", re.IGNORECASE)
_APPLY_MANUALLY_ACTION = re.compile(r"^\s*apply\s+manually\s*$", re.IGNORECASE)
_CREATE_ACCOUNT_ACTION = re.compile(
    r"^\s*(?:create\s+account|register)\s*$", re.IGNORECASE
)
_LOGIN_ACTION = re.compile(r"^\s*(?:sign\s+in|log\s+in)\s*$", re.IGNORECASE)
_CLOSE_DIALOG_ACTION = re.compile(
    r"^\s*(?:close(?:\s+(?:dialog|modal))?|cancel)\s*$", re.IGNORECASE
)
_SAVE_AND_CONTINUE_ACTION = re.compile(
    r"^\s*save\s+(?:and|&)\s+continue\s*$", re.IGNORECASE
)
_NEXT_APPLICATION_STEP_ACTION = re.compile(
    r"^\s*(?:next|save\s+(?:and|&)\s+continue)\s*$", re.IGNORECASE
)
_INFORMATION_SECTION_HEADING = re.compile(
    r"^\s*(?:basic|my)\s+information\s*$", re.IGNORECASE
)
_DIALOG_SELECTOR = "[role='dialog'], [aria-modal='true']"
_TRUSTED_MESSAGE_SELECTOR = (
    "[role='alert'], [aria-live='assertive'], "
    "[data-automation-id*='error' i], [role='dialog'], [aria-modal='true']"
)
_ACCOUNT_NOT_FOUND = re.compile(
    r"account\s+(?:was\s+)?not\s+found|no\s+account|could(?:n't| not)\s+find",
    re.IGNORECASE,
)
_INVALID_CREDENTIALS = re.compile(
    r"invalid\s+(?:email|password|credentials)|incorrect\s+(?:email|password)|"
    r"email\s+or\s+password\s+is\s+incorrect|"
    r"wrong\s+email(?:\s+address)?\s+or\s+password",
    re.IGNORECASE,
)
_ACCOUNT_EXISTS = re.compile(
    r"account\b.{0,80}\balready\s+exists|"
    r"email(?:\s+address)?\s+is\s+already\s+(?:in\s+use|registered)",
    re.IGNORECASE | re.DOTALL,
)
_ACCOUNT_TEMPORARILY_LOCKED = re.compile(
    r"\b(?:your\s+)?account\s+(?:has\s+been|is)\s+temporarily\s+locked\b|"
    r"\btoo\s+many\s+(?:failed|unsuccessful)\s+(?:sign[- ]?in|login)\s+"
    r"attempts\b.{0,120}\b(?:your\s+)?account\s+(?:has\s+been|is)\s+locked\b",
    re.IGNORECASE | re.DOTALL,
)
_LOCK_UNTIL_UTC = re.compile(
    r"\buntil\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|\+00:00))\b",
    re.IGNORECASE,
)
_LOCK_DURATION = re.compile(
    r"\b(?:try\s+again\s+in|wait)\s+(\d{1,4})\s+"
    r"(minutes?|hours?)(?:\s+before\s+trying\s+again)?\b",
    re.IGNORECASE,
)
_TRANSIENT_FAILURE = re.compile(
    r"temporarily\s+unavailable|try\s+again\s+later|service\s+unavailable|"
    r"something\s+went\s+wrong",
    re.IGNORECASE,
)
_JOB_UNAVAILABLE = re.compile(
    r"^\s*(?:the\s+page\s+you\s+are\s+looking\s+for\s+does(?:n't|\s+not)\s+"
    r"exist|this\s+job\s+is\s+no\s+longer\s+available|the\s+job\s+posting\s+"
    r"is\s+no\s+longer\s+available)\.?\s*$",
    re.IGNORECASE,
)
_NAVIGATION_CONTEXT_DESTROYED = re.compile(
    r"execution context was destroyed|Cannot find context with specified id|"
    r"frame (?:was|has been|is) detached|detached frame",
    re.IGNORECASE | re.DOTALL,
)
logger = logging.getLogger(__name__)


def _is_playwright_navigation_context_error(exc: Exception) -> bool:
    """Recognize only Playwright's transient old-document navigation failure."""
    return (
        type(exc).__module__.startswith("playwright.")
        and _NAVIGATION_CONTEXT_DESTROYED.search(str(exc)[:1000]) is not None
    )


class WorkdayWorkerError(RuntimeError):
    """Raised when the browser cannot safely execute a bounded Workday action."""

    def __init__(
        self,
        message: str,
        *,
        safe_code: str = "workday_account_gate_action_failed",
    ):
        super().__init__(message)
        self.safe_code = safe_code


class WorkdayAccountGateStatus(str, Enum):
    """Safe outcome returned to the local worker lifecycle."""

    ACCOUNT_READY = "account_ready"
    HOLD = "hold"
    RETRY_LATER = "retry_later"
    SKIPPED = "skipped"


@dataclass(frozen=True, kw_only=True)
class WorkdayLease:
    """Safe application metadata received from the worker-neutral lease API."""

    application_id: str
    user_id: str
    portal: str
    job_url: str
    external_ats_url: str | None = None

    @property
    def target_url(self) -> str:
        """Prefer the discovered ATS handoff and require a Workday HTTPS target."""
        candidate = (self.external_ats_url or self.job_url).strip()
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not _WORKDAY_HOST.search(host):
            raise WorkdayWorkerError(
                "The leased application is not a Workday HTTPS URL."
            )
        return candidate


@dataclass(frozen=True)
class WorkdayAccountSignals:
    """Bounded DOM observations; page text is treated only as inert state evidence."""

    has_email_input: bool = False
    password_input_count: int = 0
    has_login_action: bool = False
    has_create_account_action: bool = False
    has_account_dialog: bool = False
    dialog_has_email_input: bool = False
    dialog_password_input_count: int = 0
    dialog_has_login_action: bool = False
    has_application_action: bool = False
    has_captcha: bool = False
    has_otp_input: bool = False
    has_job_unavailable_notice: bool = False
    account_alert_text: str = ""
    trusted_account_message_text: str = ""
    url: str = ""


@dataclass(frozen=True)
class WorkdayAccountGateOutcome:
    """Credential-free result suitable for lifecycle and audit handling."""

    status: WorkdayAccountGateStatus
    page_state: NativeAccountPageState
    portal_scope: str
    hold_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class WorkdayFormField:
    field_uid: str
    tag: str
    input_type: str
    name_attr: str | None
    id_attr: str | None
    label_text: str
    placeholder: str | None
    aria_label: str | None
    required: bool
    readonly: bool
    disabled: bool
    current_value: str | None
    max_length: int | None
    options: tuple[dict[str, str], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["options"] = list(self.options) or None
        return payload


@dataclass(frozen=True, kw_only=True)
class WorkdayFormAssignment:
    field_uid: str
    value: str
    answer_source: str
    review_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class WorkdayFormFillResult:
    filled_count: int
    verified_count: int
    failed_field_uids: tuple[str, ...] = ()
    unsupported_field_uids: tuple[str, ...] = ()


def classify_workday_account_state(
    signals: WorkdayAccountSignals,
) -> NativeAccountPageState:
    """Classify trusted, bounded account signals with challenge-first precedence."""
    if signals.has_captcha:
        return NativeAccountPageState.CAPTCHA
    if signals.has_otp_input:
        return NativeAccountPageState.OTP
    if signals.has_job_unavailable_notice:
        return NativeAccountPageState.JOB_UNAVAILABLE

    trusted_message = signals.trusted_account_message_text[:4000]
    if _ACCOUNT_TEMPORARILY_LOCKED.search(trusted_message):
        return NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED

    alert = signals.account_alert_text[:4000]
    if _ACCOUNT_EXISTS.search(alert):
        return NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS
    if _ACCOUNT_NOT_FOUND.search(alert):
        return NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND
    if _INVALID_CREDENTIALS.search(alert):
        return NativeAccountPageState.LOGIN_INVALID_CREDENTIALS
    if _TRANSIENT_FAILURE.search(alert):
        return NativeAccountPageState.TRANSIENT_FAILURE

    # Workday leaves the registration form visible underneath its login modal.
    # Once an account dialog appears, classify only its account controls so the
    # covered form cannot keep winning while the dialog hydrates.
    if signals.has_account_dialog:
        if signals.dialog_has_email_input and signals.dialog_password_input_count >= 2:
            return NativeAccountPageState.REGISTRATION_REQUIRED
        if (
            signals.dialog_has_email_input
            and signals.dialog_password_input_count == 1
            and signals.dialog_has_login_action
        ):
            return NativeAccountPageState.LOGIN_REQUIRED
        return NativeAccountPageState.UNKNOWN

    if signals.has_email_input and signals.password_input_count >= 2:
        return NativeAccountPageState.REGISTRATION_REQUIRED
    if (
        signals.has_email_input
        and signals.password_input_count == 1
        and signals.has_login_action
    ):
        return NativeAccountPageState.LOGIN_REQUIRED
    url_path = urlsplit(signals.url).path.casefold()
    is_account_home = "/userhome" in url_path
    is_application_flow = (
        is_account_home
        or "/apply/" in url_path
        or url_path.rstrip("/").endswith("/apply")
    )
    if is_account_home or (signals.has_application_action and is_application_flow):
        return NativeAccountPageState.AUTHENTICATED
    return NativeAccountPageState.UNKNOWN


def extract_trusted_workday_unlock_time(
    signals: WorkdayAccountSignals, *, observed_at: datetime
) -> datetime | None:
    """Extract only bounded ISO-UTC or minute/hour lock deadlines."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Lock observation time must be timezone-aware.")
    observed_utc = observed_at.astimezone(UTC)
    latest_trusted_time = observed_utc + timedelta(hours=168)
    text = signals.trusted_account_message_text[:4000]
    if _ACCOUNT_TEMPORARILY_LOCKED.search(text) is None:
        return None

    until_match = _LOCK_UNTIL_UTC.search(text)
    if until_match is not None:
        value = until_match.group(1).replace(" ", "T").replace("Z", "+00:00")
        try:
            until = datetime.fromisoformat(value).astimezone(UTC)
        except ValueError:
            return None
        return until if observed_utc < until <= latest_trusted_time else None

    duration_match = _LOCK_DURATION.search(text)
    if duration_match is None:
        return None
    amount = int(duration_match.group(1))
    unit = duration_match.group(2).lower()
    duration = (
        timedelta(hours=amount)
        if unit.startswith("hour")
        else timedelta(minutes=amount)
    )
    if duration <= timedelta(0) or duration > timedelta(hours=168):
        return None
    until = observed_utc + duration
    return until if until <= latest_trusted_time else None


class WorkdayBrowser(Protocol):
    """Small browser surface owned by the deterministic Workday adapter."""

    async def open_and_start_apply(self, target_url: str) -> None: ...

    async def detect_account_state(self) -> NativeAccountPageState: ...

    async def capture_page_condition(self) -> WorkdayUnit1PageCondition: ...

    async def capture_checkpoint_evidence(
        self,
        *,
        target_url: str,
        expected_tenant_scope: str,
        application_id: UUID,
        account_binding_verified: bool,
        application_context_matches: bool,
    ) -> WorkdayPrivateCheckpointEvidence: ...

    async def wait_for_hydration(self, milliseconds: int) -> None: ...

    async def open_login(self) -> None: ...

    async def open_registration(self) -> None: ...

    async def open_registration_after_rejected_login(
        self, *, expected_scope: str
    ) -> None: ...

    async def open_registration_from_login_form(
        self, *, expected_scope: str
    ) -> None: ...

    async def fill_registration(self, credential: WorkerPortalCredential) -> None: ...

    async def submit_registration(self) -> None: ...

    async def scan_application_fields(self) -> tuple[str, list[WorkdayFormField]]: ...

    async def fill_and_verify_application_fields(
        self, assignments: list[WorkdayFormAssignment]
    ) -> WorkdayFormFillResult: ...

    async def advance_to_next_application_step(self) -> None: ...


class WorkdayStateEmitter(Protocol):
    """Credential-free state sink implemented by the worker transport."""

    async def emit(
        self, application_id: str, page_state: NativeAccountPageState
    ) -> None: ...


class WorkdayAuthenticationDelegate(Protocol):
    """Bound Task 10 broker surface; gate authority remains outside the worker."""

    async def authenticate(
        self,
        *,
        user_id: str,
        application_id: str,
        account_ref: UUID,
        portal_scope: str,
    ) -> WorkdayAuthBrokerResult: ...


class PlaywrightWorkdayBrowser:
    """Async Playwright page adapter that never logs or screenshots secrets."""

    _EMAIL_SELECTOR = (
        "input[type='email'], input[autocomplete='email'], "
        "input[autocomplete='username'], input[name*='email' i], "
        "input[data-automation-id='email']"
    )
    _PASSWORD_SELECTOR = "input[type='password']"
    _CAPTCHA_INPUT_SELECTOR = "input[name*='captcha' i]"
    _CAPTCHA_WIDGET_SELECTOR = (
        "iframe[src*='captcha' i], iframe[title*='captcha' i], " "[data-sitekey]"
    )
    _OTP_SELECTOR = (
        "input[autocomplete='one-time-code'], input[name*='verificationCode' i], "
        "input[id*='verificationCode' i]"
    )
    _ALERT_SELECTOR = (
        "[role='alert'], [aria-live='assertive'], [data-automation-id*='error' i]"
    )
    _HEADING_SELECTOR = "h1, h2, h3, [role='heading']"
    _ACTION_SELECTOR = (
        "button, [role='button'], a[href], [role='link'], "
        "input[type='submit'], input[type='button']"
    )
    _APPLICATION_FIELD_SELECTOR = (
        "input:not([type='hidden']):not([type='password']):not([type='submit']):"
        "not([type='button']), textarea, select, [role='combobox']"
    )

    def __init__(
        self,
        page: Any,
        *,
        timeout_ms: int = 15_000,
        accept_account_terms: bool = False,
        control_resolver: PortalControlResolver | None = None,
        decision_reporter: Callable[[str], None] | None = None,
    ):
        self._page = page
        self._timeout_ms = timeout_ms
        self._accept_account_terms = accept_account_terms
        self._control_resolver = control_resolver
        self._decision_reporter = decision_reporter
        self._form_locators: dict[str, Any] = {}
        self._verified_auth_scope: str | None = None
        self._verified_registration_scope: str | None = None

    def _report_control_decision(self, event: dict[str, Any]) -> None:
        if self._decision_reporter is not None:
            self._decision_reporter(
                json.dumps(event, ensure_ascii=True, separators=(",", ":"))
            )

    async def open_approved_job(self, target_url: str) -> WorkdayUnit1PageCondition:
        """Open only the leased job; Unit 1 owns every later transition."""
        parsed = urlsplit(target_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not _WORKDAY_HOST.search(host):
            raise WorkdayWorkerError(
                "Navigation outside the leased Workday target was blocked."
            )
        logger.info("workday_approved_job_navigation_started host=%s", host)
        await self._page.goto(
            target_url, wait_until="domcontentloaded", timeout=self._timeout_ms
        )
        self._assert_current_workday_url()
        # Workday is a hydrated SPA: DOMContentLoaded can fire before the Apply
        # control exists. Wait for a stable terminal state or a visible Apply
        # control before Unit 1 takes its structural observation.
        state, action = await self._wait_for_known_apply_readiness()
        if state is not NativeAccountPageState.UNKNOWN:
            logger.info("workday_approved_job_state_ready page_state=%s", state.value)
            return self._condition_for_native_state(state)
        if action is None:
            raise WorkdayWorkerError(
                "A visible Workday Apply action was not found after navigation.",
                safe_code="workday_apply_action_not_selected",
            )
        logger.info("workday_approved_job_apply_control_ready")
        return self._condition_for_native_state(NativeAccountPageState.UNKNOWN)

    async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
        """Return bounded structural controls without values or retained DOM handles."""
        account_scope, scope_kind = await self._visible_account_control_scope()
        scope = account_scope if account_scope is not None else self._page
        observed = await self._priority_transition_controls(
            scope, scope_kind=scope_kind, limit=limit
        )
        priority_counts: dict[tuple[str, str], int] = {}
        for item in observed:
            key = (item.role, " ".join(item.semantic_name.split()).casefold())
            priority_counts[key] = priority_counts.get(key, 0) + 1
        controls = scope.locator(
            "button, [role='button'], a[href], [role='link'], input, textarea, "
            "select, form, [role='status'], [role='progressbar'], [role='alert']"
        )
        for index in range(min(await controls.count(), limit)):
            if len(observed) >= limit:
                break
            control = controls.nth(index)
            details = await control.evaluate(
                """element => ({
                    tag: (element.tagName || '').toLowerCase(),
                    role: (element.getAttribute('role') || '').toLowerCase(),
                    name: element.getAttribute('aria-label') ||
                          (element.labels && element.labels.length
                              ? Array.from(element.labels)
                                  .map(label => label.innerText || label.textContent || '')
                                  .join(' ')
                              : '') ||
                          element.innerText || element.title || '',
                    inputType: (element.getAttribute('type') || '').toLowerCase(),
                    isEmail: element.matches(
                        "input[type='email'], input[autocomplete='email'], " +
                        "input[autocomplete='username'], input[name*='email' i], " +
                        "input[data-automation-id='email']"
                    ),
                    busy: element.getAttribute('aria-busy') === 'true',
                })"""
            )
            if not isinstance(details, dict):
                continue
            role = str(details.get("role", ""))
            tag = str(details.get("tag", ""))
            input_type = str(details.get("inputType", ""))
            if details.get("isEmail") is True:
                input_type = "email"
            if not role:
                role = {
                    "a": "link",
                    "button": "button",
                    "form": "form",
                    "select": "combobox",
                    "textarea": "textbox",
                }.get(tag, "")
            if tag == "input" and not role:
                role = input_type if input_type in {"checkbox", "radio"} else "textbox"
            if not role:
                continue
            name = str(details.get("name", ""))
            priority_key = (role, " ".join(name.split()).casefold())
            remaining = priority_counts.get(priority_key, 0)
            if remaining:
                priority_counts[priority_key] = remaining - 1
                continue
            observed.append(
                WorkdaySemanticControl(
                    role=role,
                    semantic_name=name,
                    input_type=input_type,
                    visible=await control.is_visible(),
                    enabled=await control.is_enabled(),
                    busy=bool(details.get("busy", False)),
                    scope_kind=scope_kind,
                )
            )
        heading_loc = getattr(self._page, "locator", None)
        headings: list[str] = []
        if callable(heading_loc):
            try:
                loc = self._page.locator(self._HEADING_SELECTOR)
                if hasattr(loc, "all_inner_texts"):
                    headings = await loc.all_inner_texts()
            except Exception:
                headings = []
        has_info_heading = any(
            _INFORMATION_SECTION_HEADING.fullmatch(" ".join(h.split()))
            for h in headings[:20]
        )
        navigation_names = {
            " ".join(item.semantic_name.split())
            for item in observed
            if item.role in {"button", "link"}
        }
        if not has_info_heading and any(
            _APPLY_ACTION.fullmatch(name) is not None
            or _APPLY_MANUALLY_ACTION.fullmatch(name) is not None
            for name in navigation_names
        ):
            observed = [
                item
                for item in observed
                if item.role not in {"checkbox", "combobox", "radio", "textbox"}
            ]
        return WorkdayPageStructure(url=self._page.url, controls=tuple(observed))

    async def _priority_transition_controls(
        self,
        scope: Any,
        *,
        scope_kind: WorkdayControlScope,
        limit: int,
    ) -> list[WorkdaySemanticControl]:
        """Capture exact Unit 1 actions before the bounded generic DOM slice."""
        observed: list[WorkdaySemanticControl] = []
        actions = (
            (_APPLY_MANUALLY_ACTION, "Apply Manually"),
            (_APPLY_ACTION, "Apply"),
            (_LOGIN_ACTION, "Sign In"),
            (_CREATE_ACCOUNT_ACTION, "Create Account"),
            (_NEXT_APPLICATION_STEP_ACTION, "Save and Continue"),
        )
        get_by_role = getattr(scope, "get_by_role", None)
        if not callable(get_by_role):
            return observed
        for pattern, semantic_name in actions:
            for role in ("button", "link"):
                locators = get_by_role(role, name=pattern)
                for index in range(min(await locators.count(), limit)):
                    candidate = locators.nth(index)
                    if not await candidate.is_visible():
                        continue
                    observed.append(
                        WorkdaySemanticControl(
                            role=role,
                            semantic_name=semantic_name,
                            visible=True,
                            enabled=await candidate.is_enabled(),
                            scope_kind=scope_kind,
                        )
                    )
                    if len(observed) >= limit:
                        return observed
        return observed

    async def execute_candidate(
        self, *, candidate_id: str, action_intent: PortalControlIntent
    ) -> None:
        """Execute one fresh allowlisted structural candidate."""
        patterns = {
            PortalControlIntent.APPLY: _APPLY_ACTION,
            PortalControlIntent.APPLY_MANUALLY: _APPLY_MANUALLY_ACTION,
            PortalControlIntent.OPEN_REGISTRATION: _CREATE_ACCOUNT_ACTION,
            PortalControlIntent.SIGN_IN: _LOGIN_ACTION,
        }
        pattern = patterns.get(action_intent)
        if pattern is None:
            raise WorkdayWorkerError("The Unit 1 action intent is not executable.")
        action_scope = None
        if action_intent is PortalControlIntent.SIGN_IN:
            action_scope = await self._visible_account_dialog()
        candidates, locators = await self._visible_action_candidates(action_scope)
        ordinal = 0
        matches: list[Any] = []
        for candidate in candidates:
            name = (candidate.accessible_name or candidate.text).strip()
            if pattern.fullmatch(name) is None:
                continue
            role = candidate.role.strip().casefold() or (
                "link" if candidate.tag.strip().casefold() == "a" else "button"
            )
            digest = hashlib.sha256(
                f"{role}:{action_intent.value}:{ordinal}".encode("ascii")
            ).hexdigest()[:16]
            ordinal += 1
            if candidate_id == f"wdc-{digest}":
                locator = locators.get(candidate.candidate_id)
                if (
                    locator is not None
                    and await locator.is_visible()
                    and await locator.is_enabled()
                ):
                    matches.append(locator)
        if len(matches) != 1:
            raise WorkdayWorkerError("The Unit 1 transition candidate was not unique.")
        previous_account_state = (
            await self.detect_account_state()
            if action_intent
            in {
                PortalControlIntent.OPEN_REGISTRATION,
                PortalControlIntent.SIGN_IN,
            }
            else None
        )
        await self._click_and_adopt_workday_popup(matches[0])
        if previous_account_state is not None:
            await self._wait_for_account_state_change(previous_account_state)

    async def observe_post_submit(self) -> WorkdayPostSubmitObservation:
        """Map one read-only post-submit page observation to broker facts."""
        try:
            signals = await self._capture_account_signals()
        except Exception as exc:
            if not _is_playwright_navigation_context_error(exc):
                raise
            logger.info(
                "workday_post_submit_observation_deferred "
                "reason=navigation_context_changed"
            )
            return WorkdayPostSubmitObservation(frozenset(), terminal=False)
        state = classify_workday_account_state(signals)
        trusted_portal_until = extract_trusted_workday_unlock_time(
            signals, observed_at=datetime.now(UTC)
        )
        failure = {
            NativeAccountPageState.AUTHENTICATED: WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
            NativeAccountPageState.LOGIN_COMPLETE: WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS,
            NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED: WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
            NativeAccountPageState.CAPTCHA: WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.OTP: WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS: WorkdayFailureClass.POST_SUBMIT_ACCOUNT_EXISTS,
            NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND: WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
            NativeAccountPageState.LOGIN_INVALID_CREDENTIALS: WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
        }.get(state)
        if failure is None:
            terminal = state not in {
                NativeAccountPageState.UNKNOWN,
                NativeAccountPageState.LOGIN_REQUIRED,
                NativeAccountPageState.REGISTRATION_REQUIRED,
            }
            logger.info(
                "workday_post_submit_observed state=%s terminal=%s",
                state.value,
                terminal,
            )
            return WorkdayPostSubmitObservation(
                (
                    frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN})
                    if terminal
                    else frozenset()
                ),
                terminal=terminal,
                trusted_portal_until=trusted_portal_until,
            )
        logger.info(
            "workday_post_submit_observed state=%s terminal=true outcome=%s",
            state.value,
            failure.value,
        )
        return WorkdayPostSubmitObservation(
            frozenset({failure}),
            trusted_portal_until=trusted_portal_until,
        )

    async def open_and_start_apply(self, target_url: str) -> None:
        parsed = urlsplit(target_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not _WORKDAY_HOST.search(host):
            raise WorkdayWorkerError(
                "Navigation outside the leased Workday target was blocked."
            )
        logger.info("workday_navigation_started host=%s", host)
        await self._page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=self._timeout_ms,
        )
        self._assert_current_workday_url()
        state, apply_action = await self._wait_for_state_or_action(
            PortalControlIntent.APPLY, _APPLY_ACTION
        )
        if state is not NativeAccountPageState.UNKNOWN:
            logger.info("workday_apply_navigation_state page_state=%s", state.value)
            return

        if apply_action is None:
            raise WorkdayWorkerError(
                "A visible Workday Apply action was not found.",
                safe_code="workday_apply_action_not_selected",
            )
        await self._click_and_adopt_workday_popup(apply_action)
        logger.info("workday_apply_action_completed")

        state, manual_action = await self._wait_for_state_or_action(
            PortalControlIntent.APPLY_MANUALLY, _APPLY_MANUALLY_ACTION
        )
        if state is not NativeAccountPageState.UNKNOWN:
            logger.info("workday_manual_apply_state page_state=%s", state.value)
            return
        if manual_action is None:
            raise WorkdayWorkerError(
                "A Workday account screen or Apply Manually action was not found.",
                safe_code="workday_apply_manually_not_selected",
            )
        await self._click_and_adopt_workday_popup(manual_action)
        logger.info("workday_manual_apply_action_completed")
        state = await self._wait_for_account_state()
        if state is NativeAccountPageState.UNKNOWN:
            raise WorkdayWorkerError(
                "The Workday account screen did not become available.",
                safe_code="workday_account_screen_not_confirmed",
            )

    async def _click_and_adopt_workday_popup(self, action: Any) -> None:
        existing_pages = tuple(self._page.context.pages)
        await action.click(timeout=self._timeout_ms)
        await self._page.wait_for_timeout(500)
        new_pages = [
            page for page in self._page.context.pages if page not in existing_pages
        ]
        if new_pages:
            popup = new_pages[-1]
            popup_url = urlsplit(popup.url)
            popup_host = (popup_url.hostname or "").lower()
            if popup_url.scheme != "https" or not _WORKDAY_HOST.search(popup_host):
                await popup.close()
                raise WorkdayWorkerError(
                    "The Workday Apply action opened an unapproved target."
                )
            self._page = popup
        self._assert_current_workday_url()

    async def detect_account_state(self) -> NativeAccountPageState:
        signals = await self._capture_account_signals()
        return classify_workday_account_state(signals)

    async def capture_page_condition(self) -> WorkdayUnit1PageCondition:
        """Capture a fresh typed condition without exposing page content."""
        try:
            signals = await self._capture_account_signals()
        except WorkdayWorkerError as exc:
            raise WorkdayStateSecurityError() from exc
        state = classify_workday_account_state(signals)
        failure = {
            NativeAccountPageState.JOB_UNAVAILABLE: WorkdayFailureClass.JOB_UNAVAILABLE,
            NativeAccountPageState.CAPTCHA: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.OTP: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.TRANSIENT_FAILURE: WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
        }.get(state)
        return WorkdayUnit1PageCondition(
            native_state=state,
            pre_submit_failure=failure,
            already_authenticated=state
            in {
                NativeAccountPageState.AUTHENTICATED,
                NativeAccountPageState.LOGIN_COMPLETE,
            },
            account_dialog_active=signals.has_account_dialog,
            confirmed_account_lock=(
                state is NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED
            ),
            trusted_unlock_time=extract_trusted_workday_unlock_time(
                signals, observed_at=datetime.now(UTC)
            ),
        )

    async def capture_checkpoint_evidence(
        self,
        *,
        target_url: str,
        expected_tenant_scope: str,
        application_id: UUID,
        account_binding_verified: bool,
        application_context_matches: bool,
    ) -> WorkdayPrivateCheckpointEvidence:
        """Inspect the live page privately and return only checkpoint facts."""
        del application_id
        try:
            current = urlsplit(self._page.url)
            target = urlsplit(target_url)
            approved_origin = current.scheme.casefold() == "https" and bool(
                _WORKDAY_HOST.search((current.hostname or "").casefold())
            )
            current_scope = derive_workday_portal_scope(self._page.url)
            tenant_verified = current_scope == expected_tenant_scope
            signals = await self._capture_account_signals()
            state = classify_workday_account_state(signals)
            job_context_matches = await self._private_job_context_matches(target)
            basic_heading, basic_control_count = (
                await self._private_basic_information_structure()
            )
            basic_control_hydrated = basic_heading and basic_control_count > 0
            no_auth_error = (
                not signals.has_account_dialog
                and not signals.account_alert_text.strip()
                and state
                not in {
                    NativeAccountPageState.LOGIN_REQUIRED,
                    NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
                    NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
                    NativeAccountPageState.REGISTRATION_REQUIRED,
                    NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS,
                    NativeAccountPageState.TRANSIENT_FAILURE,
                }
            )
            no_challenge_or_lock = state not in {
                NativeAccountPageState.CAPTCHA,
                NativeAccountPageState.OTP,
                NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED,
            }
            facts = {
                "approved_https_origin": approved_origin,
                "canonical_tenant_verified": tenant_verified,
                "leased_job_context_matches": job_context_matches,
                "leased_application_context_matches": application_context_matches,
                "external_account_matches": account_binding_verified,
                "no_login_or_auth_error": no_auth_error,
                "no_captcha_or_otp_or_lock": no_challenge_or_lock,
                "basic_information_control_hydrated": basic_control_hydrated,
                "basic_control_count": basic_control_count,
                "target_path_present": bool(target.path),
            }
            logger.info(
                "workday_checkpoint_structure_observed "
                "native_state=%s basic_heading_matched=%s basic_control_count=%s",
                state.value,
                basic_heading,
                basic_control_count,
            )
        except Exception as exc:
            logger.info(
                "workday_checkpoint_structure_observation_failed error_type=%s",
                type(exc).__name__,
            )
            facts = {
                "approved_https_origin": False,
                "canonical_tenant_verified": False,
                "leased_job_context_matches": False,
                "leased_application_context_matches": False,
                "external_account_matches": False,
                "no_login_or_auth_error": False,
                "no_captcha_or_otp_or_lock": False,
                "basic_information_control_hydrated": False,
                "basic_control_count": 0,
                "target_path_present": False,
            }
        signature_payload = json.dumps(
            {
                "approved_https_origin": facts["approved_https_origin"],
                "canonical_tenant_verified": facts["canonical_tenant_verified"],
                "leased_job_context_matches": facts["leased_job_context_matches"],
                "leased_application_context_matches": facts[
                    "leased_application_context_matches"
                ],
                "external_account_matches": facts["external_account_matches"],
                "no_login_or_auth_error": facts["no_login_or_auth_error"],
                "no_captcha_or_otp_or_lock": facts["no_captcha_or_otp_or_lock"],
                "basic_information_control_hydrated": facts[
                    "basic_information_control_hydrated"
                ],
                "target_path_present": facts["target_path_present"],
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return WorkdayPrivateCheckpointEvidence(
            approved_https_origin=facts["approved_https_origin"],
            canonical_tenant_verified=facts["canonical_tenant_verified"],
            leased_job_context_matches=facts["leased_job_context_matches"],
            leased_application_context_matches=facts[
                "leased_application_context_matches"
            ],
            external_account_matches=facts["external_account_matches"],
            no_login_or_auth_error=facts["no_login_or_auth_error"],
            no_captcha_or_otp_or_lock=facts["no_captcha_or_otp_or_lock"],
            basic_information_control_hydrated=facts[
                "basic_information_control_hydrated"
            ],
            safe_signature=("wdcp1:" + hashlib.sha256(signature_payload).hexdigest()),
        )

    async def _private_job_context_matches(self, target: Any) -> bool:
        """Compare private job markers without returning their values."""
        current_text = (f"{self._page.url} {urlsplit(self._page.url).query}").casefold()
        target_path = unquote(target.path).rstrip("/").casefold()
        target_leaf = target_path.rsplit("/", 1)[-1]
        if target_path and target_path in current_text:
            return True
        if target_leaf and len(target_leaf) >= 4 and target_leaf in current_text:
            return True
        marker_locator = self._page.locator(
            "[data-job-id], [data-job-id*='job' i], " "[data-automation-id*='job' i]"
        )
        markers = await marker_locator.evaluate_all(
            """elements => elements.slice(0, 40).map(element => [
                element.getAttribute('data-job-id') || '',
                element.getAttribute('data-automation-id') || '',
                element.getAttribute('href') || ''
            ].join(' '))"""
        )
        return any(
            isinstance(marker, str)
            and target_leaf
            and target_leaf in unquote(marker).casefold()
            for marker in markers
        )

    async def _private_basic_information_structure(self) -> tuple[bool, int]:
        """Require the tenant's first information section and a hydrated control."""
        if await self._visible_account_dialog() is not None:
            return False, 0
        headings = await self._page.locator(self._HEADING_SELECTOR).all_inner_texts()
        matched_heading = next(
            (
                " ".join(text.split()).casefold().replace(" ", "_")
                for text in headings[:30]
                if _INFORMATION_SECTION_HEADING.fullmatch(" ".join(text.split()))
                is not None
            ),
            None,
        )
        if matched_heading is None:
            return False, 0
        controls = self._page.locator(self._APPLICATION_FIELD_SELECTOR)
        hydrated_count = 0
        for index in range(min(await controls.count(), 80)):
            control = controls.nth(index)
            if not await control.is_visible() or not await control.is_enabled():
                continue
            details = await control.evaluate(
                """element => ({
                    type: (element.getAttribute('type') || '').toLowerCase(),
                    busy: element.getAttribute('aria-busy') === 'true',
                    disabled: element.hasAttribute('disabled'),
                    hidden: element.getAttribute('aria-hidden') === 'true'
                })"""
            )
            if not isinstance(details, dict):
                continue
            if details.get("type") in {"hidden", "password", "submit", "button"}:
                continue
            if details.get("busy") or details.get("disabled") or details.get("hidden"):
                continue
            hydrated_count += 1
        next_action_count = 0
        get_by_role = getattr(self._page, "get_by_role", None)
        if callable(get_by_role):
            for role in ("button", "link"):
                actions = get_by_role(role, name=_NEXT_APPLICATION_STEP_ACTION)
                for index in range(min(await actions.count(), 3)):
                    action = actions.nth(index)
                    if await action.is_visible() and await action.is_enabled():
                        next_action_count += 1
        logger.info(
            "workday_information_step_structure_observed "
            "heading_kind=%s application_field_count=%s next_action_count=%s",
            matched_heading,
            hydrated_count,
            next_action_count,
        )
        if hydrated_count > 0:
            return True, hydrated_count
        return True, 1 if next_action_count == 1 else 0

    async def wait_for_hydration(self, milliseconds: int) -> None:
        """Yield to the SPA for a bounded, read-only state refresh."""
        if type(milliseconds) is not int or not 1 <= milliseconds <= 1000:
            raise ValueError("Hydration wait must be between 1 and 1000 milliseconds.")
        await self._page.wait_for_timeout(milliseconds)

    async def _capture_account_signals(self) -> WorkdayAccountSignals:
        self._assert_current_workday_url()
        alerts = await self._page.locator(self._ALERT_SELECTOR).all_inner_texts()
        headings = await self._page.locator(self._HEADING_SELECTOR).all_inner_texts()
        account_dialog = await self._visible_account_dialog()
        trusted_messages = await self._visible_trusted_message_texts()
        signals = WorkdayAccountSignals(
            has_email_input=await self._has_visible(self._EMAIL_SELECTOR),
            password_input_count=await self._visible_count(self._PASSWORD_SELECTOR),
            has_login_action=(
                await self._first_visible_role(("button",), _LOGIN_ACTION) is not None
            ),
            has_create_account_action=(
                await self._first_visible_role(
                    ("button", "link"), _CREATE_ACCOUNT_ACTION
                )
                is not None
            ),
            has_account_dialog=account_dialog is not None,
            dialog_has_email_input=(
                account_dialog is not None
                and await self._has_visible_in(account_dialog, self._EMAIL_SELECTOR)
            ),
            dialog_password_input_count=(
                await self._visible_count_in(account_dialog, self._PASSWORD_SELECTOR)
                if account_dialog is not None
                else 0
            ),
            dialog_has_login_action=(
                account_dialog is not None
                and await self._first_visible_role_in(
                    account_dialog, ("button",), _LOGIN_ACTION
                )
                is not None
            ),
            has_application_action=(
                await self._first_visible_role(
                    ("button",),
                    re.compile(r"^(?:next|save\s+(?:and|&)\s+continue|submit)$", re.I),
                )
                is not None
            ),
            has_captcha=await self._has_active_captcha(),
            has_otp_input=await self._has_visible(self._OTP_SELECTOR),
            has_job_unavailable_notice=any(
                _JOB_UNAVAILABLE.fullmatch(text[:500])
                for text in [*headings[:20], *alerts[:20]]
            ),
            account_alert_text=" ".join(alerts)[:4000],
            trusted_account_message_text=" ".join(trusted_messages)[:4000],
            url=self._page.url,
        )
        return signals

    @staticmethod
    def _condition_for_native_state(
        state: NativeAccountPageState,
    ) -> WorkdayUnit1PageCondition:
        failure = {
            NativeAccountPageState.JOB_UNAVAILABLE: WorkdayFailureClass.JOB_UNAVAILABLE,
            NativeAccountPageState.CAPTCHA: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.OTP: WorkdayFailureClass.PRE_SUBMIT_CAPTCHA_OR_OTP,
            NativeAccountPageState.TRANSIENT_FAILURE: WorkdayFailureClass.PRE_SUBMIT_TRANSIENT,
        }.get(state)
        return WorkdayUnit1PageCondition(
            native_state=state,
            pre_submit_failure=failure,
            already_authenticated=state
            in {
                NativeAccountPageState.AUTHENTICATED,
                NativeAccountPageState.LOGIN_COMPLETE,
            },
            confirmed_account_lock=(
                state is NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED
            ),
        )

    async def open_registration(self) -> None:
        action = await self._resolve_action(
            PortalControlIntent.OPEN_REGISTRATION,
            ("button", "link"),
            _CREATE_ACCOUNT_ACTION,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A visible Create Account action was not found.",
                safe_code="workday_open_registration_not_selected",
            )
        await action.click(timeout=self._timeout_ms)
        await self._page.wait_for_timeout(500)
        self._assert_current_workday_url()
        logger.info("workday_registration_opened")

    async def open_login(self) -> None:
        action = await self._resolve_action(
            PortalControlIntent.SIGN_IN,
            ("button", "link"),
            _LOGIN_ACTION,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A visible existing-account Sign In action was not found.",
                safe_code="workday_open_login_not_selected",
            )
        await action.click(timeout=self._timeout_ms)
        await self._wait_for_account_state_change(
            NativeAccountPageState.REGISTRATION_REQUIRED
        )
        self._assert_current_workday_url()
        logger.info("workday_login_opened")

    async def open_registration_after_rejected_login(
        self, *, expected_scope: str
    ) -> None:
        """Open one visible registration form after one explicit login rejection."""
        self._assert_current_workday_url()
        if derive_workday_portal_scope(self._page.url) != expected_scope:
            raise WorkdayWorkerError(
                "The rejected login is outside the expected Workday tenant."
            )
        previous_state = await self.detect_account_state()
        if previous_state not in {
            NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
            NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
        }:
            raise WorkdayWorkerError(
                "Registration fallback requires an explicit login rejection."
            )
        account_dialog = await self._visible_account_dialog()
        if account_dialog is None:
            raise WorkdayWorkerError(
                "The rejected Workday login dialog is no longer available."
            )
        create_count = await self._visible_role_count_in(
            account_dialog, ("button", "link"), _CREATE_ACCOUNT_ACTION
        )
        if create_count == 1:
            action = await self._first_visible_role_in(
                account_dialog, ("button", "link"), _CREATE_ACCOUNT_ACTION
            )
            if action is None:  # pragma: no cover - count/action invariant
                raise WorkdayWorkerError(
                    "The Workday Create Account action became unavailable."
                )
            await action.click(timeout=self._timeout_ms)
        elif create_count == 0:
            close_count = await self._visible_role_count_in(
                account_dialog, ("button",), _CLOSE_DIALOG_ACTION
            )
            if close_count != 1:
                raise WorkdayWorkerError(
                    "The rejected Workday login dialog cannot be closed uniquely."
                )
            close_action = await self._first_visible_role_in(
                account_dialog, ("button",), _CLOSE_DIALOG_ACTION
            )
            if close_action is None:  # pragma: no cover - count/action invariant
                raise WorkdayWorkerError(
                    "The Workday login close action became unavailable."
                )
            await close_action.click(timeout=self._timeout_ms)
            await self._page.wait_for_timeout(250)
            if (
                await self.detect_account_state()
                is NativeAccountPageState.REGISTRATION_REQUIRED
            ):
                self._assert_current_workday_url()
                logger.info("workday_registration_fallback_opened")
                return
            page_create_count = await self._visible_role_count_in(
                self._page, ("button", "link"), _CREATE_ACCOUNT_ACTION
            )
            if page_create_count != 1:
                raise WorkdayWorkerError(
                    "The Workday page does not contain one Create Account action."
                )
            action = await self._first_visible_role_in(
                self._page, ("button", "link"), _CREATE_ACCOUNT_ACTION
            )
            if action is None:  # pragma: no cover - count/action invariant
                raise WorkdayWorkerError(
                    "The Workday Create Account action became unavailable."
                )
            await action.click(timeout=self._timeout_ms)
        else:
            raise WorkdayWorkerError(
                "The rejected Workday login dialog has ambiguous Create Account actions."
            )
        reached_state = await self._wait_for_account_state_change(previous_state)
        if reached_state is not NativeAccountPageState.REGISTRATION_REQUIRED:
            raise WorkdayWorkerError(
                "The Workday Create Account action did not open a registration form."
            )
        self._assert_current_workday_url()
        logger.info("workday_registration_fallback_opened")

    async def open_registration_from_login_form(self, *, expected_scope: str) -> None:
        """Open signup from one verified login form without submitting login."""
        self._assert_current_workday_url()
        if derive_workday_portal_scope(self._page.url) != expected_scope:
            raise WorkdayWorkerError(
                "The Workday login form is outside the expected tenant."
            )
        previous_state = await self.detect_account_state()
        if previous_state is not NativeAccountPageState.LOGIN_REQUIRED:
            raise WorkdayWorkerError(
                "Registration entry requires one confirmed Workday login form."
            )
        if not await self.verify_unique_auth_controls(expected_scope=expected_scope):
            raise WorkdayWorkerError(
                "The Workday login form is not structurally unique."
            )
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            raise WorkdayWorkerError("The Workday login form was not available.")
        create_count = await self._visible_role_count_in(
            account_scope, ("button", "link"), _CREATE_ACCOUNT_ACTION
        )
        if create_count != 1:
            raise WorkdayWorkerError(
                "The Workday login form does not contain one Create Account action."
            )
        action = await self._first_visible_role_in(
            account_scope, ("button", "link"), _CREATE_ACCOUNT_ACTION
        )
        if action is None or not await action.is_enabled():
            raise WorkdayWorkerError(
                "The Workday Create Account action is unavailable."
            )
        await action.click(timeout=self._timeout_ms)
        self._verified_auth_scope = None
        reached_state = await self._wait_for_account_state_change(previous_state)
        if reached_state is not NativeAccountPageState.REGISTRATION_REQUIRED:
            raise WorkdayWorkerError(
                "The Workday Create Account action did not open registration."
            )
        if not await self.verify_unique_registration_controls(
            expected_scope=expected_scope
        ):
            raise WorkdayWorkerError(
                "The Workday registration form is not structurally unique."
            )
        logger.info("workday_registration_opened_from_login_form")

    async def verify_unique_auth_controls(self, *, expected_scope: str) -> bool:
        """Recheck origin/tenant and exactly one active-dialog login control set."""
        self._assert_current_workday_url()
        try:
            current_scope = derive_workday_portal_scope(self._page.url)
        except PortalCredentialError:
            return False
        if current_scope != expected_scope:
            return False
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            return False
        email_count = await self._visible_count_in(account_scope, self._EMAIL_SELECTOR)
        password_count = await self._visible_count_in(
            account_scope, self._PASSWORD_SELECTOR
        )
        sign_in_count = await self._visible_role_count_in(
            account_scope, ("button",), _LOGIN_ACTION
        )
        verified = email_count == password_count == sign_in_count == 1
        self._verified_auth_scope = expected_scope if verified else None
        return verified

    async def fill_verified_auth_controls(
        self, credential: WorkerPortalCredential
    ) -> None:
        if self._verified_auth_scope is None:
            raise WorkdayWorkerError("The Workday auth form was not verified.")
        if not await self.verify_unique_auth_controls(
            expected_scope=self._verified_auth_scope
        ):
            raise WorkdayWorkerError("The Workday auth form changed before fill.")
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            raise WorkdayWorkerError("The Workday login form was not available.")
        email = await self._first_visible_locator_in(
            account_scope, self._EMAIL_SELECTOR
        )
        password = await self._first_visible_locator_in(
            account_scope, self._PASSWORD_SELECTOR
        )
        if email is None or password is None:
            raise WorkdayWorkerError("The Workday login fields were not available.")
        await email.fill(credential.account_email, timeout=self._timeout_ms)
        await password.fill(credential.password, timeout=self._timeout_ms)
        logger.info("workday_login_fields_filled")

    async def verify_unique_registration_controls(self, *, expected_scope: str) -> bool:
        """Verify one tenant-bound registration form before secret access."""
        self._assert_current_workday_url()
        try:
            current_scope = derive_workday_portal_scope(self._page.url)
        except PortalCredentialError:
            return False
        if current_scope != expected_scope:
            return False
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            return False
        email_count = await self._visible_count_in(account_scope, self._EMAIL_SELECTOR)
        password_count = await self._visible_count_in(
            account_scope, self._PASSWORD_SELECTOR
        )
        create_count = await self._visible_role_count_in(
            account_scope, ("button",), _CREATE_ACCOUNT_ACTION
        )
        verified = email_count == 1 and password_count == 2 and create_count == 1
        self._verified_registration_scope = expected_scope if verified else None
        return verified

    async def fill_verified_registration_controls(
        self, credential: WorkerPortalCredential
    ) -> None:
        """Fill only the registration form proven by the broker."""
        if self._verified_registration_scope is None:
            raise WorkdayWorkerError("The Workday registration form was not verified.")
        if not await self.verify_unique_registration_controls(
            expected_scope=self._verified_registration_scope
        ):
            raise WorkdayWorkerError(
                "The Workday registration form changed before fill."
            )
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            raise WorkdayWorkerError("The Workday registration form was unavailable.")
        email = await self._first_visible_locator_in(
            account_scope, self._EMAIL_SELECTOR
        )
        passwords = account_scope.locator(self._PASSWORD_SELECTOR)
        visible_passwords = [
            passwords.nth(index)
            for index in range(await passwords.count())
            if await passwords.nth(index).is_visible()
        ]
        if email is None or len(visible_passwords) != 2:
            raise WorkdayWorkerError(
                "The Workday registration fields were not available."
            )
        terms_consent = await self._first_visible_role_in(
            account_scope, ("checkbox",), WORKDAY_ACCOUNT_TERMS_CONSENT
        )
        if terms_consent is not None:
            if not self._accept_account_terms:
                raise WorkdayWorkerError(
                    "Explicit approval is required for Workday account terms."
                )
            if not await terms_consent.is_checked():
                await terms_consent.check(timeout=self._timeout_ms)
                logger.info("workday_account_terms_checked")
        await email.fill(credential.account_email, timeout=self._timeout_ms)
        await visible_passwords[0].fill(credential.password, timeout=self._timeout_ms)
        await visible_passwords[1].fill(credential.password, timeout=self._timeout_ms)
        logger.info("workday_registration_fields_filled")

    async def click_verified_create_account(self) -> None:
        """Submit exactly one previously verified registration form."""
        if self._verified_registration_scope is None:
            raise WorkdayWorkerError("The Workday registration form was not verified.")
        if not await self.verify_unique_registration_controls(
            expected_scope=self._verified_registration_scope
        ):
            raise WorkdayWorkerError(
                "The Workday registration form changed before submit."
            )
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            raise WorkdayWorkerError("The Workday registration form was unavailable.")
        action = await self._resolve_action(
            PortalControlIntent.CREATE_ACCOUNT,
            ("button",),
            _CREATE_ACCOUNT_ACTION,
            scope=account_scope,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A visible Create Account action was not found.",
                safe_code="workday_create_account_not_selected",
            )
        await action.click(timeout=self._timeout_ms)
        self._verified_registration_scope = None
        logger.info("workday_registration_submitted")
        await self._wait_for_account_state_change(
            NativeAccountPageState.REGISTRATION_REQUIRED
        )

    async def click_verified_sign_in(self) -> None:
        if self._verified_auth_scope is None:
            raise WorkdayWorkerError("The Workday auth form was not verified.")
        if not await self.verify_unique_auth_controls(
            expected_scope=self._verified_auth_scope
        ):
            raise WorkdayWorkerError("The Workday auth form changed before submit.")
        account_scope, scope_kind = await self._visible_account_control_scope()
        if account_scope is None or scope_kind is WorkdayControlScope.PAGE:
            raise WorkdayWorkerError("The Workday login form was not available.")
        action = await self._resolve_action(
            PortalControlIntent.SIGN_IN,
            ("button",),
            _LOGIN_ACTION,
            scope=account_scope,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A visible Sign In action was not found.",
                safe_code="workday_sign_in_not_selected",
            )
        await action.click(timeout=self._timeout_ms)
        self._verified_auth_scope = None
        logger.info("workday_login_submitted")

    async def fill_registration(self, credential: WorkerPortalCredential) -> None:
        email = await self._first_visible_locator(self._EMAIL_SELECTOR)
        passwords = self._page.locator(self._PASSWORD_SELECTOR)
        visible_passwords = []
        for index in range(await passwords.count()):
            candidate = passwords.nth(index)
            if await candidate.is_visible():
                visible_passwords.append(candidate)
        if email is None or len(visible_passwords) < 2:
            raise WorkdayWorkerError(
                "The Workday registration fields were not available."
            )
        terms_consent = await self._first_visible_role(
            ("checkbox",), WORKDAY_ACCOUNT_TERMS_CONSENT
        )
        if terms_consent is not None:
            if not self._accept_account_terms:
                raise WorkdayWorkerError(
                    "Explicit approval is required for Workday account terms."
                )
            if not await terms_consent.is_checked():
                await terms_consent.check(timeout=self._timeout_ms)
                logger.info("workday_account_terms_checked")
        await email.fill(credential.account_email, timeout=self._timeout_ms)
        await visible_passwords[0].fill(credential.password, timeout=self._timeout_ms)
        await visible_passwords[1].fill(credential.password, timeout=self._timeout_ms)
        logger.info("workday_registration_fields_filled")

    async def submit_registration(self) -> None:
        action = await self._resolve_action(
            PortalControlIntent.CREATE_ACCOUNT,
            ("button",),
            _CREATE_ACCOUNT_ACTION,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A visible Create Account action was not found.",
                safe_code="workday_create_account_not_selected",
            )
        await action.click(timeout=self._timeout_ms)
        logger.info("workday_registration_submitted")
        await self._wait_for_account_state_change(
            NativeAccountPageState.REGISTRATION_REQUIRED
        )

    async def scan_application_fields(self) -> tuple[str, list[WorkdayFormField]]:
        """Discover visible controls without portal-specific field selectors."""
        self._assert_current_workday_url()
        selector = self._APPLICATION_FIELD_SELECTOR
        await self._wait_for_application_form_controls(selector)
        controls = self._page.locator(selector)
        fields: list[WorkdayFormField] = []
        self._form_locators = {}
        for index in range(min(await controls.count(), 80)):
            control = controls.nth(index)
            if not await control.is_visible():
                continue
            details = await control.evaluate(
                r"""element => {
                    const labels = element.labels ? Array.from(element.labels) : [];
                    const labelledBy = (element.getAttribute('aria-labelledby') || '')
                      .split(/\s+/).filter(Boolean)
                      .map(id => document.getElementById(id)?.innerText || '').join(' ');
                    const label = labels.map(x => x.innerText || x.textContent || '').join(' ')
                      || labelledBy || element.getAttribute('aria-label')
                      || element.getAttribute('placeholder') || element.getAttribute('name') || '';
                    const options = element.tagName === 'SELECT'
                      ? Array.from(element.options).slice(0, 40).map(o => ({
                          value: String(o.value || '').slice(0, 500),
                          text: String(o.text || '').trim().slice(0, 200)
                        })) : [];
                    return {
                      tag: String(element.tagName || '').toLowerCase(),
                      inputType: String(
                        element.getAttribute('role') === 'combobox'
                          ? 'combobox'
                          : (element.getAttribute('type') || element.getAttribute('role') || '')
                      ).toLowerCase(),
                      nameAttr: element.getAttribute('name'), idAttr: element.id || null,
                      labelText: String(label).replace(/\s+/g, ' ').trim().slice(0, 600),
                      placeholder: element.getAttribute('placeholder'),
                      ariaLabel: element.getAttribute('aria-label'),
                      required: Boolean(element.required || element.getAttribute('aria-required') === 'true'),
                      readonly: Boolean(element.readOnly), disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
                      currentValue: String(element.type === 'checkbox' || element.type === 'radio' ? element.checked : (element.value || '')).slice(0, 500),
                      maxLength: element.maxLength >= 0 ? element.maxLength : null,
                      options
                    };
                }"""
            )
            if not isinstance(details, dict):
                continue
            uid = str(len(fields))
            field = WorkdayFormField(
                field_uid=uid,
                tag=str(details.get("tag", ""))[:24],
                input_type=str(details.get("inputType", ""))[:32],
                name_attr=details.get("nameAttr"),
                id_attr=details.get("idAttr"),
                label_text=str(details.get("labelText", ""))[:600],
                placeholder=details.get("placeholder"),
                aria_label=details.get("ariaLabel"),
                required=bool(details.get("required")),
                readonly=bool(details.get("readonly")),
                disabled=bool(details.get("disabled")),
                current_value=details.get("currentValue"),
                max_length=details.get("maxLength"),
                options=tuple(details.get("options") or ()),
            )
            fields.append(field)
            self._form_locators[uid] = control
        logger.info("workday_form_fields_discovered field_count=%s", len(fields))
        return self._page.url, fields

    async def _wait_for_application_form_controls(self, selector: str) -> bool:
        """Wait for Workday's post-login application form to hydrate."""
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            if await self._has_visible(selector):
                logger.info("workday_form_controls_ready")
                return True
            if monotonic() >= deadline:
                logger.warning("workday_form_controls_readiness_timeout")
                return False
            await self._page.wait_for_timeout(250)

    async def fill_and_verify_application_fields(
        self, assignments: list[WorkdayFormAssignment]
    ) -> WorkdayFormFillResult:
        """Write supported approved values and verify the browser committed them."""
        expected: dict[str, tuple[str, str]] = {}
        unsupported: list[str] = []
        for assignment in assignments:
            if assignment.answer_source not in {"profile", "approved_rule"}:
                continue
            locator = self._form_locators.get(assignment.field_uid)
            if locator is None:
                unsupported.append(assignment.field_uid)
                continue
            control_type = str(
                await locator.evaluate(
                    "e => String(e.getAttribute('type') || e.getAttribute('role') || e.tagName || '').toLowerCase()"
                )
            )
            if control_type == "file":
                unsupported.append(assignment.field_uid)
                continue
            if control_type == "combobox":
                await locator.fill(assignment.value, timeout=self._timeout_ms)
                option = await self._wait_for_combobox_option(assignment.value)
                if option is None:
                    unsupported.append(assignment.field_uid)
                    continue
                await option.click(timeout=self._timeout_ms)
                expected[assignment.field_uid] = ("combobox", assignment.value)
            elif control_type in {"checkbox", "radio"}:
                if assignment.value.strip().casefold() not in {
                    "yes",
                    "true",
                    "checked",
                    "1",
                }:
                    unsupported.append(assignment.field_uid)
                    continue
                await locator.check(timeout=self._timeout_ms)
                expected[assignment.field_uid] = (control_type, "true")
            elif control_type in {"select", "select-one"}:
                try:
                    await locator.select_option(
                        label=assignment.value, timeout=self._timeout_ms
                    )
                except Exception:
                    await locator.select_option(
                        value=assignment.value, timeout=self._timeout_ms
                    )
                expected[assignment.field_uid] = ("select", assignment.value)
            else:
                await locator.fill(assignment.value, timeout=self._timeout_ms)
                expected[assignment.field_uid] = ("text", assignment.value)

        failed: list[str] = []
        for uid, (kind, wanted) in expected.items():
            locator = self._form_locators[uid]
            if kind in {"checkbox", "radio"}:
                verified = await locator.is_checked()
            elif kind == "select":
                actual = await locator.evaluate(
                    "e => ({value: String(e.value || ''), text: String(e.selectedOptions?.[0]?.text || '').trim()})"
                )
                verified = wanted in {actual.get("value"), actual.get("text")}
            else:
                verified = await locator.input_value() == wanted
            if not verified:
                failed.append(uid)
        logger.info(
            "workday_form_fill_verified filled_count=%s verified_count=%s failed_count=%s unsupported_count=%s",
            len(expected),
            len(expected) - len(failed),
            len(failed),
            len(unsupported),
        )
        return WorkdayFormFillResult(
            filled_count=len(expected),
            verified_count=len(expected) - len(failed),
            failed_field_uids=tuple(failed),
            unsupported_field_uids=tuple(unsupported),
        )

    async def _wait_for_combobox_option(self, value: str) -> Any | None:
        exact_value = re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            option = await self._first_visible_role(("option",), exact_value)
            if option is not None:
                return option
            if monotonic() >= deadline:
                return None
            await self._page.wait_for_timeout(250)

    async def advance_to_next_application_step(self) -> None:
        """Click one non-final navigation action and verify the form changed."""
        self._assert_current_workday_url()
        before_url = self._page.url
        before_signature = await self._application_field_signature()
        action = await self._resolve_action(
            PortalControlIntent.NEXT_APPLICATION_STEP,
            ("button",),
            _NEXT_APPLICATION_STEP_ACTION,
        )
        if action is None:
            raise WorkdayWorkerError(
                "A unique visible Next action was not found.",
                safe_code="workday_application_next_action_not_selected",
            )
        await action.click(timeout=self._timeout_ms)

        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            current_signature = await self._application_field_signature()
            if self._page.url != before_url or current_signature != before_signature:
                logger.info("workday_application_next_step_completed")
                return
            if monotonic() >= deadline:
                raise WorkdayWorkerError(
                    "The Workday application did not reach a confirmed next step.",
                    safe_code="workday_application_step_transition_timeout",
                )
            await self._page.wait_for_timeout(250)

    async def _application_field_signature(self) -> tuple[str, ...]:
        controls = self._page.locator(self._APPLICATION_FIELD_SELECTOR)
        signature: list[str] = []
        for index in range(min(await controls.count(), 80)):
            control = controls.nth(index)
            if not await control.is_visible():
                continue
            descriptor = await control.evaluate(
                """element => [
                    element.tagName || '',
                    element.getAttribute('type') || element.getAttribute('role') || '',
                    element.getAttribute('name') || '',
                    element.id || '',
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('data-automation-id') || ''
                ].join('|')"""
            )
            signature.append(str(descriptor)[:1000])
        return tuple(signature)

    def _assert_current_workday_url(self) -> None:
        parsed = urlsplit(self._page.url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not _WORKDAY_HOST.search(host):
            raise WorkdayWorkerError(
                "The Workday browser left its approved HTTPS origin."
            )

    async def _has_visible(self, selector: str) -> bool:
        return await self._first_visible_locator(selector) is not None

    async def _has_active_captcha(self) -> bool:
        """Ignore ambient/invisible CAPTCHA widgets until a challenge is rendered."""
        if await self._has_visible(self._CAPTCHA_INPUT_SELECTOR):
            return True
        widgets = self._page.locator(self._CAPTCHA_WIDGET_SELECTOR)
        for index in range(await widgets.count()):
            candidate = widgets.nth(index)
            if not await candidate.is_visible():
                continue
            active = await candidate.evaluate(
                """element => {
                    if (element.closest(
                        ".grecaptcha-badge, [data-size='invisible'], " +
                        "[aria-hidden='true']"
                    )) return false;
                    const rect = element.getBoundingClientRect();
                    return rect.width >= 180 && rect.height >= 50;
                }"""
            )
            if active is True:
                return True
        return False

    async def _has_visible_in(self, scope: Any, selector: str) -> bool:
        return await self._first_visible_locator_in(scope, selector) is not None

    async def _visible_account_dialog(self) -> Any | None:
        dialogs = self._page.locator(_DIALOG_SELECTOR)
        account_dialog = None
        for index in range(await dialogs.count()):
            candidate = dialogs.nth(index)
            if not await candidate.is_visible():
                continue
            has_account_control = (
                await self._has_visible_in(candidate, self._EMAIL_SELECTOR)
                or await self._has_visible_in(candidate, self._PASSWORD_SELECTOR)
                or await self._first_visible_role_in(
                    candidate, ("button",), _LOGIN_ACTION
                )
                is not None
                or await self._first_visible_role_in(
                    candidate, ("button", "link"), _CREATE_ACCOUNT_ACTION
                )
                is not None
            )
            if has_account_control:
                account_dialog = candidate
        return account_dialog

    async def _visible_account_control_scope(
        self,
    ) -> tuple[Any | None, WorkdayControlScope]:
        """Return one bounded dialog or unique full-page existing-account form."""
        account_dialog = await self._visible_account_dialog()
        if account_dialog is not None:
            return account_dialog, WorkdayControlScope.ACTIVE_DIALOG
        email_count = await self._visible_count_in(self._page, self._EMAIL_SELECTOR)
        password_count = await self._visible_count_in(
            self._page, self._PASSWORD_SELECTOR
        )
        sign_in_count = await self._visible_role_count_in(
            self._page, ("button",), _LOGIN_ACTION
        )
        create_count = await self._visible_role_count_in(
            self._page, ("button",), _CREATE_ACCOUNT_ACTION
        )
        login_form = email_count == password_count == sign_in_count == 1
        registration_form = (
            email_count == 1 and password_count == 2 and create_count == 1
        )
        if login_form or registration_form:
            return self._page, WorkdayControlScope.ACTIVE_ACCOUNT_FORM
        return None, WorkdayControlScope.PAGE

    async def _visible_trusted_message_texts(self) -> tuple[str, ...]:
        messages = self._page.locator(_TRUSTED_MESSAGE_SELECTOR)
        texts: list[str] = []
        for index in range(min(await messages.count(), 20)):
            candidate = messages.nth(index)
            if await candidate.is_visible():
                texts.append((await candidate.inner_text())[:1000])
        return tuple(texts)

    async def _wait_for_state_or_action(
        self,
        intent: PortalControlIntent,
        action_name: re.Pattern[str],
    ) -> tuple[NativeAccountPageState, Any | None]:
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            state = await self.detect_account_state()
            if state is not NativeAccountPageState.UNKNOWN:
                return state, None
            if self._control_resolver is None:
                apply_action = await self._first_visible_role(
                    ("button", "link"), action_name
                )
                if apply_action is not None or monotonic() >= deadline:
                    return state, apply_action
            else:
                # Workday is a hydrated SPA. Use its known inert readiness marker
                # only to avoid repeatedly invoking the model while controls load;
                # the local LLM still selects the action from the generic candidate set.
                ready_marker = await self._first_visible_role(
                    ("button", "link"), action_name
                )
                if ready_marker is not None or monotonic() >= deadline:
                    return state, await self._resolve_action(
                        intent, ("button", "link"), action_name
                    )
            await self._page.wait_for_timeout(250)

    async def _wait_for_known_apply_readiness(
        self,
    ) -> tuple[NativeAccountPageState, Any | None]:
        """Wait for a known page state or an exact Apply control without the LLM."""
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            state = await self.detect_account_state()
            if state is not NativeAccountPageState.UNKNOWN:
                return state, None
            apply_action = await self._first_visible_role(
                ("button", "link"), _APPLY_ACTION
            )
            if apply_action is not None or monotonic() >= deadline:
                return state, apply_action
            await self._page.wait_for_timeout(250)

    async def _wait_for_account_state(self) -> NativeAccountPageState:
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            state = await self.detect_account_state()
            if state is not NativeAccountPageState.UNKNOWN or monotonic() >= deadline:
                return state
            await self._page.wait_for_timeout(250)

    async def _wait_for_account_state_change(
        self, previous_state: NativeAccountPageState
    ) -> NativeAccountPageState:
        deadline = monotonic() + (self._timeout_ms / 1000)
        while True:
            self._assert_current_workday_url()
            try:
                state = await self.detect_account_state()
            except Exception as exc:
                if not _is_playwright_navigation_context_error(exc):
                    raise
                state = NativeAccountPageState.UNKNOWN
            if state not in {NativeAccountPageState.UNKNOWN, previous_state}:
                return state
            if monotonic() >= deadline:
                raise WorkdayWorkerError(
                    "The Workday account action did not reach a confirmed new state.",
                    safe_code="workday_account_state_transition_timeout",
                )
            await self._page.wait_for_timeout(250)

    async def _visible_count(self, selector: str) -> int:
        return await self._visible_count_in(self._page, selector)

    async def _visible_count_in(self, scope: Any, selector: str) -> int:
        locator = scope.locator(selector)
        count = 0
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                count += 1
        return count

    async def _first_visible_locator(self, selector: str) -> Any | None:
        return await self._first_visible_locator_in(self._page, selector)

    async def _first_visible_locator_in(self, scope: Any, selector: str) -> Any | None:
        locator = scope.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        return None

    async def _resolve_action(
        self,
        intent: PortalControlIntent,
        roles: tuple[str, ...],
        deterministic_name: re.Pattern[str],
        *,
        scope: Any | None = None,
    ) -> Any | None:
        if self._control_resolver is None:
            if scope is None:
                return await self._first_visible_role(roles, deterministic_name)
            return await self._first_visible_role_in(scope, roles, deterministic_name)

        for attempt in range(2):
            if scope is None:
                candidates, locators = await self._visible_action_candidates()
            else:
                candidates, locators = await self._visible_action_candidates(scope)
            selection = await self._control_resolver.select(intent, candidates)
            if selection is not None:
                action = locators.get(selection.candidate_id)
                descriptor = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.candidate_id == selection.candidate_id
                    ),
                    None,
                )
                semantic_name = (
                    descriptor.accessible_name or descriptor.text if descriptor else ""
                )
                semantically_confirmed = (
                    deterministic_name.fullmatch(semantic_name.strip()) is not None
                )
                visible = action is not None and await action.is_visible()
                enabled = visible and await action.is_enabled()
                accepted = bool(
                    visible
                    and enabled
                    and (selection.confidence >= 0.9 or semantically_confirmed)
                )
                self._report_control_decision(
                    {
                        "source": "browser_validator",
                        "intent": intent.value,
                        "attempt": attempt + 1,
                        "candidate_count": len(candidates),
                        "candidate_id": selection.candidate_id,
                        "confidence": selection.confidence,
                        "visible": visible,
                        "enabled": enabled,
                        "semantic_match": semantically_confirmed,
                        "outcome": "accepted" if accepted else "rejected",
                    }
                )
                if accepted:
                    return action
            else:
                self._report_control_decision(
                    {
                        "source": "browser_validator",
                        "intent": intent.value,
                        "attempt": attempt + 1,
                        "candidate_count": len(candidates),
                        "candidate_id": None,
                        "confidence": None,
                        "outcome": "no_selection",
                    }
                )
            if attempt == 0:
                await self._page.wait_for_timeout(250)
        semantic_matches: list[tuple[str, Any]] = []
        for candidate in candidates:
            semantic_name = candidate.accessible_name or candidate.text
            if deterministic_name.fullmatch(semantic_name.strip()) is None:
                continue
            action = locators.get(candidate.candidate_id)
            if (
                action is not None
                and await action.is_visible()
                and await action.is_enabled()
            ):
                semantic_matches.append((candidate.candidate_id, action))
        accepted = len(semantic_matches) == 1
        self._report_control_decision(
            {
                "source": "browser_validator",
                "intent": intent.value,
                "attempt": 2,
                "candidate_count": len(candidates),
                "semantic_match_count": len(semantic_matches),
                "candidate_id": semantic_matches[0][0] if accepted else None,
                "outcome": (
                    "unique_semantic_fallback_accepted"
                    if accepted
                    else "semantic_fallback_rejected"
                ),
            }
        )
        if accepted:
            return semantic_matches[0][1]
        return None

    async def _visible_action_candidates(
        self, scope: Any | None = None
    ) -> tuple[list[PortalControlCandidate], dict[str, Any]]:
        action_scope = self._page if scope is None else scope
        controls = action_scope.locator(self._ACTION_SELECTOR)
        candidates: list[PortalControlCandidate] = []
        locators: dict[str, Any] = {}
        for index in range(min(await controls.count(), 80)):
            control = controls.nth(index)
            if not await control.is_visible() or not await control.is_enabled():
                continue
            details = await control.evaluate(
                """element => ({
                    tag: element.tagName || '',
                    role: element.getAttribute('role') || '',
                    accessibleName: element.getAttribute('aria-label') || '',
                    text: element.innerText || element.value || element.title || '',
                    automationId: element.getAttribute('data-automation-id') || '',
                    inputType: element.getAttribute('type') || '',
                    ariaHidden: element.getAttribute('aria-hidden') || '',
                })"""
            )
            if not isinstance(details, dict) or details.get("ariaHidden") == "true":
                continue
            candidate_id = f"control-{index}"
            candidate = PortalControlCandidate(
                candidate_id=candidate_id,
                tag=str(details.get("tag", "")),
                role=str(details.get("role", "")),
                accessible_name=str(details.get("accessibleName", "")),
                text=str(details.get("text", "")),
                automation_id=str(details.get("automationId", "")),
                input_type=str(details.get("inputType", "")),
            )
            candidates.append(candidate)
            locators[candidate_id] = control
            if len(candidates) >= 40:
                break
        return candidates, locators

    async def _first_visible_role(
        self, roles: tuple[str, ...], name: re.Pattern[str]
    ) -> Any | None:
        return await self._first_visible_role_in(self._page, roles, name)

    async def _first_visible_role_in(
        self, scope: Any, roles: tuple[str, ...], name: re.Pattern[str]
    ) -> Any | None:
        for role in roles:
            locator = scope.get_by_role(role, name=name)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if await candidate.is_visible():
                    return candidate
        return None

    async def _visible_role_count_in(
        self, scope: Any, roles: tuple[str, ...], name: re.Pattern[str]
    ) -> int:
        count = 0
        for role in roles:
            locator = scope.get_by_role(role, name=name)
            for index in range(await locator.count()):
                if await locator.nth(index).is_visible():
                    count += 1
        return count


class WorkdayAccountGateWorker:
    """Run one leased Workday job through a bounded native-account gate."""

    def __init__(
        self,
        browser: WorkdayBrowser,
        coordinator: NativePortalAccountCoordinator,
        state_emitter: WorkdayStateEmitter,
        auth_broker: WorkdayAuthenticationDelegate | None = None,
    ):
        self._browser = browser
        self._coordinator = coordinator
        self._state_emitter = state_emitter
        self._auth_broker = auth_broker

    async def run(
        self,
        *,
        lease: WorkdayLease,
        portal_name: str,
        account_email: str,
    ) -> WorkdayAccountGateOutcome:
        target_url = lease.target_url
        logger.info(
            "workday_account_gate_worker_started application_id=%s",
            lease.application_id,
        )
        await self._browser.open_and_start_apply(target_url)
        page_state = await self._browser.detect_account_state()
        logger.info(
            "workday_account_state_detected application_id=%s page_state=%s",
            lease.application_id,
            page_state.value,
        )
        await self._state_emitter.emit(lease.application_id, page_state)
        if page_state is NativeAccountPageState.JOB_UNAVAILABLE:
            return WorkdayAccountGateOutcome(
                WorkdayAccountGateStatus.SKIPPED,
                page_state,
                derive_workday_portal_scope(target_url),
            )

        for attempt in range(1, 5):
            plan = await self._coordinator.plan_workday_action(
                user_id=lease.user_id,
                job_or_apply_url=target_url,
                portal_name=portal_name,
                account_email=account_email,
                page_state=page_state,
            )
            logger.info(
                "workday_account_action_planned application_id=%s attempt=%s action=%s page_state=%s portal_scope=%s hold_code=%s",
                lease.application_id,
                attempt,
                plan.action.value,
                page_state.value,
                plan.portal_scope,
                plan.hold_code or "none",
            )
            if plan.action is NativeAccountAction.CONTINUE_APPLICATION:
                return WorkdayAccountGateOutcome(
                    WorkdayAccountGateStatus.ACCOUNT_READY,
                    page_state,
                    plan.portal_scope,
                )
            if plan.action is NativeAccountAction.CREATE_HOLD:
                return WorkdayAccountGateOutcome(
                    WorkdayAccountGateStatus.HOLD,
                    page_state,
                    plan.portal_scope,
                    hold_code=plan.hold_code,
                )
            if plan.action is NativeAccountAction.RETRY_LATER:
                return WorkdayAccountGateOutcome(
                    WorkdayAccountGateStatus.RETRY_LATER,
                    page_state,
                    plan.portal_scope,
                )

            if plan.action is NativeAccountAction.ATTEMPT_LOGIN:
                if page_state is NativeAccountPageState.REGISTRATION_REQUIRED:
                    await self._browser.open_login()
                    page_state = await self._browser.detect_account_state()
                    logger.info(
                        "workday_login_state_opened application_id=%s page_state=%s",
                        lease.application_id,
                        page_state.value,
                    )
                    await self._state_emitter.emit(lease.application_id, page_state)
                if page_state is not NativeAccountPageState.LOGIN_REQUIRED:
                    page_state = (
                        page_state
                        if page_state
                        in {
                            NativeAccountPageState.CAPTCHA,
                            NativeAccountPageState.OTP,
                            NativeAccountPageState.TRANSIENT_FAILURE,
                        }
                        else NativeAccountPageState.UNKNOWN
                    )
                    continue
                if self._auth_broker is None or plan.account_ref is None:
                    raise WorkdayWorkerError(
                        "The private Workday authentication broker is unavailable.",
                        safe_code="workday_auth_broker_unavailable",
                    )
                broker_result = await self._auth_broker.authenticate(
                    user_id=lease.user_id,
                    application_id=lease.application_id,
                    account_ref=plan.account_ref,
                    portal_scope=plan.portal_scope,
                )
                outcome = broker_result.route.outcome
                if outcome is WorkdayFailureOutcome.COMPLETE_UNIT:
                    page_state = NativeAccountPageState.LOGIN_COMPLETE
                elif outcome is WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN:
                    page_state = NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED
                elif outcome is WorkdayFailureOutcome.USER_HOLD:
                    page_state = NativeAccountPageState.CAPTCHA
                elif outcome is WorkdayFailureOutcome.CREDENTIAL_HOLD:
                    page_state = NativeAccountPageState.LOGIN_INVALID_CREDENTIALS
                else:
                    page_state = NativeAccountPageState.UNKNOWN
                logger.info(
                    "workday_login_state_detected application_id=%s page_state=%s",
                    lease.application_id,
                    page_state.value,
                )
                await self._state_emitter.emit(lease.application_id, page_state)
                continue

            credential = plan.credential
            if credential is None:
                raise PortalCredentialError(
                    "The account action has no worker credential."
                )
            if page_state is not NativeAccountPageState.REGISTRATION_REQUIRED:
                await self._browser.open_registration()
                page_state = await self._browser.detect_account_state()
                logger.info(
                    "workday_registration_state_detected application_id=%s page_state=%s",
                    lease.application_id,
                    page_state.value,
                )
                await self._state_emitter.emit(lease.application_id, page_state)
            if page_state is not NativeAccountPageState.REGISTRATION_REQUIRED:
                page_state = (
                    page_state
                    if page_state
                    in {
                        NativeAccountPageState.CAPTCHA,
                        NativeAccountPageState.OTP,
                        NativeAccountPageState.TRANSIENT_FAILURE,
                    }
                    else NativeAccountPageState.UNKNOWN
                )
                continue
            await self._browser.fill_registration(credential)
            await self._browser.submit_registration()
            page_state = await self._browser.detect_account_state()
            if page_state is NativeAccountPageState.AUTHENTICATED:
                page_state = NativeAccountPageState.REGISTRATION_COMPLETE
            logger.info(
                "workday_registration_result_detected application_id=%s page_state=%s",
                lease.application_id,
                page_state.value,
            )
            await self._state_emitter.emit(lease.application_id, page_state)

        final_plan = await self._coordinator.plan_workday_action(
            user_id=lease.user_id,
            job_or_apply_url=target_url,
            portal_name=portal_name,
            account_email=account_email,
            page_state=NativeAccountPageState.UNKNOWN,
        )
        await self._state_emitter.emit(
            lease.application_id, NativeAccountPageState.UNKNOWN
        )
        return WorkdayAccountGateOutcome(
            WorkdayAccountGateStatus.HOLD,
            NativeAccountPageState.UNKNOWN,
            final_plan.portal_scope,
            hold_code="unknown_page_state",
        )
