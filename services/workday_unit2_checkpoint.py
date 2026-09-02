"""Private evidence boundary and session resume verification for Workday Unit 2."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import hashlib
import logging
import re
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID

from services.portal_account_automation import derive_workday_portal_scope
from services.portal_credentials import normalize_portal_scope
from services.workday_playwright_worker import (
    _ACCOUNT_TEMPORARILY_LOCKED,
    _INFORMATION_SECTION_HEADING,
    _LOGIN_ACTION,
    _WORKDAY_HOST,
    WorkdayWorkerError,
)
from services.workday_worker_api import LeasedWorkdayUnit2Application

logger = logging.getLogger(__name__)

_CAPTCHA_PATTERN = re.compile(r"captcha|recaptcha|hcaptcha|arkoselabs", re.IGNORECASE)
_OTP_PATTERN = re.compile(
    r"\bone[- ]?time\s+pass(?:code|word)|verification\s+code\b|\botp\b", re.IGNORECASE
)
_EMAIL_VERIFICATION_PATTERN = re.compile(
    r"verify\s+your\s+email|email\s+verification|check\s+your\s+inbox", re.IGNORECASE
)
_CONTINUE_OR_APPLY_PATTERN = re.compile(
    r"^\s*(?:continue\s+application|apply(?:\s+now)?|apply\s+manually)\s*$",
    re.IGNORECASE,
)
_APPLICATION_ID_QUERY_KEYS = frozenset(
    {"applicationid", "appid", "application_id", "draftid", "candidateid"}
)


@dataclass(frozen=True, slots=True)
class Unit2ResumeProof:
    """Private, non-serializable proof of an authenticated My Information draft."""

    application_id: UUID
    attempt_id: UUID
    lease_id: UUID
    approved_origin: bool
    canonical_tenant_verified: bool
    job_context_matches: bool
    application_context_matches: bool
    durable_account_continuity_verified: bool
    authenticated_state_verified: bool
    my_information_structure_verified: bool
    safe_signature: str

    def __post_init__(self) -> None:
        if not self.safe_signature:
            raise ValueError("A safe resume proof signature is required.")

    @property
    def is_valid(self) -> bool:
        return (
            self.application_id.int != 0
            and self.attempt_id.int != 0
            and self.lease_id.int != 0
            and self.approved_origin
            and self.canonical_tenant_verified
            and self.job_context_matches
            and self.application_context_matches
            and self.durable_account_continuity_verified
            and self.authenticated_state_verified
            and self.my_information_structure_verified
            and bool(self.safe_signature)
        )


@dataclass(frozen=True, slots=True)
class Unit2Observation:
    """Safe structural observation of a page without private values."""

    approved_origin: bool
    canonical_tenant: bool
    job_context_matches: bool
    application_context_matches: bool
    is_authenticated: bool
    has_my_info_heading: bool
    has_form_controls: bool
    is_expired_or_login: bool
    has_captcha: bool
    has_otp: bool
    has_email_verification: bool
    is_locked: bool
    has_validation_errors: bool
    safe_signature: str


def compute_safe_structural_signature(
    *,
    url_path: str,
    heading_name: str,
    field_count: int,
    button_count: int,
    route_identity_digest: str = "",
) -> str:
    """Compute a value-free structural signature."""
    raw = (
        f"path:{url_path}|heading:{heading_name.lower().strip()}|fields:{field_count}"
        f"|buttons:{button_count}|route:{route_identity_digest}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def canonical_workday_route_identity(url: str) -> str:
    """Return a safe digest of the application route and external draft identifiers."""
    parsed = urlsplit(url)
    path = "/".join(
        unquote(part).strip().casefold()
        for part in parsed.path.split("/")
        if part.strip()
    )
    query_parts = sorted(
        (key.casefold(), value.strip().casefold())
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() in _APPLICATION_ID_QUERY_KEYS
    )
    raw = f"path:{path}|ids:{query_parts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def bind_safe_structural_signature(
    safe_signature: str,
    *,
    application_id: UUID,
    attempt_id: UUID,
    lease_id: UUID,
) -> str:
    """Bind value-free page evidence to exactly one internal execution authority."""
    if not safe_signature or 0 in (application_id.int, attempt_id.int, lease_id.int):
        raise ValueError(
            "A structural signature and nonzero execution IDs are required."
        )
    raw = (
        f"signature:{safe_signature}|application:{application_id}"
        f"|attempt:{attempt_id}|lease:{lease_id}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def workday_application_route_matches(
    *, current_url: str, target_url: str, application_id: UUID
) -> bool:
    """Match the live application route to the leased job and bind a nonzero app ID."""
    if not isinstance(application_id, UUID) or application_id.int == 0:
        return False
    current = urlsplit(current_url)
    target = urlsplit(target_url)
    current_segments = [
        unquote(part).strip().casefold()
        for part in current.path.split("/")
        if part.strip()
    ]
    job_slug = extract_workday_job_slug(target_url).casefold()
    if not job_slug or job_slug not in current_segments:
        return False
    if not any(part in {"apply", "application"} for part in current_segments):
        return False

    def _external_ids(url: str) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {}
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            normalized_key = key.casefold()
            if normalized_key in _APPLICATION_ID_QUERY_KEYS:
                values.setdefault(normalized_key, []).append(value.strip().casefold())
        return {key: tuple(items) for key, items in values.items()}

    target_ids = _external_ids(target_url)
    current_ids = _external_ids(current_url)
    return not target_ids or target_ids == current_ids


def extract_workday_job_slug(target_url: str) -> str:
    """Extract non-generic job identifier/slug from a Workday URL."""
    parsed = urlsplit(target_url)
    parts = [unquote(p).strip() for p in parsed.path.split("/") if p.strip()]
    generic = {
        "apply",
        "job",
        "en-us",
        "careers",
        "recruiting",
        "jobs",
        "myworkdayjobs",
    }
    for part in reversed(parts):
        if part.lower() not in generic:
            return part
    return parts[-1] if parts else ""


class WorkdayUnit2PageAdapter(Protocol):
    """Protocol for abstracting Playwright page operations during resume."""

    async def current_url(self) -> str: ...

    async def observe_structure(
        self,
        *,
        expected_origin: str,
        expected_tenant: str,
        expected_job_path_token: str,
        expected_target_url: str,
        application_id: UUID,
    ) -> Unit2Observation: ...

    async def click_continue_or_apply(self) -> bool: ...

    async def navigate_to_job(self, url: str) -> None: ...


class PlaywrightWorkdayUnit2PageAdapter:
    """Playwright implementation of WorkdayUnit2PageAdapter."""

    def __init__(self, page: Any, *, timeout_ms: int = 10_000) -> None:
        self._page = page
        self._timeout_ms = timeout_ms

    async def current_url(self) -> str:
        return self._page.url

    async def navigate_to_job(self, url: str) -> None:
        await self._page.goto(
            url, timeout=self._timeout_ms, wait_until="domcontentloaded"
        )

    async def click_continue_or_apply(self) -> bool:
        # Strictly query button or [role='button'] elements (rejecting <a> links)
        candidates = await self._page.query_selector_all("button, [role='button']")
        matching: list[Any] = []
        for btn in candidates:
            try:
                tag = await btn.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    continue
                if not await btn.is_visible():
                    continue
                disabled = await btn.evaluate(
                    "el => Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')"
                )
                if disabled:
                    continue
                text = str(
                    await btn.evaluate(
                        r"""el => {
                            const aria = (el.getAttribute('aria-label') || '').trim();
                            if (aria) return aria;
                            const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
                            const labelled = ids.map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                            return labelled || String(el.innerText || el.value || '').trim();
                        }"""
                    )
                ).strip()
                if _CONTINUE_OR_APPLY_PATTERN.match(text):
                    matching.append(btn)
            except Exception:
                continue

        # Exactly one visible, enabled control with exact allowlisted accessible name
        if len(matching) != 1:
            return False

        target_btn = matching[0]
        try:
            await target_btn.click(timeout=self._timeout_ms)
            await self._page.wait_for_load_state(
                "domcontentloaded", timeout=self._timeout_ms
            )
            return True
        except Exception:
            # Never click a second candidate after any click attempt
            return False

    async def observe_structure(
        self,
        *,
        expected_origin: str,
        expected_tenant: str,
        expected_job_path_token: str,
        expected_target_url: str,
        application_id: UUID,
    ) -> Unit2Observation:
        current_url = self._page.url
        parsed = urlsplit(current_url)
        current_origin = f"{parsed.scheme}://{parsed.netloc}".lower()

        # Origin verification: must be HTTPS, recognized host, and match expected origin exactly
        approved_origin = (
            parsed.scheme == "https"
            and bool(_WORKDAY_HOST.search(parsed.hostname or ""))
            and current_origin == expected_origin.lower()
        )

        # Tenant verification
        canonical_tenant = False
        try:
            scope = derive_workday_portal_scope(current_url)
            canonical_tenant = normalize_portal_scope(scope) == normalize_portal_scope(
                expected_tenant
            )
        except Exception:
            canonical_tenant = False

        # Job context: require non-generic token from expected_job_path_token
        current_segments = {
            unquote(part).strip().casefold()
            for part in parsed.path.split("/")
            if part.strip()
        }
        job_context_matches = (
            bool(expected_job_path_token)
            and expected_job_path_token.casefold() in current_segments
        )
        application_context_matches = workday_application_route_matches(
            current_url=current_url,
            target_url=expected_target_url,
            application_id=application_id,
        )

        # Check page text for challenges / login / locks
        page_text = ""
        inspection_failed = False
        try:
            body_elem = await self._page.query_selector("body")
            if body_elem:
                page_text = (await body_elem.inner_text())[:4000]
        except Exception:
            inspection_failed = True

        has_captcha = bool(_CAPTCHA_PATTERN.search(page_text))
        has_otp = bool(_OTP_PATTERN.search(page_text))
        has_email_verification = bool(_EMAIL_VERIFICATION_PATTERN.search(page_text))
        is_locked = bool(_ACCOUNT_TEMPORARILY_LOCKED.search(page_text))

        # Check login or sign-in prompts
        is_expired_or_login = False
        try:
            sign_in_elem = await self._page.query_selector(
                "input[type='password'], [data-automation-id='signInSubmitButton']"
            )
            if sign_in_elem and await sign_in_elem.is_visible():
                is_expired_or_login = True
        except Exception:
            inspection_failed = True

        # Check My Information heading
        has_my_info_heading = False
        heading_name = ""
        try:
            headings = await self._page.query_selector_all(
                "h1, h2, h3, [data-automation-id*='heading' i], [data-automation-id*='pageHeader' i]"
            )
            for h in headings:
                if not await h.is_visible():
                    continue
                txt = (await h.inner_text()).strip()
                if _INFORMATION_SECTION_HEADING.match(txt):
                    has_my_info_heading = True
                    heading_name = txt
                    break
        except Exception:
            inspection_failed = True

        # Count inputs and buttons
        input_count = 0
        button_count = 0
        try:
            inputs = await self._page.query_selector_all(
                "input:not([type='hidden']), select, textarea"
            )
            for item in inputs:
                if await item.is_visible():
                    input_count += 1
            buttons = await self._page.query_selector_all("button, [role='button']")
            for item in buttons:
                if await item.is_visible():
                    button_count += 1
        except Exception:
            inspection_failed = True

        has_validation_errors = False
        try:
            validation_nodes = await self._page.query_selector_all(
                "[aria-invalid='true'], [data-automation-id*='error' i], "
                "[role='alert'], [data-automation-id='formFeedback']"
            )
            for item in validation_nodes:
                if not await item.is_visible():
                    continue
                aria_invalid = await item.get_attribute("aria-invalid")
                text = (await item.inner_text()).strip()
                if aria_invalid == "true" or text:
                    has_validation_errors = True
                    break
        except Exception:
            inspection_failed = True

        if self._page.url != current_url:
            inspection_failed = True

        has_form_controls = input_count >= 1
        is_authenticated = bool(
            not inspection_failed
            and not is_expired_or_login
            and not has_captcha
            and not has_otp
            and not has_email_verification
            and not is_locked
            and not has_validation_errors
            and has_my_info_heading
        )

        signature = compute_safe_structural_signature(
            url_path=parsed.path,
            heading_name=heading_name or "unknown",
            field_count=input_count,
            button_count=button_count,
            route_identity_digest=canonical_workday_route_identity(current_url),
        )

        return Unit2Observation(
            approved_origin=approved_origin,
            canonical_tenant=canonical_tenant,
            job_context_matches=job_context_matches,
            application_context_matches=application_context_matches,
            is_authenticated=is_authenticated,
            has_my_info_heading=has_my_info_heading,
            has_form_controls=has_form_controls,
            is_expired_or_login=is_expired_or_login,
            has_captcha=has_captcha,
            has_otp=has_otp,
            has_email_verification=has_email_verification,
            is_locked=is_locked,
            has_validation_errors=has_validation_errors,
            safe_signature=signature,
        )


class WorkdayUnit2ResumeCoordinator:
    """Coordinate deterministic resume and dual private observation of the draft."""

    def __init__(self, *, observation_delay_seconds: float = 0.15) -> None:
        self._delay_seconds = observation_delay_seconds

    async def resume_and_verify(
        self,
        *,
        lease: LeasedWorkdayUnit2Application,
        adapter: WorkdayUnit2PageAdapter,
        durable_account_continuity_verified: bool,
    ) -> tuple[Unit2ResumeProof | None, str]:
        """Verify the My Information draft with two matching observations.

        Returns: (Unit2ResumeProof, "ready") on success, or (None, outcome_code) on stop.
        """
        # Determine expected tenant and job path token
        target_url = lease.external_ats_url or lease.job_url
        try:
            expected_tenant = derive_workday_portal_scope(target_url)
        except Exception:
            expected_tenant = lease.portal

        parsed_target = urlsplit(target_url)
        job_token = extract_workday_job_slug(target_url)

        # First observation
        obs1 = await adapter.observe_structure(
            expected_origin=f"{parsed_target.scheme}://{parsed_target.netloc}",
            expected_tenant=expected_tenant,
            expected_job_path_token=job_token,
            expected_target_url=target_url,
            application_id=lease.application_id,
        )

        # If not currently on My Information draft, attempt single deterministic navigation
        if not (obs1.has_my_info_heading and obs1.has_form_controls):
            if obs1.is_expired_or_login:
                return None, "expired_session"
            if obs1.has_captcha:
                return None, "captcha"
            if obs1.has_otp:
                return None, "otp"
            if obs1.has_email_verification:
                return None, "email_verification"
            if obs1.is_locked:
                return None, "account_temporarily_locked"

            # Navigate to job URL if not already on it
            current_url = await adapter.current_url()
            if job_token.lower() not in current_url.lower():
                await adapter.navigate_to_job(target_url)

            # Try clicking Continue Application or Apply (at most one candidate)
            clicked = await adapter.click_continue_or_apply()
            if not clicked:
                # Re-observe after navigation attempt
                pass

            obs1 = await adapter.observe_structure(
                expected_origin=f"{parsed_target.scheme}://{parsed_target.netloc}",
                expected_tenant=expected_tenant,
                expected_job_path_token=job_token,
                expected_target_url=target_url,
                application_id=lease.application_id,
            )

        # Check outcomes on obs1
        if obs1.is_expired_or_login:
            return None, "expired_session"
        if obs1.has_captcha:
            return None, "captcha"
        if obs1.has_otp:
            return None, "otp"
        if obs1.has_email_verification:
            return None, "email_verification"
        if obs1.is_locked:
            return None, "account_temporarily_locked"

        if not (
            obs1.approved_origin
            and obs1.canonical_tenant
            and obs1.job_context_matches
            and obs1.application_context_matches
            and obs1.is_authenticated
            and obs1.has_my_info_heading
            and obs1.has_form_controls
            and not obs1.has_validation_errors
        ):
            return None, "unknown_page_state"

        # Bounded pause before second observation
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)

        # Second observation
        obs2 = await adapter.observe_structure(
            expected_origin=f"{parsed_target.scheme}://{parsed_target.netloc}",
            expected_tenant=expected_tenant,
            expected_job_path_token=job_token,
            expected_target_url=target_url,
            application_id=lease.application_id,
        )

        # Verify stability and matching signatures
        if (
            obs2.safe_signature != obs1.safe_signature
            or not obs2.approved_origin
            or not obs2.canonical_tenant
            or not obs2.job_context_matches
            or not obs2.application_context_matches
            or not obs2.is_authenticated
            or not obs2.has_my_info_heading
            or not obs2.has_form_controls
            or obs2.is_expired_or_login
            or obs2.has_captcha
            or obs2.has_otp
            or obs2.has_email_verification
            or obs2.is_locked
            or obs2.has_validation_errors
        ):
            return None, "unknown_page_state"

        if not durable_account_continuity_verified:
            return None, "unknown_page_state"

        # Create verified private proof
        proof = Unit2ResumeProof(
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            approved_origin=obs1.approved_origin and obs2.approved_origin,
            canonical_tenant_verified=obs1.canonical_tenant and obs2.canonical_tenant,
            job_context_matches=obs1.job_context_matches and obs2.job_context_matches,
            application_context_matches=obs1.application_context_matches
            and obs2.application_context_matches,
            durable_account_continuity_verified=durable_account_continuity_verified,
            authenticated_state_verified=obs1.is_authenticated
            and obs2.is_authenticated,
            my_information_structure_verified=obs1.has_my_info_heading
            and obs2.has_my_info_heading
            and obs1.has_form_controls
            and obs2.has_form_controls,
            safe_signature=bind_safe_structural_signature(
                obs1.safe_signature,
                application_id=lease.application_id,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
            ),
        )

        if not proof.is_valid:
            return None, "unknown_page_state"

        return proof, "ready"


class WorkdayNextSectionClass(str, Enum):
    """Closed allowlist of non-final Workday application sections following My Information."""

    MY_EXPERIENCE = "my_experience"
    APPLICATION_QUESTIONS = "application_questions"
    VOLUNTARY_DISCLOSURES = "voluntary_disclosures"
    SELF_IDENTIFICATION = "self_identification"


@dataclass(frozen=True, slots=True)
class Unit2CheckpointProof:
    """Private, non-serializable proof of reaching an allowlisted next section after Save."""

    application_id: UUID
    attempt_id: UUID
    lease_id: UUID
    checkpoint_version: str
    section_class: WorkdayNextSectionClass
    safe_signature: str

    def __post_init__(self) -> None:
        if 0 in (self.application_id.int, self.attempt_id.int, self.lease_id.int):
            raise ValueError("Checkpoint execution IDs must be nonzero.")
        if self.checkpoint_version != "workday_unit2_v1":
            raise ValueError("Invalid checkpoint version.")
        if not self.safe_signature:
            raise ValueError("A checkpoint proof safe signature is required.")

    @property
    def is_valid(self) -> bool:
        return bool(
            self.application_id
            and self.attempt_id
            and self.lease_id
            and self.checkpoint_version == "workday_unit2_v1"
            and isinstance(self.section_class, WorkdayNextSectionClass)
            and self.safe_signature
        )


_SAVE_AND_CONTINUE_PATTERN = re.compile(
    r"^\s*save\s+(?:and|&)\s+continue\s*$", re.IGNORECASE
)

_NEXT_SECTION_PATTERNS: tuple[tuple[WorkdayNextSectionClass, re.Pattern[str]], ...] = (
    (
        WorkdayNextSectionClass.MY_EXPERIENCE,
        re.compile(r"^\s*(?:my\s+)?experience\s*$", re.IGNORECASE),
    ),
    (
        WorkdayNextSectionClass.APPLICATION_QUESTIONS,
        re.compile(r"^\s*application\s+questions\s*$", re.IGNORECASE),
    ),
    (
        WorkdayNextSectionClass.VOLUNTARY_DISCLOSURES,
        re.compile(r"^\s*(?:voluntary\s+)?disclosures\s*$", re.IGNORECASE),
    ),
    (
        WorkdayNextSectionClass.SELF_IDENTIFICATION,
        re.compile(
            r"^\s*(?:self[- ]?identification|equal\s+opportunity)\s*$", re.IGNORECASE
        ),
    ),
)

_DISALLOWED_FINAL_OR_REVIEW_PATTERN = re.compile(
    r"^\s*(?:review(?:\s+and\s+submit)?|submit(?:\s+application)?|confirmation|application\s+completed?|submitted)\s*$",
    re.IGNORECASE,
)


def classify_next_section_heading(heading_text: str) -> WorkdayNextSectionClass | None:
    """Match heading text against the closed allowlist of valid next sections."""
    clean = heading_text.strip()
    if not clean:
        return None
    if _DISALLOWED_FINAL_OR_REVIEW_PATTERN.match(clean):
        return None
    for section_class, pattern in _NEXT_SECTION_PATTERNS:
        if pattern.match(clean):
            return section_class
    return None
