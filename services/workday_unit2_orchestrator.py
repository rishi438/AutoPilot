"""Bounded foreground orchestrator for Workday Unit 2 (My Information)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import UUID

from services.workday_browser_runtime import (
    PersistentWorkdayBrowserRuntime,
    user_scoped_autopilot_browser_profile,
)
from services.workday_form_step_policy import (
    Unit2PreparedForm,
    execute_workday_form_step,
)
from services.workday_unit2_checkpoint import (
    PlaywrightWorkdayUnit2PageAdapter,
    Unit2CheckpointProof,
    WorkdayNextSectionClass,
    WorkdayUnit2ResumeCoordinator,
    bind_safe_structural_signature,
)
from services.workday_unit2_save_step import (
    PlaywrightSaveButtonAdapter,
    WorkdayUnit2SaveStepCoordinator,
    verify_page_continuity,
)
from services.workday_worker_api import (
    LeasedWorkdayUnit2Application,
    WorkdayWorkerApi,
)

logger = logging.getLogger(__name__)


class WorkdayUnit2ExecutionPhase(str, Enum):
    """Irreversible execution boundaries in Workday Unit 2."""

    PREFLIGHT = "preflight"
    BROWSER_ACQUISITION = "browser_acquisition"
    SESSION_RESUME = "session_resume"
    FORM_PREPARATION = "form_preparation"
    SAVE_CLAIM = "save_claim"
    SAVE_CLICK = "save_click"
    NEXT_SECTION_CHECKPOINT = "next_section_checkpoint"
    FINALIZATION = "finalization"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class WorkdayUnit2Result:
    """Safe, credential-free summary of one Unit 2 run."""

    status: Literal["completed", "review_required", "released", "idle", "failed"]
    application_id: UUID | None = None
    attempt_id: UUID | None = None
    lease_id: UUID | None = None
    phase: WorkdayUnit2ExecutionPhase = WorkdayUnit2ExecutionPhase.PREFLIGHT
    hold_code: str | None = None
    safe_reason: str | None = None


class WorkdayUnit2Orchestrator:
    """Orchestrates one bounded lease of Workday Unit 2 without LLM or second Save clicks."""

    def __init__(
        self,
        *,
        worker_api: WorkdayWorkerApi,
        repository_root: Path,
        headless: bool = True,
        runtime_factory: Callable[[str], PersistentWorkdayBrowserRuntime] | None = None,
        resume_coordinator: WorkdayUnit2ResumeCoordinator | None = None,
        save_step_coordinator: WorkdayUnit2SaveStepCoordinator | None = None,
    ) -> None:
        self._worker_api = worker_api
        self._repo_root = repository_root
        self._headless = headless
        self._runtime_factory = runtime_factory
        self._resume_coordinator = resume_coordinator or WorkdayUnit2ResumeCoordinator()
        self._save_step_coordinator = (
            save_step_coordinator
            or WorkdayUnit2SaveStepCoordinator(worker_api=worker_api)
        )

    def _create_runtime(self, user_id: str) -> Any:
        if self._runtime_factory is not None:
            return self._runtime_factory(user_id)
        profile_dir = user_scoped_autopilot_browser_profile(user_id, must_exist=True)
        return PersistentWorkdayBrowserRuntime(
            profile_dir=profile_dir,
            repository_root=self._repo_root,
            headless=self._headless,
            control_resolver=None,  # Zero LLM controls permitted in Unit 2
        )

    def _verify_durable_account_continuity(
        self, lease: LeasedWorkdayUnit2Application, runtime: Any
    ) -> bool:
        """Factual check that the browser runtime owns the genuine locked profile for the leased user."""
        try:
            if hasattr(runtime, "verify_account_continuity"):
                return bool(runtime.verify_account_continuity(lease.user_id))

            # Factual check for runtimes exposing profile_dir / _profile_dir and lock
            profile_dir = getattr(runtime, "profile_dir", None) or getattr(
                runtime, "_profile_dir", None
            )
            if profile_dir is not None:
                if not isinstance(profile_dir, Path) or not profile_dir.exists():
                    return False
                expected_profile = user_scoped_autopilot_browser_profile(
                    str(lease.user_id), must_exist=True
                ).resolve()
                if profile_dir.resolve() != expected_profile:
                    return False
                lock = getattr(runtime, "_lock", None) or getattr(runtime, "lock", None)
                if lock is None or not getattr(lock, "is_locked", False):
                    return False
                return True

            return False
        except Exception:
            return False

    async def run_once(
        self, *, application_id: UUID | None = None
    ) -> WorkdayUnit2Result:
        """Execute one bounded Unit 2 lease cycle from preflight to finalization."""
        # 1. Preflight lease
        try:
            lease = await self._worker_api.lease_next_unit2(
                application_id=application_id
            )
        except Exception as exc:
            logger.error("unit2_lease_preflight_failed exc=%s", exc)
            return WorkdayUnit2Result(
                status="failed",
                phase=WorkdayUnit2ExecutionPhase.PREFLIGHT,
                safe_reason="lease_preflight_failed",
            )

        if lease is None:
            return WorkdayUnit2Result(
                status="idle",
                phase=WorkdayUnit2ExecutionPhase.PREFLIGHT,
            )

        # 2. Acquire browser runtime
        async def _handle_browser_acquisition_failure(
            safe_reason: str,
        ) -> WorkdayUnit2Result:
            if lease.mode == "observe_only":
                await self._worker_api.finalize_unit2(
                    lease, outcome="review_required", hold_code="unknown_page_state"
                )
                return WorkdayUnit2Result(
                    status="review_required",
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                    phase=WorkdayUnit2ExecutionPhase.BROWSER_ACQUISITION,
                    hold_code="unknown_page_state",
                    safe_reason=safe_reason,
                )
            await self._worker_api.release_startup_lease_unit2(
                lease, reason=safe_reason
            )
            return WorkdayUnit2Result(
                status="released",
                application_id=lease.application_id,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                phase=WorkdayUnit2ExecutionPhase.BROWSER_ACQUISITION,
                safe_reason=safe_reason,
            )

        try:
            runtime = self._create_runtime(str(lease.user_id))
        except Exception as exc:
            logger.warning("unit2_runtime_creation_failed exc=%s", exc)
            return await _handle_browser_acquisition_failure(
                "browser_profile_unavailable"
            )

        async with AsyncExitStack() as stack:
            try:
                browser = await stack.enter_async_context(runtime)
                page = getattr(browser, "raw_page", None) or getattr(
                    browser, "page", None
                )
                if page is None and hasattr(
                    runtime, "borrow_playwright_browser_for_page"
                ):
                    borrowed = runtime.borrow_playwright_browser_for_page()
                    page = (
                        getattr(borrowed, "raw_page", None)
                        or getattr(borrowed, "page", None)
                        or borrowed
                    )
            except Exception as exc:
                logger.warning("unit2_browser_borrow_failed exc=%s", exc)
                return await _handle_browser_acquisition_failure(
                    "browser_page_borrow_failed"
                )

            if page is None:
                return await _handle_browser_acquisition_failure(
                    "browser_page_unavailable"
                )

            # 3. Observe-only recovery mode (for post-claim holds or expired attempts)
            if lease.mode == "observe_only":
                target_url = lease.external_ats_url or lease.job_url
                if not self._verify_durable_account_continuity(lease, runtime):
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code="unknown_page_state"
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code="unknown_page_state",
                        safe_reason="account_continuity_unverified",
                    )

                save_adapter = PlaywrightSaveButtonAdapter(page)
                observation1 = await save_adapter.observe_next_section()
                if observation1.issue_code or not observation1.is_valid:
                    hold_code = observation1.issue_code or "unsupported_step"
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code=hold_code
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code=hold_code,
                    )

                if not verify_page_continuity(
                    current_url=observation1.current_url,
                    target_url=target_url,
                    application_id=lease.application_id,
                    page_evidence=observation1,
                ):
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code="unknown_page_state"
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code="unknown_page_state",
                        safe_reason="continuity_verification_failed",
                    )

                section_class1 = observation1.section_class
                sig1 = bind_safe_structural_signature(
                    observation1.safe_signature,
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                )

                await asyncio.sleep(0.15)
                if not self._verify_durable_account_continuity(lease, runtime):
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code="unknown_page_state"
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code="unknown_page_state",
                        safe_reason="account_continuity_unverified",
                    )
                observation2 = await save_adapter.observe_next_section()
                sig2 = bind_safe_structural_signature(
                    observation2.safe_signature,
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                )
                if (
                    observation2.issue_code
                    or not observation2.is_valid
                    or observation2.current_url != observation1.current_url
                    or observation2.heading_name != observation1.heading_name
                    or observation2.section_class != section_class1
                    or sig2 != sig1
                ):
                    hold_code = observation2.issue_code or "unknown_page_state"
                    await self._worker_api.finalize_unit2(
                        lease,
                        outcome="review_required",
                        hold_code=hold_code,
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code=hold_code,
                    )

                if not verify_page_continuity(
                    current_url=observation2.current_url,
                    target_url=target_url,
                    application_id=lease.application_id,
                    page_evidence=observation2,
                ):
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code="unknown_page_state"
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code="unknown_page_state",
                        safe_reason="continuity_verification_failed",
                    )

                checkpoint_proof = Unit2CheckpointProof(
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                    checkpoint_version="workday_unit2_v1",
                    section_class=section_class1,
                    safe_signature=sig1,
                )
                if not checkpoint_proof.is_valid:
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code="unknown_page_state"
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.NEXT_SECTION_CHECKPOINT,
                        hold_code="unknown_page_state",
                    )

                # Valid stable checkpoint verified in observe-only mode.
                await self._worker_api.finalize_unit2(
                    lease,
                    outcome="complete",
                    checkpoint_version="workday_unit2_v1",
                )
                return WorkdayUnit2Result(
                    status="completed",
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                    phase=WorkdayUnit2ExecutionPhase.FINALIZATION,
                )

            # 4. Normal mode: Session resume & evidence verification
            account_continuity = self._verify_durable_account_continuity(lease, runtime)
            page_adapter = PlaywrightWorkdayUnit2PageAdapter(page)
            proof, resume_outcome = await self._resume_coordinator.resume_and_verify(
                lease=lease,
                adapter=page_adapter,
                durable_account_continuity_verified=account_continuity,
            )
            if (
                resume_outcome != "ready"
                or proof is None
                or not proof.is_valid
                or proof.application_id != lease.application_id
                or proof.attempt_id != lease.attempt_id
                or proof.lease_id != lease.lease_id
            ):
                if resume_outcome in (
                    "expired_session",
                    "captcha",
                    "otp",
                    "account_temporarily_locked",
                    "email_verification",
                    "locked",
                ):
                    hold_code = (
                        "account_temporarily_locked"
                        if resume_outcome == "locked"
                        else resume_outcome
                    )
                    await self._worker_api.finalize_unit2(
                        lease, outcome="review_required", hold_code=hold_code
                    )
                    return WorkdayUnit2Result(
                        status="review_required",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.SESSION_RESUME,
                        hold_code=hold_code,
                    )
                else:
                    await self._worker_api.finalize_unit2(
                        lease, outcome="preclick_release"
                    )
                    return WorkdayUnit2Result(
                        status="released",
                        application_id=lease.application_id,
                        attempt_id=lease.attempt_id,
                        lease_id=lease.lease_id,
                        phase=WorkdayUnit2ExecutionPhase.SESSION_RESUME,
                        safe_reason=resume_outcome or "resume_verification_failed",
                    )

            # 5. Form preparation (scan, server-side approved field mapping, fill, and verification)
            async def _map_fields(page_url: str, fields: list[Any]) -> list[Any]:
                return await self._worker_api.map_approved_form_fields(
                    lease, page_url=page_url, fields=fields
                )

            fill_result = await execute_workday_form_step(
                browser=browser,
                map_fields_fn=_map_fields,
                application_id=lease.application_id,
                step_number=1,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                resume_safe_signature=proof.safe_signature,
            )
            if fill_result.prepared_form is None:
                hold_code = fill_result.hold_code or "unknown_page_state"
                await self._worker_api.finalize_unit2(
                    lease,
                    outcome="review_required",
                    hold_code=hold_code,
                    question=fill_result.hold_question,
                )
                return WorkdayUnit2Result(
                    status="review_required",
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                    phase=WorkdayUnit2ExecutionPhase.FORM_PREPARATION,
                    hold_code=hold_code,
                )

            # 6. Save claim, single click, and dual next section checkpoint verification
            save_adapter = PlaywrightSaveButtonAdapter(page)
            checkpoint_proof, save_outcome = (
                await self._save_step_coordinator.execute_save_and_verify_checkpoint(
                    lease=lease,
                    prepared_form=fill_result.prepared_form,
                    adapter=save_adapter,
                    account_continuity_check=lambda: self._verify_durable_account_continuity(
                        lease, runtime
                    ),
                )
            )
            if save_outcome != "ready" or checkpoint_proof is None:
                if save_outcome == "challenge_detected":
                    hold_code = "captcha"
                elif save_outcome in (
                    "page_drift",
                    "save_button_unresolved",
                    "unstable_next_section",
                ):
                    hold_code = "unknown_page_state"
                elif save_outcome in (
                    "disallowed_or_unsupported_section",
                    "unknown_next_section",
                ):
                    hold_code = "unsupported_step"
                elif save_outcome in (
                    "expired_session",
                    "captcha",
                    "otp",
                    "email_verification",
                    "account_temporarily_locked",
                    "unfamiliar_consent",
                    "unknown_required_question",
                    "unsupported_step",
                    "validation_failure",
                    "upload_failure",
                    "unknown_page_state",
                ):
                    hold_code = save_outcome
                else:
                    hold_code = "unknown_page_state"

                await self._worker_api.finalize_unit2(
                    lease,
                    outcome="review_required",
                    hold_code=hold_code,
                )
                return WorkdayUnit2Result(
                    status="review_required",
                    application_id=lease.application_id,
                    attempt_id=lease.attempt_id,
                    lease_id=lease.lease_id,
                    phase=WorkdayUnit2ExecutionPhase.SAVE_CLICK,
                    hold_code=hold_code,
                )

            # 7. Finalization: Mark complete atomically
            await self._worker_api.finalize_unit2(
                lease,
                outcome="complete",
                checkpoint_version=checkpoint_proof.checkpoint_version,
            )
            return WorkdayUnit2Result(
                status="completed",
                application_id=lease.application_id,
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                phase=WorkdayUnit2ExecutionPhase.FINALIZATION,
            )
