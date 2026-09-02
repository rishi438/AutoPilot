from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from models.database import (
    ApplicationAutomationEvent,
    ApplicationStatus,
    JobApplication,
    WorkdayUnit2Attempt,
)
from services.workday_form_step_policy import Unit2PreparedForm
from services.workday_unit2_checkpoint import (
    Unit2CheckpointProof,
    WorkdayNextSectionClass,
    bind_safe_structural_signature,
    classify_next_section_heading,
)
from services.workday_unit2_save_step import (
    Unit2NextSectionObservation,
    WorkdaySaveButtonAdapter,
    WorkdayUnit2SaveStepCoordinator,
)
from services.workday_worker_api import LeasedWorkdayUnit2Application


def test_unit2_checkpoint_proof_validity() -> None:
    application_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    lease_id = uuid.uuid4()

    proof = Unit2CheckpointProof(
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        checkpoint_version="workday_unit2_v1",
        section_class=WorkdayNextSectionClass.MY_EXPERIENCE,
        safe_signature="sig_next_123",
    )
    assert proof.is_valid is True

    # Bad version
    with pytest.raises(ValueError):
        Unit2CheckpointProof(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            checkpoint_version="invalid_version",
            section_class=WorkdayNextSectionClass.MY_EXPERIENCE,
            safe_signature="sig_next_123",
        )

    # Empty signature
    with pytest.raises(ValueError):
        Unit2CheckpointProof(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            checkpoint_version="workday_unit2_v1",
            section_class=WorkdayNextSectionClass.MY_EXPERIENCE,
            safe_signature="",
        )


def test_classify_next_section_heading() -> None:
    # Closed allowlist
    assert (
        classify_next_section_heading("My Experience")
        == WorkdayNextSectionClass.MY_EXPERIENCE
    )
    assert (
        classify_next_section_heading("Experience")
        == WorkdayNextSectionClass.MY_EXPERIENCE
    )
    assert (
        classify_next_section_heading("Application Questions")
        == WorkdayNextSectionClass.APPLICATION_QUESTIONS
    )
    assert (
        classify_next_section_heading("Voluntary Disclosures")
        == WorkdayNextSectionClass.VOLUNTARY_DISCLOSURES
    )
    assert (
        classify_next_section_heading("Self-Identification")
        == WorkdayNextSectionClass.SELF_IDENTIFICATION
    )
    assert (
        classify_next_section_heading("Equal Opportunity")
        == WorkdayNextSectionClass.SELF_IDENTIFICATION
    )

    # Disallowed / Review / Submit
    assert classify_next_section_heading("Review") is None
    assert classify_next_section_heading("Review and Submit") is None
    assert classify_next_section_heading("Submit Application") is None
    assert classify_next_section_heading("Application Submitted") is None
    assert classify_next_section_heading("Confirmation") is None
    assert classify_next_section_heading("Unknown Heading") is None
    assert classify_next_section_heading("") is None


class _MockSaveAdapter:
    def __init__(
        self,
        *,
        current_sig: str = "prepared_sig_001",
        save_button_element: Any | None = "mock_save_btn",
        next_headings: tuple[str, ...] = ("My Experience", "My Experience"),
        next_sigs: tuple[str, ...] = ("next_sig_002", "next_sig_002"),
        has_challenge: bool = False,
        current_url: str = "https://wd1.myworkdaysite.com/job/R-1/apply",
    ) -> None:
        self._current_sig = current_sig
        self._save_btn = save_button_element
        self._next_headings = list(next_headings)
        self._next_sigs = list(next_sigs)
        self._heading_idx = 0
        self._has_challenge = has_challenge
        self._current_url = current_url
        self.click_count = 0

    async def current_url(self) -> str:
        return self._current_url

    async def get_page_safe_signature(self) -> str:
        return self._current_sig

    async def resolve_unique_save_button(self) -> Any | None:
        return self._save_btn

    async def click_save_button_once(self, button: Any) -> None:
        self.click_count += 1

    async def observe_next_section(self) -> Unit2NextSectionObservation:
        if self._heading_idx < len(self._next_headings):
            h = self._next_headings[self._heading_idx]
            s = self._next_sigs[self._heading_idx]
            self._heading_idx += 1
        else:
            h = self._next_headings[-1]
            s = self._next_sigs[-1]
        section_class = classify_next_section_heading(h)
        issue_code = (
            "captcha"
            if self._has_challenge
            else (None if section_class is not None else "unknown_page_state")
        )
        return Unit2NextSectionObservation(
            current_url=self._current_url,
            heading_name=h,
            section_class=section_class,
            safe_signature=s,
            visible_field_count=2,
            visible_button_count=2,
            is_hydrated=section_class is not None and issue_code is None,
            is_authenticated=issue_code is None,
            has_validation_errors=False,
            issue_code=issue_code,
        )


def _make_lease() -> LeasedWorkdayUnit2Application:
    return LeasedWorkdayUnit2Application(
        application_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        mode="normal",
        user_id=uuid.uuid4(),
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/job/R-1/apply",
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
    )


def _make_prepared_form(
    lease: LeasedWorkdayUnit2Application,
    *,
    raw_signature: str = "prepared_sig_001",
) -> Unit2PreparedForm:
    return Unit2PreparedForm(
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
        resume_safe_signature="resume_sig_001",
        prepared_safe_signature=bind_safe_structural_signature(
            raw_signature,
            application_id=lease.application_id,
            attempt_id=lease.attempt_id,
            lease_id=lease.lease_id,
        ),
        required_count=2,
        verified_count=2,
    )


@pytest.mark.asyncio
async def test_save_step_successful_claim_and_one_click() -> None:
    lease = _make_lease()
    prepared_form = _make_prepared_form(lease)

    mock_api = AsyncMock()
    mock_api.claim_unit2_save.return_value = "claimed_now"

    adapter = _MockSaveAdapter(current_sig="prepared_sig_001")
    coordinator = WorkdayUnit2SaveStepCoordinator(
        worker_api=mock_api, observation_delay_seconds=0.01
    )

    proof, outcome = await coordinator.execute_save_and_verify_checkpoint(
        lease=lease,
        prepared_form=prepared_form,
        adapter=adapter,
        account_continuity_check=lambda: True,
    )

    assert outcome == "ready"
    assert proof is not None
    assert proof.is_valid is True
    assert proof.section_class == WorkdayNextSectionClass.MY_EXPERIENCE
    assert proof.safe_signature == bind_safe_structural_signature(
        "next_sig_002",
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
    )
    assert adapter.click_count == 1
    mock_api.claim_unit2_save.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_save_step_already_claimed_zero_clicks() -> None:
    lease = _make_lease()
    prepared_form = _make_prepared_form(lease)

    mock_api = AsyncMock()
    mock_api.claim_unit2_save.return_value = "already_claimed"

    adapter = _MockSaveAdapter(current_sig="prepared_sig_001")
    coordinator = WorkdayUnit2SaveStepCoordinator(
        worker_api=mock_api, observation_delay_seconds=0.01
    )

    proof, outcome = await coordinator.execute_save_and_verify_checkpoint(
        lease=lease,
        prepared_form=prepared_form,
        adapter=adapter,
        account_continuity_check=lambda: True,
    )

    assert proof is None
    assert outcome == "already_claimed"
    assert adapter.click_count == 0  # Zero clicks permitted!


@pytest.mark.asyncio
async def test_save_step_page_drift_zero_clicks() -> None:
    lease = _make_lease()
    prepared_form = _make_prepared_form(lease)

    mock_api = AsyncMock()
    adapter = _MockSaveAdapter(current_sig="drifted_sig_999")
    coordinator = WorkdayUnit2SaveStepCoordinator(
        worker_api=mock_api, observation_delay_seconds=0.01
    )

    proof, outcome = await coordinator.execute_save_and_verify_checkpoint(
        lease=lease,
        prepared_form=prepared_form,
        adapter=adapter,
        account_continuity_check=lambda: True,
    )

    assert proof is None
    assert outcome == "page_drift"
    assert adapter.click_count == 0
    mock_api.claim_unit2_save.assert_not_called()


@pytest.mark.asyncio
async def test_save_step_disallowed_review_section_fails_closed() -> None:
    lease = _make_lease()
    prepared_form = _make_prepared_form(lease)

    mock_api = AsyncMock()
    mock_api.claim_unit2_save.return_value = "claimed_now"

    adapter = _MockSaveAdapter(
        current_sig="prepared_sig_001",
        next_headings=("Review and Submit", "Review and Submit"),
        next_sigs=("next_sig_002", "next_sig_002"),
    )
    coordinator = WorkdayUnit2SaveStepCoordinator(
        worker_api=mock_api, observation_delay_seconds=0.01
    )

    proof, outcome = await coordinator.execute_save_and_verify_checkpoint(
        lease=lease,
        prepared_form=prepared_form,
        adapter=adapter,
        account_continuity_check=lambda: True,
    )

    assert proof is None
    assert outcome == "unknown_page_state"
    assert (
        adapter.click_count == 1
    )  # Clicked once, but failed closed on review page without retry


class _MockResult:
    def __init__(self, items: list[Any]):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _MockDb:
    def __init__(self, app: JobApplication, attempt: WorkdayUnit2Attempt):
        self.app = app
        self.attempt = attempt
        self.events: list[ApplicationAutomationEvent] = []
        self.committed = False
        self.rolled_back = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, ApplicationAutomationEvent):
            self.events.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def execute(self, stmt: Any) -> _MockResult:
        stmt_str = str(stmt).lower()
        if "from job_applications" in stmt_str:
            return _MockResult([self.app])
        if "from workday_unit2_attempts" in stmt_str:
            return _MockResult([self.attempt])
        return _MockResult([])


@pytest.mark.asyncio
async def test_worker_claim_unit2_save_endpoint_atomic_behavior() -> None:
    from api.automation import (
        WorkerUnit2SaveClaimRequest,
        worker_claim_unit2_save,
    )
    from models.database import (
        ApplicationAutomationEvent,
        ApplicationStatus,
        JobApplication,
        WorkdayUnit2Attempt,
    )

    user_id = uuid.uuid4()
    app_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    now = datetime.now(UTC)

    app = JobApplication(
        id=app_id,
        user_id=user_id,
        portal="workday",
        status=ApplicationStatus.PREPARING.value,
        automation_lease_id=lease_id,
        automation_lease_expires_at=now + timedelta(minutes=15),
        automation_batch_id=uuid.uuid4(),
    )
    attempt = WorkdayUnit2Attempt(
        id=attempt_id,
        application_id=app_id,
        lease_id=lease_id,
        status="leased",
        mode="normal",
        save_claim_count=0,
        lease_expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )

    mock_db = _MockDb(app, attempt)
    req = WorkerUnit2SaveClaimRequest(attempt_id=attempt_id, lease_id=lease_id)

    # 1. First claim: changes 0 -> 1, returns claimed_now, records event
    res1 = await worker_claim_unit2_save(
        application_id=app_id,
        body=req,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )
    assert res1.status == "claimed_now"
    assert attempt.save_claim_count == 1
    assert attempt.status == "save_claimed"
    assert len(mock_db.events) == 1
    assert mock_db.events[0].event_type == "workday_unit2_save_claimed"
    assert mock_db.committed is True

    # 2. Second claim: returns already_claimed, leaves count at 1
    mock_db.committed = False
    res2 = await worker_claim_unit2_save(
        application_id=app_id,
        body=req,
        worker_user={"id": str(user_id)},
        db=mock_db,
    )
    assert res2.status == "already_claimed"
    assert attempt.save_claim_count == 1
    assert len(mock_db.events) == 1  # No duplicate event
    assert mock_db.rolled_back is True
