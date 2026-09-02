from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.workday_browser_runtime import (
    WorkdayWorkerError,
    user_scoped_autopilot_browser_profile,
)
from services.workday_unit2_checkpoint import (
    Unit2Observation,
    Unit2ResumeProof,
    WorkdayUnit2ResumeCoordinator,
    bind_safe_structural_signature,
    compute_safe_structural_signature,
)
from services.workday_worker_api import LeasedWorkdayUnit2Application


def test_unit2_resume_proof_validity() -> None:
    application_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    proof = Unit2ResumeProof(
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        approved_origin=True,
        canonical_tenant_verified=True,
        job_context_matches=True,
        application_context_matches=True,
        durable_account_continuity_verified=True,
        authenticated_state_verified=True,
        my_information_structure_verified=True,
        safe_signature="a1b2c3d4e5f6",
    )
    assert proof.is_valid is True

    # Any missing boolean makes proof invalid
    invalid_proof = Unit2ResumeProof(
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        approved_origin=True,
        canonical_tenant_verified=False,
        job_context_matches=True,
        application_context_matches=True,
        durable_account_continuity_verified=True,
        authenticated_state_verified=True,
        my_information_structure_verified=True,
        safe_signature="a1b2c3d4e5f6",
    )
    assert invalid_proof.is_valid is False

    with pytest.raises(ValueError):
        Unit2ResumeProof(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            approved_origin=True,
            canonical_tenant_verified=True,
            job_context_matches=True,
            application_context_matches=True,
            durable_account_continuity_verified=True,
            authenticated_state_verified=True,
            my_information_structure_verified=True,
            safe_signature="",
        )


def test_user_scoped_browser_profile_must_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        monkeypatch.setenv("LOCALAPPDATA", temp_dir)
        user_id = str(uuid.uuid4())

        # Profile does not exist yet -> raises WorkdayWorkerError
        with pytest.raises(WorkdayWorkerError, match="missing"):
            user_scoped_autopilot_browser_profile(user_id, must_exist=True)

        # When created, returns path normally
        profile_dir = user_scoped_autopilot_browser_profile(user_id, must_exist=False)
        profile_dir.mkdir(parents=True, exist_ok=True)
        assert user_scoped_autopilot_browser_profile(user_id, must_exist=True).exists()


class _MockUnit2PageAdapter:
    def __init__(
        self,
        observations: list[Unit2Observation],
        current_url_val: str = "https://wd1.myworkdaysite.com/job/R-1/apply",
    ):
        self._observations = list(observations)
        self._obs_index = 0
        self._url = current_url_val
        self.navigated_urls: list[str] = []
        self.continue_clicked_count = 0

    async def current_url(self) -> str:
        return self._url

    async def navigate_to_job(self, url: str) -> None:
        self.navigated_urls.append(url)
        self._url = url

    async def click_continue_or_apply(self) -> bool:
        self.continue_clicked_count += 1
        return True

    async def observe_structure(self, **kwargs: Any) -> Unit2Observation:
        del kwargs
        if self._obs_index < len(self._observations):
            obs = self._observations[self._obs_index]
            self._obs_index += 1
            return obs
        return self._observations[-1]


def _make_lease() -> LeasedWorkdayUnit2Application:
    from datetime import UTC, datetime, timedelta

    return LeasedWorkdayUnit2Application(
        application_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        mode="normal",
        user_id=uuid.uuid4(),
        portal="workday",
        job_url="https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Engineer_R-1",
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
    )


@pytest.mark.asyncio
async def test_coordinator_resume_success_already_restored_page() -> None:
    lease = _make_lease()
    sig = compute_safe_structural_signature(
        url_path="/en-US/recruiting/wf/WellsFargoJobs/job/Engineer_R-1/apply",
        heading_name="My Information",
        field_count=5,
        button_count=2,
    )
    obs = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=True,
        is_authenticated=True,
        has_my_info_heading=True,
        has_form_controls=True,
        is_expired_or_login=False,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature=sig,
    )
    adapter = _MockUnit2PageAdapter([obs, obs])
    coordinator = WorkdayUnit2ResumeCoordinator(observation_delay_seconds=0.01)

    proof, outcome = await coordinator.resume_and_verify(
        lease=lease,
        adapter=adapter,
        durable_account_continuity_verified=True,
    )

    assert outcome == "ready"
    assert proof is not None
    assert proof.is_valid is True
    assert proof.safe_signature == bind_safe_structural_signature(
        sig,
        application_id=lease.application_id,
        attempt_id=lease.attempt_id,
        lease_id=lease.lease_id,
    )
    assert adapter.continue_clicked_count == 0


@pytest.mark.asyncio
async def test_coordinator_resume_success_via_navigation_and_continue() -> None:
    lease = _make_lease()
    sig = compute_safe_structural_signature(
        url_path="/apply",
        heading_name="My Information",
        field_count=6,
        button_count=2,
    )
    obs_initial_job_page = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=False,
        is_authenticated=True,
        has_my_info_heading=False,
        has_form_controls=False,
        is_expired_or_login=False,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature="job_page_sig",
    )
    obs_draft = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=True,
        is_authenticated=True,
        has_my_info_heading=True,
        has_form_controls=True,
        is_expired_or_login=False,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature=sig,
    )
    adapter = _MockUnit2PageAdapter([obs_initial_job_page, obs_draft, obs_draft])
    coordinator = WorkdayUnit2ResumeCoordinator(observation_delay_seconds=0.01)

    proof, outcome = await coordinator.resume_and_verify(
        lease=lease,
        adapter=adapter,
        durable_account_continuity_verified=True,
    )

    assert outcome == "ready"
    assert proof is not None
    assert proof.is_valid is True
    assert adapter.continue_clicked_count == 1


@pytest.mark.asyncio
async def test_coordinator_resume_expired_session() -> None:
    lease = _make_lease()
    obs_login = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=False,
        is_authenticated=False,
        has_my_info_heading=False,
        has_form_controls=True,
        is_expired_or_login=True,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature="login_sig",
    )
    adapter = _MockUnit2PageAdapter([obs_login])
    coordinator = WorkdayUnit2ResumeCoordinator(observation_delay_seconds=0.01)

    proof, outcome = await coordinator.resume_and_verify(
        lease=lease,
        adapter=adapter,
        durable_account_continuity_verified=True,
    )

    assert proof is None
    assert outcome == "expired_session"


@pytest.mark.asyncio
async def test_coordinator_resume_typed_challenges() -> None:
    lease = _make_lease()
    coordinator = WorkdayUnit2ResumeCoordinator(observation_delay_seconds=0.01)

    challenges = [
        ({"has_captcha": True}, "captcha"),
        ({"has_otp": True}, "otp"),
        ({"has_email_verification": True}, "email_verification"),
        ({"is_locked": True}, "account_temporarily_locked"),
    ]

    for flags, expected_outcome in challenges:
        obs = Unit2Observation(
            approved_origin=True,
            canonical_tenant=True,
            job_context_matches=True,
            application_context_matches=True,
            is_authenticated=False,
            has_my_info_heading=False,
            has_form_controls=False,
            is_expired_or_login=False,
            has_captcha=flags.get("has_captcha", False),
            has_otp=flags.get("has_otp", False),
            has_email_verification=flags.get("has_email_verification", False),
            is_locked=flags.get("is_locked", False),
            has_validation_errors=False,
            safe_signature="challenge_sig",
        )
        adapter = _MockUnit2PageAdapter([obs])
        proof, outcome = await coordinator.resume_and_verify(
            lease=lease,
            adapter=adapter,
            durable_account_continuity_verified=True,
        )
        assert proof is None
        assert outcome == expected_outcome


@pytest.mark.asyncio
async def test_coordinator_resume_unstable_observation_fails_closed() -> None:
    lease = _make_lease()
    obs1 = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=True,
        is_authenticated=True,
        has_my_info_heading=True,
        has_form_controls=True,
        is_expired_or_login=False,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature="sig_1",
    )
    obs2 = Unit2Observation(
        approved_origin=True,
        canonical_tenant=True,
        job_context_matches=True,
        application_context_matches=True,
        is_authenticated=True,
        has_my_info_heading=True,
        has_form_controls=True,
        is_expired_or_login=False,
        has_captcha=False,
        has_otp=False,
        has_email_verification=False,
        is_locked=False,
        has_validation_errors=False,
        safe_signature="sig_2_mismatch",
    )
    adapter = _MockUnit2PageAdapter([obs1, obs2])
    coordinator = WorkdayUnit2ResumeCoordinator(observation_delay_seconds=0.01)

    proof, outcome = await coordinator.resume_and_verify(
        lease=lease,
        adapter=adapter,
        durable_account_continuity_verified=True,
    )
    assert proof is None
    assert outcome == "unknown_page_state"
