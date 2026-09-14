from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.workday_form_step_policy import (
    Unit2PreparedForm,
    WorkdayFormStepPolicyResult,
)
from services.workday_unit2_checkpoint import (
    Unit2CheckpointProof,
    Unit2ResumeProof,
    WorkdayNextSectionClass,
    bind_safe_structural_signature,
)
from services.workday_unit2_orchestrator import (
    WorkdayUnit2ExecutionPhase,
    WorkdayUnit2Orchestrator,
    WorkdayUnit2Result,
)
from services.workday_unit2_save_step import Unit2NextSectionObservation
from services.workday_worker_api import LeasedWorkdayUnit2Application


def _make_lease(mode: str = "normal") -> LeasedWorkdayUnit2Application:
    return LeasedWorkdayUnit2Application(
        application_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        mode=mode,
        user_id=uuid.uuid4(),
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/R-1/apply",
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
    )


_NO_PAGE = object()


class _MockBrowserRuntime:
    def __init__(self, page: Any = _NO_PAGE):
        self.page = AsyncMock() if page is _NO_PAGE else page
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.exited = True

    def borrow_playwright_browser_for_page(self):
        return self.page

    def verify_account_continuity(self, user_id: uuid.UUID) -> bool:
        return bool(user_id) and self.page is not None


@pytest.mark.asyncio
async def test_orchestrator_idle_when_no_lease() -> None:
    mock_api = AsyncMock()
    mock_api.lease_next_unit2.return_value = None

    orchestrator = WorkdayUnit2Orchestrator(
        worker_api=mock_api,
        repository_root=Path("."),
    )

    result = await orchestrator.run_once()
    assert result.status == "idle"
    assert result.phase == WorkdayUnit2ExecutionPhase.PREFLIGHT
    assert result.application_id is None


@pytest.mark.asyncio
async def test_orchestrator_browser_borrow_failure_releases_startup_lease() -> None:
    lease = _make_lease()
    mock_api = AsyncMock()
    mock_api.lease_next_unit2.return_value = lease
    mock_api.release_startup_lease_unit2.return_value = "released"

    runtime = _MockBrowserRuntime(page=None)  # No page found
    orchestrator = WorkdayUnit2Orchestrator(
        worker_api=mock_api,
        repository_root=Path("."),
        runtime_factory=lambda uid: runtime,
    )

    result = await orchestrator.run_once()
    assert result.status == "released"
    assert result.phase == WorkdayUnit2ExecutionPhase.BROWSER_ACQUISITION
    assert runtime.entered is True
    assert runtime.exited is True
    mock_api.release_startup_lease_unit2.assert_awaited_once_with(
        lease, reason="browser_page_unavailable"
    )


@pytest.mark.asyncio
async def test_orchestrator_normal_mode_complete_flow() -> None:
    lease = _make_lease(mode="normal")
    mock_api = AsyncMock()
    mock_api.lease_next_unit2.return_value = lease
    mock_api.finalize_unit2.return_value = "completed"

    mock_page = AsyncMock()
    runtime = _MockBrowserRuntime(page=mock_page)

    mock_resume = AsyncMock()
    resume_proof = Unit2ResumeProof(
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        approved_origin=True,
        canonical_tenant_verified=True,
        job_context_matches=True,
        application_context_matches=True,
        durable_account_continuity_verified=True,
        authenticated_state_verified=True,
        my_information_structure_verified=True,
        safe_signature=bind_safe_structural_signature(
            "sig_resume_123",
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
        ),
    )
    mock_resume.resume_and_verify.return_value = (resume_proof, "ready")

    mock_save = AsyncMock()
    checkpoint_proof = Unit2CheckpointProof(
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        checkpoint_version="workday_unit2_v1",
        section_class=WorkdayNextSectionClass.MY_EXPERIENCE,
        safe_signature="sig_next_456",
    )
    mock_save.execute_save_and_verify_checkpoint.return_value = (
        checkpoint_proof,
        "ready",
    )

    prepared_form = Unit2PreparedForm(
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        resume_safe_signature="sig_resume_123",
        prepared_safe_signature="sig_prep_123",
        required_count=1,
        verified_count=1,
    )

    orchestrator = WorkdayUnit2Orchestrator(
        worker_api=mock_api,
        repository_root=Path("."),
        runtime_factory=lambda uid: runtime,
        resume_coordinator=mock_resume,
        save_step_coordinator=mock_save,
    )

    with patch(
        "services.workday_unit2_orchestrator.execute_workday_form_step",
        autospec=True,
    ) as mock_form_step:
        mock_form_step.return_value = WorkdayFormStepPolicyResult(
            prepared_form=prepared_form,
            hold_code=None,
            hold_question=None,
        )
        result = await orchestrator.run_once()
        assert result.status == "completed"
        assert result.phase == WorkdayUnit2ExecutionPhase.FINALIZATION
        assert runtime.entered is True
        assert runtime.exited is True
        mock_api.finalize_unit2.assert_awaited_once_with(
            lease,
            outcome="complete",
            checkpoint_version="workday_unit2_v1",
        )
        mock_form_step.assert_awaited_once()
        call_kwargs = mock_form_step.await_args.kwargs
        assert call_kwargs["browser"] is runtime
        assert call_kwargs["application_id"] == lease.application_id
        assert call_kwargs["attempt_id"] == lease.attempt_id
        assert call_kwargs["lease_id"] == lease.lease_id


@pytest.mark.asyncio
async def test_orchestrator_observe_only_mode_recovery() -> None:
    lease = _make_lease(mode="observe_only")
    mock_api = AsyncMock()
    mock_api.lease_next_unit2.return_value = lease
    mock_api.finalize_unit2.return_value = "completed"

    mock_page = AsyncMock()
    mock_page.url = "https://wd1.myworkdaysite.com/job/R-1/apply"
    mock_page.query_selector.return_value = None
    mock_page.query_selector_all.return_value = []

    runtime = _MockBrowserRuntime(page=mock_page)

    mock_resume = AsyncMock()
    mock_save = AsyncMock()

    orchestrator = WorkdayUnit2Orchestrator(
        worker_api=mock_api,
        repository_root=Path("."),
        runtime_factory=lambda uid: runtime,
        resume_coordinator=mock_resume,
        save_step_coordinator=mock_save,
    )

    observation = Unit2NextSectionObservation(
        current_url=lease.job_url,
        heading_name="My Experience",
        section_class=WorkdayNextSectionClass.MY_EXPERIENCE,
        safe_signature="sig_obs_1",
        visible_field_count=1,
        visible_button_count=2,
        is_hydrated=True,
        is_authenticated=True,
        has_validation_errors=False,
        issue_code=None,
    )

    # In observe-only mode, it observes the typed next-section evidence twice.
    with patch(
        "services.workday_unit2_orchestrator.PlaywrightSaveButtonAdapter.observe_next_section",
        AsyncMock(side_effect=[observation, observation]),
    ):
        result = await orchestrator.run_once()
        assert result.status == "completed"
        assert result.phase == WorkdayUnit2ExecutionPhase.FINALIZATION
        # Ensure form fill and save step coordinators were NOT called in observe-only mode!
        mock_resume.resume_and_verify.assert_not_called()
        mock_save.execute_save_and_verify_checkpoint.assert_not_called()
        mock_api.finalize_unit2.assert_awaited_once_with(
            lease,
            outcome="complete",
            checkpoint_version="workday_unit2_v1",
        )


@pytest.mark.asyncio
async def test_orchestrator_resume_expired_session_review_required() -> None:
    lease = _make_lease(mode="normal")
    mock_api = AsyncMock()
    mock_api.lease_next_unit2.return_value = lease
    mock_api.finalize_unit2.return_value = "review_required"

    mock_page = AsyncMock()
    runtime = _MockBrowserRuntime(page=mock_page)

    mock_resume = AsyncMock()
    mock_resume.resume_and_verify.return_value = (None, "expired_session")

    orchestrator = WorkdayUnit2Orchestrator(
        worker_api=mock_api,
        repository_root=Path("."),
        runtime_factory=lambda uid: runtime,
        resume_coordinator=mock_resume,
    )

    result = await orchestrator.run_once()
    assert result.status == "review_required"
    assert result.hold_code == "expired_session"
    assert result.phase == WorkdayUnit2ExecutionPhase.SESSION_RESUME
    mock_api.finalize_unit2.assert_awaited_once_with(
        lease, outcome="review_required", hold_code="expired_session"
    )
