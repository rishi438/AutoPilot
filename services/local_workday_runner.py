"""One-lease local Workday runner; form submission remains deliberately disabled."""

from __future__ import annotations

import logging
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Callable, Protocol

from services.portal_account_automation import NativePortalAccountCoordinator
from services.portal_credentials import PortalCredentialError
from services.workday_playwright_worker import (
    WorkdayAccountGateStatus,
    WorkdayAccountGateWorker,
    WorkdayBrowser,
    WorkdayWorkerError,
)
from services.workday_worker_api import (
    LeasedWorkdayApplication,
    WorkdayWorkerApi,
)
from services.workday_unit1_orchestrator import (
    WorkdayExecutionPhase,
    WorkdayUnit1ExecutionError,
    WorkdayUnit1Result,
    WorkdayUnit1Status,
)

logger = logging.getLogger(__name__)

_HOLD_REMEDIATION = {
    "captcha": (
        "The agent paused this job because CAPTCHA cannot be automated safely; "
        "continue processing other eligible jobs."
    ),
    "otp": (
        "The agent paused this job because OTP verification cannot be automated "
        "safely; continue processing other eligible jobs."
    ),
    "native_credentials_required": (
        "Import or correct this Workday credential in the AutoPilot Credential Vault."
    ),
    "unknown_page_state": (
        "Review the Workday page in the dedicated Autopilot Browser before retrying."
    ),
    "unknown_required_question": (
        "Add and approve the missing required answer in AutoPilot before retrying."
    ),
    "unsupported_step": (
        "Review the unsupported required control in the dedicated AutoPilot Browser."
    ),
    "validation_failure": (
        "A filled value did not remain committed; review the form before retrying."
    ),
    "upload_failure": (
        "The required document upload is deferred until secure resume attachment is enabled."
    ),
}


class LocalWorkdayRunStatus(str, Enum):
    """Credential-free one-lease runner outcome."""

    IDLE = "idle"
    HELD = "held"
    RETRYING = "retrying"
    SKIPPED = "skipped"
    ACCOUNT_READY_DEFERRED = "account_ready_deferred"
    FORM_STEP_FILLED_DEFERRED = "form_step_filled_deferred"
    UNIT1_COMPLETE = "unit1_complete"
    DEFERRED = "deferred"
    REVIEW_REQUIRED = "review_required"


class _WorkdayBrowserStartupFailure(RuntimeError):
    """The browser runtime failed before any portal action could begin."""


@dataclass(frozen=True)
class LocalWorkdayRunResult:
    status: LocalWorkdayRunStatus
    application_id: str | None = None
    hold_code: str | None = None
    safe_reason: str | None = None


class WorkdayBrowserRuntime(Protocol):
    async def __aenter__(self) -> WorkdayBrowser: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class WorkdayUnit1Executor(Protocol):
    async def run(self, lease: LeasedWorkdayApplication) -> WorkdayUnit1Result: ...


class LocalWorkdayRunner:
    """Lease and execute one account-gate job without enabling form submission."""

    def __init__(
        self,
        *,
        api: WorkdayWorkerApi,
        coordinator: NativePortalAccountCoordinator,
        browser_runtime: WorkdayBrowserRuntime | None = None,
        browser_runtime_factory: (
            Callable[[LeasedWorkdayApplication], WorkdayBrowserRuntime] | None
        ) = None,
        unit1_orchestrator: WorkdayUnit1Executor | None = None,
        initial_lease: LeasedWorkdayApplication | None = None,
    ):
        legacy_runtime_count = sum(
            item is not None for item in (browser_runtime, browser_runtime_factory)
        )
        if unit1_orchestrator is None and legacy_runtime_count != 1:
            raise ValueError(
                "Exactly one Workday browser runtime or runtime factory is required."
            )
        if unit1_orchestrator is not None and legacy_runtime_count:
            raise ValueError(
                "The Unit 1 orchestrator owns its browser boundary when configured."
            )
        self._api = api
        self._coordinator = coordinator
        self._browser_runtime = browser_runtime
        self._browser_runtime_factory = browser_runtime_factory
        self._unit1_orchestrator = unit1_orchestrator
        self._initial_lease = initial_lease

    async def run_once(
        self, *, application_id: uuid.UUID | None = None
    ) -> LocalWorkdayRunResult:
        logger.info(
            "workday_lease_request_started targeted=%s",
            application_id is not None,
        )
        lease = self._initial_lease
        self._initial_lease = None
        if lease is not None:
            if application_id is not None and lease.application_id != application_id:
                raise RuntimeError(
                    "The preflight Workday lease does not match the target."
                )
        else:
            lease_method = getattr(self._api, "lease_next", None)
            if self._unit1_orchestrator is not None:
                unit1_lease_method = getattr(self._api, "lease_next_unit1", None)
                if callable(unit1_lease_method):
                    lease_method = unit1_lease_method
            if not callable(lease_method):
                raise RuntimeError("The Workday worker lease operation is unavailable.")
            lease = await lease_method(application_id=application_id)
        if lease is None:
            logger.info("workday_runner_idle reason=no_eligible_lease")
            return LocalWorkdayRunResult(LocalWorkdayRunStatus.IDLE)

        application_id = str(lease.application_id)
        logger.info(
            "workday_lease_acquired application_id=%s portal=%s",
            application_id,
            lease.portal,
        )

        if self._unit1_orchestrator is not None:
            try:
                unit1_result = await self._unit1_orchestrator.run(lease)
            except WorkdayUnit1ExecutionError as exc:
                if exc.phase is WorkdayExecutionPhase.BEFORE_BROWSER_ACTION:
                    if lease.gate_decision == "observe_only":
                        await self._api.record_unit1_review(
                            lease,
                            authentication_submitted=True,
                        )
                        return LocalWorkdayRunResult(
                            LocalWorkdayRunStatus.REVIEW_REQUIRED,
                            application_id=application_id,
                            safe_reason="workday_observe_only_startup_failed",
                        )
                    await self._api.release_startup_lease(lease)
                    return LocalWorkdayRunResult(
                        LocalWorkdayRunStatus.RETRYING,
                        application_id=application_id,
                        safe_reason="workday_browser_startup_failed",
                    )
                if exc.phase is WorkdayExecutionPhase.SUBMIT_CLAIMED_PENDING:
                    await self._api.record_unit1_review(
                        lease,
                        authentication_submitted=True,
                    )
                    return LocalWorkdayRunResult(
                        LocalWorkdayRunStatus.REVIEW_REQUIRED,
                        application_id=application_id,
                        safe_reason="workday_unit1_review_required",
                    )
                if exc.phase is WorkdayExecutionPhase.STALE_AUTHORITY:
                    return LocalWorkdayRunResult(
                        LocalWorkdayRunStatus.REVIEW_REQUIRED,
                        application_id=application_id,
                        safe_reason="workday_unit1_stale_authority",
                    )
                raise
            status_map = {
                WorkdayUnit1Status.COMPLETE: LocalWorkdayRunStatus.UNIT1_COMPLETE,
                WorkdayUnit1Status.DEFERRED: LocalWorkdayRunStatus.DEFERRED,
                WorkdayUnit1Status.HELD: LocalWorkdayRunStatus.HELD,
                WorkdayUnit1Status.REVIEW_REQUIRED: (
                    LocalWorkdayRunStatus.REVIEW_REQUIRED
                ),
                WorkdayUnit1Status.SKIPPED: LocalWorkdayRunStatus.SKIPPED,
                WorkdayUnit1Status.BACKOFF: LocalWorkdayRunStatus.RETRYING,
            }
            return LocalWorkdayRunResult(
                status_map[unit1_result.status], application_id=application_id
            )

        try:
            (
                outcome,
                form_hold_code,
                form_hold_question,
                form_safe_reason,
            ) = await self._run_account_gate(lease)
        except _WorkdayBrowserStartupFailure:
            logger.exception(
                "workday_browser_startup_failed application_id=%s",
                application_id,
            )
            await self._api.release_startup_lease(lease)
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.RETRYING,
                application_id=application_id,
                safe_reason="workday_browser_startup_failed",
            )
        except WorkdayWorkerError as exc:
            logger.exception(
                "workday_account_gate_action_failed application_id=%s safe_code=%s",
                application_id,
                exc.safe_code,
            )
            await self._api.record_retry(
                lease,
                safe_reason=exc.safe_code,
            )
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.RETRYING,
                application_id=str(lease.application_id),
                safe_reason=exc.safe_code,
            )
        except PortalCredentialError:
            logger.exception(
                "workday_vault_operation_failed application_id=%s",
                application_id,
            )
            await self._api.record_retry(
                lease,
                safe_reason="workday_account_gate_action_failed",
            )
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.RETRYING,
                application_id=str(lease.application_id),
                safe_reason="workday_account_gate_action_failed",
            )

        if outcome.status is WorkdayAccountGateStatus.HOLD:
            hold_code = outcome.hold_code or "unknown_page_state"
            logger.warning(
                "workday_account_gate_held application_id=%s hold_code=%s page_state=%s",
                application_id,
                hold_code,
                outcome.page_state.value,
            )
            await self._api.create_hold(
                lease,
                hold_code=hold_code,
                remediation=_HOLD_REMEDIATION.get(
                    hold_code, _HOLD_REMEDIATION["unknown_page_state"]
                ),
            )
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.HELD,
                application_id=str(lease.application_id),
                hold_code=hold_code,
            )
        if outcome.status is WorkdayAccountGateStatus.RETRY_LATER:
            logger.warning(
                "workday_account_gate_retry_later application_id=%s page_state=%s",
                application_id,
                outcome.page_state.value,
            )
            await self._api.record_retry(
                lease,
                safe_reason="workday_account_gate_transient_failure",
            )
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.RETRYING,
                application_id=str(lease.application_id),
                safe_reason="workday_account_gate_transient_failure",
            )
        if outcome.status is WorkdayAccountGateStatus.SKIPPED:
            logger.info(
                "workday_account_gate_skipped application_id=%s page_state=%s",
                application_id,
                outcome.page_state.value,
            )
            await self._api.record_skip(lease)
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.SKIPPED,
                application_id=str(lease.application_id),
            )

        if form_hold_code is not None:
            logger.warning(
                "workday_form_step_held application_id=%s hold_code=%s",
                application_id,
                form_hold_code,
            )
            await self._api.create_hold(
                lease,
                hold_code=form_hold_code,
                remediation=_HOLD_REMEDIATION[form_hold_code],
                question=form_hold_question,
            )
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.HELD,
                application_id=application_id,
                hold_code=form_hold_code,
            )

        if form_safe_reason is not None:
            await self._api.record_retry(lease, safe_reason=form_safe_reason)
            return LocalWorkdayRunResult(
                LocalWorkdayRunStatus.FORM_STEP_FILLED_DEFERRED,
                application_id=application_id,
                safe_reason=form_safe_reason,
            )

        # The current vertical slice intentionally stops before form inspection.
        # Release the lease as retryable so the next slice can resume it safely.
        logger.info(
            "workday_account_gate_ready application_id=%s page_state=%s next=form_fill_pending",
            application_id,
            outcome.page_state.value,
        )
        await self._api.record_retry(
            lease,
            safe_reason="workday_account_ready_form_fill_pending",
        )
        return LocalWorkdayRunResult(
            LocalWorkdayRunStatus.ACCOUNT_READY_DEFERRED,
            application_id=str(lease.application_id),
        )

    async def _run_account_gate(self, lease: LeasedWorkdayApplication):
        portal_name = (
            f"{lease.company_name} Workday" if lease.company_name else "Workday"
        )
        logger.info(
            "workday_browser_account_gate_started application_id=%s",
            lease.application_id,
        )
        stack = AsyncExitStack()
        try:
            browser_runtime = (
                self._browser_runtime_factory(lease)
                if self._browser_runtime_factory is not None
                else self._browser_runtime
            )
            if browser_runtime is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("The Workday browser runtime is unavailable.")
            browser = await stack.enter_async_context(browser_runtime)
        except Exception as exc:
            await stack.aclose()
            raise _WorkdayBrowserStartupFailure(
                "The Workday browser runtime could not start."
            ) from exc

        async with stack:
            worker = WorkdayAccountGateWorker(
                browser,
                self._coordinator,
                self._api.state_emitter(lease),
            )
            outcome = await worker.run(
                lease=lease.to_workday_lease(),
                portal_name=portal_name,
                account_email="",
            )
            logger.info(
                "workday_browser_account_gate_completed application_id=%s status=%s page_state=%s",
                lease.application_id,
                outcome.status.value,
                outcome.page_state.value,
            )
            if outcome.status is not WorkdayAccountGateStatus.ACCOUNT_READY:
                return outcome, None, None, None

            for step_number in (1, 2):
                hold_code, hold_question = await self._fill_current_form_step(
                    browser,
                    lease,
                    step_number=step_number,
                )
                if hold_code is not None:
                    return outcome, hold_code, hold_question, None
                if step_number == 1:
                    await browser.advance_to_next_application_step()

            return (
                outcome,
                None,
                None,
                "workday_second_form_step_verified_navigation_pending",
            )

    async def _fill_current_form_step(
        self,
        browser: WorkdayBrowser,
        lease: LeasedWorkdayApplication,
        *,
        step_number: int,
    ) -> tuple[str | None, str | None]:
        page_url, fields = await browser.scan_application_fields()
        if not fields:
            return "unsupported_step", None
        assignments = await self._api.map_approved_form_fields(
            lease,
            page_url=page_url,
            fields=fields,
        )
        fill_result = await browser.fill_and_verify_application_fields(assignments)
        required = {field.field_uid for field in fields if field.required}
        assigned = {
            item.field_uid
            for item in assignments
            if item.answer_source in {"profile", "approved_rule"} and item.value
        }
        required_files = {
            field.field_uid
            for field in fields
            if field.required and field.input_type == "file"
        }
        if required_files:
            return "upload_failure", None
        if fill_result.failed_field_uids:
            failed_uid = fill_result.failed_field_uids[0]
            failed_field = next(
                (field for field in fields if field.field_uid == failed_uid),
                None,
            )
            failed_question = (
                next(
                    (
                        " ".join(candidate.split())[:2000]
                        for candidate in (
                            failed_field.label_text,
                            failed_field.aria_label,
                            failed_field.placeholder,
                            failed_field.name_attr,
                            failed_field.id_attr,
                        )
                        if candidate and candidate.strip()
                    ),
                    None,
                )
                if failed_field is not None
                else None
            )
            logger.warning(
                "workday_form_field_validation_failed application_id=%s step=%s field_uid=%s input_type=%s",
                lease.application_id,
                step_number,
                failed_uid,
                failed_field.input_type if failed_field is not None else "unknown",
            )
            return "validation_failure", failed_question
        if required.intersection(fill_result.unsupported_field_uids):
            return "unsupported_step", None
        missing_required = [
            field
            for field in fields
            if field.required and field.field_uid not in assigned
        ]
        if missing_required:
            missing_field = missing_required[0]
            question = next(
                (
                    " ".join(candidate.split())[:2000]
                    for candidate in (
                        missing_field.label_text,
                        missing_field.aria_label,
                        missing_field.placeholder,
                        missing_field.name_attr,
                        missing_field.id_attr,
                    )
                    if candidate and candidate.strip()
                ),
                None,
            )
            if question is None:
                return "unsupported_step", None
            return "unknown_required_question", question
        logger.info(
            "workday_form_step_verified application_id=%s step=%s field_count=%s filled_count=%s",
            lease.application_id,
            step_number,
            len(fields),
            fill_result.verified_count,
        )
        return None, None
