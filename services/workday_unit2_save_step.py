"""Deterministic Save-and-Continue resolution, single atomic claim, and next section proof."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from services.workday_form_step_policy import Unit2PreparedForm
from services.portal_account_automation import derive_workday_portal_scope
from services.portal_credentials import normalize_portal_scope
from services.workday_playwright_worker import (
    _ACCOUNT_TEMPORARILY_LOCKED,
    _INFORMATION_SECTION_HEADING,
    _WORKDAY_HOST,
    WorkdayWorkerError,
)
from services.workday_unit2_checkpoint import (
    Unit2CheckpointProof,
    WorkdayNextSectionClass,
    bind_safe_structural_signature,
    canonical_workday_route_identity,
    classify_next_section_heading,
    compute_safe_structural_signature,
    workday_application_route_matches,
)
from services.workday_worker_api import LeasedWorkdayUnit2Application, WorkdayWorkerApi

logger = logging.getLogger(__name__)

_UNIT2_FORM_CONTROL_SELECTOR = (
    "input:not([type='hidden']):not([type='password']):not([type='submit']):"
    "not([type='button']), textarea, select, [role='combobox']"
)
_SAVE_AND_CONTINUE_EXACT = re.compile(
    r"^\s*save\s+(?:and|&)\s+continue\s*$", re.IGNORECASE
)
_CAPTCHA_PATTERN = re.compile(r"captcha|recaptcha|hcaptcha|arkoselabs", re.IGNORECASE)
_OTP_PATTERN = re.compile(
    r"\bone[- ]?time\s+pass(?:code|word)|verification\s+code\b|\botp\b", re.IGNORECASE
)
_EMAIL_VERIFICATION_PATTERN = re.compile(
    r"verify\s+your\s+email|email\s+verification|check\s+your\s+inbox",
    re.IGNORECASE,
)


async def _count_visible(elements: list[Any]) -> int:
    count = 0
    for element in elements:
        if await element.is_visible():
            count += 1
    return count


@dataclass(frozen=True, slots=True)
class Unit2NextSectionObservation:
    """One value-free, fail-closed observation of an allowlisted next section."""

    current_url: str
    heading_name: str | None
    section_class: WorkdayNextSectionClass | None
    safe_signature: str
    visible_field_count: int
    visible_button_count: int
    is_hydrated: bool
    is_authenticated: bool
    has_validation_errors: bool
    issue_code: str | None

    @property
    def is_valid(self) -> bool:
        return bool(
            self.heading_name
            and self.section_class is not None
            and self.safe_signature
            and self.visible_field_count >= 1
            and self.is_hydrated
            and self.is_authenticated
            and not self.has_validation_errors
            and self.issue_code is None
        )


def verify_page_continuity(
    *,
    current_url: str,
    target_url: str,
    application_id: UUID,
    page_evidence: Any | None = None,
) -> bool:
    """Prove HTTPS origin, canonical tenant, job context, and active application draft continuity bound to application_id."""
    try:
        if not isinstance(application_id, UUID):
            return False

        parsed_current = urlsplit(current_url)
        parsed_target = urlsplit(target_url)

        # 1. Origin match: must be HTTPS, recognized host, and match expected origin exactly
        if parsed_current.scheme != "https" or not bool(
            _WORKDAY_HOST.search(parsed_current.hostname or "")
        ):
            return False
        current_origin = f"{parsed_current.scheme}://{parsed_current.netloc}".lower()
        target_origin = f"{parsed_target.scheme}://{parsed_target.netloc}".lower()
        if current_origin != target_origin:
            return False

        # 2. Canonical tenant match
        scope_current = derive_workday_portal_scope(current_url)
        scope_target = derive_workday_portal_scope(target_url)
        if normalize_portal_scope(scope_current) != normalize_portal_scope(
            scope_target
        ):
            return False

        # 3. Exact leased job/application route and external draft identifiers.
        if not workday_application_route_matches(
            current_url=current_url,
            target_url=target_url,
            application_id=application_id,
        ):
            return False

        # 4. Accept only typed evidence bound to the same application.
        if isinstance(page_evidence, Unit2PreparedForm):
            return bool(
                page_evidence.application_id == application_id
                and bool(page_evidence.prepared_safe_signature)
                and page_evidence.verified_count >= page_evidence.required_count
            )
        elif isinstance(page_evidence, Unit2CheckpointProof):
            return bool(
                page_evidence.application_id == application_id
                and page_evidence.checkpoint_version == "workday_unit2_v1"
                and page_evidence.is_valid
            )
        elif isinstance(page_evidence, Unit2NextSectionObservation):
            return bool(
                page_evidence.current_url == current_url
                and page_evidence.is_valid
                and classify_next_section_heading(page_evidence.heading_name or "")
                == page_evidence.section_class
            )
        return False
    except Exception:
        return False


class WorkdaySaveButtonAdapter(Protocol):
    """Protocol for interacting with the page during Save and Continue."""

    async def current_url(self) -> str: ...
    async def get_page_safe_signature(self) -> str: ...
    async def resolve_unique_save_button(self) -> Any | None: ...
    async def click_save_button_once(self, button: Any) -> None: ...
    async def observe_next_section(self) -> Unit2NextSectionObservation: ...


class PlaywrightSaveButtonAdapter:
    """Playwright implementation for Save button resolution and next-section observation."""

    def __init__(self, page: Any, *, timeout_ms: int = 10_000) -> None:
        self._page = page
        self._timeout_ms = timeout_ms

    async def current_url(self) -> str:
        return self._page.url

    async def get_page_safe_signature(self) -> str:
        current_url = self._page.url
        url_path = urlsplit(current_url).path
        headings = await self._page.query_selector_all(
            "h1, h2, h3, [data-automation-id*='heading' i], "
            "[data-automation-id*='pageHeader' i]"
        )
        heading_name: str | None = None
        for heading in headings:
            if not await heading.is_visible():
                continue
            candidate = (await heading.inner_text()).strip()
            if _INFORMATION_SECTION_HEADING.match(candidate):
                heading_name = candidate
                break
        if heading_name is None:
            raise WorkdayWorkerError(
                "The My Information structure is no longer active."
            )
        fields = await self._page.query_selector_all(_UNIT2_FORM_CONTROL_SELECTOR)
        buttons = await self._page.query_selector_all("button, [role='button']")
        if self._page.url != current_url:
            raise WorkdayWorkerError(
                "The My Information page changed during verification."
            )
        return compute_safe_structural_signature(
            url_path=url_path,
            heading_name=heading_name,
            field_count=await _count_visible(fields),
            button_count=await _count_visible(buttons),
            route_identity_digest=canonical_workday_route_identity(current_url),
        )

    async def resolve_unique_save_button(self) -> Any | None:
        """Find exactly one visible, enabled button with exact name 'Save and Continue'."""
        # Query strictly button or [role='button'] elements, rejecting <a> links
        candidates = await self._page.query_selector_all("button, [role='button']")
        matching_buttons: list[Any] = []

        for candidate in candidates:
            try:
                tag = await candidate.evaluate("el => el.tagName.toLowerCase()")
                if tag == "a":
                    continue  # Disallow links

                if not await candidate.is_visible():
                    continue

                disabled = await candidate.evaluate(
                    "el => Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true')"
                )
                if disabled:
                    continue

                text = str(
                    await candidate.evaluate(
                        r"""el => {
                            const aria = (el.getAttribute('aria-label') || '').trim();
                            if (aria) return aria;
                            const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
                            const labelled = ids.map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
                            return labelled || String(el.innerText || el.value || '').trim();
                        }"""
                    )
                ).strip()

                if _SAVE_AND_CONTINUE_EXACT.match(text):
                    matching_buttons.append(candidate)
            except Exception:
                continue

        if len(matching_buttons) == 1:
            return matching_buttons[0]
        return None

    async def click_save_button_once(self, button: Any) -> None:
        """Click the Save button once without retry."""
        try:
            await button.click(timeout=self._timeout_ms)
            await self._page.wait_for_load_state(
                "domcontentloaded", timeout=self._timeout_ms
            )
        except Exception as exc:
            logger.warning(
                "save_and_continue_click_completed_with_notice exc=%s",
                type(exc).__name__,
            )

    async def observe_next_section(self) -> Unit2NextSectionObservation:
        """Observe one hydrated next section and fail closed on auth/validation state."""
        initial_url = self._page.url
        try:
            body_elem = await self._page.query_selector("body")
            page_text = (await body_elem.inner_text())[:4000] if body_elem else ""

            issue_code: str | None = None
            if _CAPTCHA_PATTERN.search(page_text):
                issue_code = "captcha"
            elif _OTP_PATTERN.search(page_text):
                issue_code = "otp"
            elif _EMAIL_VERIFICATION_PATTERN.search(page_text):
                issue_code = "email_verification"
            elif _ACCOUNT_TEMPORARILY_LOCKED.search(page_text):
                issue_code = "account_temporarily_locked"

            login_controls = await self._page.query_selector_all(
                "input[type='password'], [data-automation-id='signInSubmitButton']"
            )
            if issue_code is None:
                for control in login_controls:
                    if await control.is_visible():
                        issue_code = "expired_session"
                        break

            has_validation_errors = False
            validation_nodes = await self._page.query_selector_all(
                "[aria-invalid='true'], [data-automation-id*='error' i], "
                "[role='alert'], [data-automation-id='formFeedback']"
            )
            for node in validation_nodes:
                if not await node.is_visible():
                    continue
                aria_invalid = await node.get_attribute("aria-invalid")
                text = (await node.inner_text()).strip()
                if aria_invalid == "true" or text:
                    has_validation_errors = True
                    if issue_code is None:
                        issue_code = "validation_failure"
                    break

            heading_text: str | None = None
            section_class: WorkdayNextSectionClass | None = None
            first_visible_heading: str | None = None
            allowlisted_headings: list[tuple[str, WorkdayNextSectionClass]] = []
            headings = await self._page.query_selector_all(
                "h1, h2, h3, [data-automation-id*='heading' i], "
                "[data-automation-id*='pageHeader' i]"
            )
            for heading in headings:
                if not await heading.is_visible():
                    continue
                text = (await heading.inner_text()).strip()
                if not text:
                    continue
                first_visible_heading = first_visible_heading or text
                classified = classify_next_section_heading(text)
                if classified is not None:
                    allowlisted_headings.append((text, classified))
            if len(allowlisted_headings) == 1:
                heading_text, section_class = allowlisted_headings[0]
            else:
                heading_text = first_visible_heading
                if issue_code is None:
                    issue_code = "unknown_page_state"

            fields = await self._page.query_selector_all(_UNIT2_FORM_CONTROL_SELECTOR)
            visible_fields = await _count_visible(fields)
            buttons = await self._page.query_selector_all("button, [role='button']")
            visible_buttons = await _count_visible(buttons)
            final_url = self._page.url
            if final_url != initial_url and issue_code is None:
                issue_code = "unknown_page_state"

            sig = compute_safe_structural_signature(
                url_path=urlsplit(final_url).path,
                heading_name=heading_text or "unknown",
                field_count=visible_fields,
                button_count=visible_buttons,
                route_identity_digest=canonical_workday_route_identity(final_url),
            )
            is_authenticated = issue_code not in {
                "captcha",
                "otp",
                "email_verification",
                "account_temporarily_locked",
                "expired_session",
            }
            is_hydrated = bool(
                final_url == initial_url
                and heading_text
                and section_class is not None
                and visible_fields >= 1
            )
            return Unit2NextSectionObservation(
                current_url=final_url,
                heading_name=heading_text,
                section_class=section_class,
                safe_signature=sig,
                visible_field_count=visible_fields,
                visible_button_count=visible_buttons,
                is_hydrated=is_hydrated,
                is_authenticated=is_authenticated,
                has_validation_errors=has_validation_errors,
                issue_code=issue_code,
            )
        except Exception as exc:
            logger.warning("next_section_observation_failed exc=%s", type(exc).__name__)
            return Unit2NextSectionObservation(
                current_url=self._page.url,
                heading_name=None,
                section_class=None,
                safe_signature="observation_failed",
                visible_field_count=0,
                visible_button_count=0,
                is_hydrated=False,
                is_authenticated=False,
                has_validation_errors=False,
                issue_code="unknown_page_state",
            )


class WorkdayUnit2SaveStepCoordinator:
    """Coordinate pre-click verification, atomic Save claim (0 -> 1), single click, and next-section proof."""

    def __init__(
        self,
        *,
        worker_api: WorkdayWorkerApi,
        observation_delay_seconds: float = 0.15,
    ) -> None:
        self._api = worker_api
        self._delay_seconds = observation_delay_seconds

    async def execute_save_and_verify_checkpoint(
        self,
        *,
        lease: LeasedWorkdayUnit2Application,
        prepared_form: Unit2PreparedForm,
        adapter: WorkdaySaveButtonAdapter,
        account_continuity_check: Callable[[], bool],
    ) -> tuple[Unit2CheckpointProof | None, str]:
        """Execute the atomic Save step. Returns (Unit2CheckpointProof, 'ready') or (None, outcome)."""
        # 1. Reverification of authority and prepared form invariants
        if (
            prepared_form.application_id != lease.application_id
            or prepared_form.attempt_id != lease.attempt_id
            or prepared_form.lease_id != lease.lease_id
        ):
            return None, "prepared_form_mismatch"
        try:
            if not account_continuity_check():
                return None, "unknown_page_state"
        except Exception:
            return None, "unknown_page_state"

        # 2. Check for page drift and continuity before touching Save
        target_url = lease.external_ats_url or lease.job_url
        try:
            current_url = await adapter.current_url()
        except Exception as exc:
            logger.warning("save_step_current_url_failed exc=%s", type(exc).__name__)
            return None, "page_drift"
        if not verify_page_continuity(
            current_url=current_url,
            target_url=target_url,
            application_id=lease.application_id,
            page_evidence=prepared_form,
        ):
            logger.warning("save_step_continuity_mismatch_before_save")
            return None, "page_drift"

        try:
            current_sig = bind_safe_structural_signature(
                await adapter.get_page_safe_signature(),
                application_id=lease.application_id,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
            )
        except Exception as exc:
            logger.warning(
                "save_step_signature_observation_failed exc=%s", type(exc).__name__
            )
            return None, "page_drift"
        if current_sig != prepared_form.prepared_safe_signature:
            logger.warning(
                "save_step_page_drift_detected current=%s prepared=%s",
                current_sig,
                prepared_form.prepared_safe_signature,
            )
            return None, "page_drift"

        # 3. Resolve unique Save and Continue button (must be exactly 1, visible, enabled button)
        try:
            save_button = await adapter.resolve_unique_save_button()
        except Exception as exc:
            logger.warning("save_button_resolution_failed exc=%s", type(exc).__name__)
            return None, "save_button_unresolved"
        if save_button is None:
            return None, "save_button_unresolved"

        try:
            if not account_continuity_check():
                return None, "unknown_page_state"
        except Exception:
            return None, "unknown_page_state"

        # 4. Atomic claim via server (locks attempt and sets save_claim_count 0 -> 1)
        claim_result = await self._api.claim_unit2_save(lease)
        if claim_result != "claimed_now":
            logger.warning("save_claim_rejected result=%s", claim_result)
            return None, claim_result

        # 5. Click the Save button ONCE (never retry)
        await adapter.click_save_button_once(save_button)

        # 6. First next-section observation
        observation1 = await adapter.observe_next_section()
        if observation1.issue_code:
            return None, observation1.issue_code
        try:
            if not account_continuity_check():
                return None, "unknown_page_state"
        except Exception:
            return None, "unknown_page_state"
        if not observation1.is_valid:
            return None, "unknown_next_section"

        if not verify_page_continuity(
            current_url=observation1.current_url,
            target_url=target_url,
            application_id=lease.application_id,
            page_evidence=observation1,
        ):
            logger.warning("save_step_continuity_failed_obs1")
            return None, "unknown_next_section"

        section_class1 = observation1.section_class
        sig1 = bind_safe_structural_signature(
            observation1.safe_signature,
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
        )

        # Pre-click signature must have changed
        if sig1 == prepared_form.prepared_safe_signature:
            return None, "signature_unchanged"

        # 7. Bounded pause before second observation
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)

        # 8. Second next-section observation for stability
        observation2 = await adapter.observe_next_section()
        if observation2.issue_code:
            return None, observation2.issue_code
        try:
            if not account_continuity_check():
                return None, "unknown_page_state"
        except Exception:
            return None, "unknown_page_state"
        if not observation2.is_valid:
            return None, "unknown_next_section"

        if not verify_page_continuity(
            current_url=observation2.current_url,
            target_url=target_url,
            application_id=lease.application_id,
            page_evidence=observation2,
        ):
            logger.warning("save_step_continuity_failed_obs2")
            return None, "unknown_next_section"

        section_class2 = observation2.section_class
        sig2 = bind_safe_structural_signature(
            observation2.safe_signature,
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
        )
        if (
            observation2.current_url != observation1.current_url
            or observation2.heading_name != observation1.heading_name
            or section_class2 != section_class1
            or sig2 != sig1
        ):
            return None, "unstable_next_section"

        proof = Unit2CheckpointProof(
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
            checkpoint_version="workday_unit2_v1",
            section_class=section_class1,
            safe_signature=sig1,
        )

        return proof, "ready"
