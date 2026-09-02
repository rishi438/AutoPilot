from __future__ import annotations

from dataclasses import dataclass, field
import uuid

import pytest

from services.local_workday_runner import (
    LocalWorkdayRunner,
    LocalWorkdayRunStatus,
)
from services.portal_account_automation import (
    NativeAccountAction,
    NativeAccountPageState,
    NativeAccountPlan,
)
from services.workday_playwright_worker import (
    WorkdayFormAssignment,
    WorkdayFormField,
    WorkdayFormFillResult,
    WorkdayWorkerError,
)
from services.workday_worker_api import LeasedWorkdayApplication
from services.workday_unit1_orchestrator import (
    WorkdayUnit1Result,
    WorkdayUnit1Status,
)
from services.workday_transition_contracts import WorkdayTransitionState


def _lease() -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        lease_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        portal="workday",
        job_url=(
            "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        gate_generation=1,
        gate_lease_token="opaque-gate-token",
        gate_decision="allow",
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


@dataclass
class _FakeEmitter:
    states: list[NativeAccountPageState] = field(default_factory=list)

    async def emit(self, application_id, page_state) -> None:
        del application_id
        self.states.append(page_state)


class _FakeApi:
    def __init__(self, lease, *, assignments=None):
        self.lease = lease
        self.lease_requests = []
        self.emitter = _FakeEmitter()
        self.holds = []
        self.retries = []
        self.skips = []
        self.startup_releases = []
        self.assignments = (
            [
                WorkdayFormAssignment(
                    field_uid="0", value="Candidate", answer_source="profile"
                )
            ]
            if assignments is None
            else assignments
        )

    async def lease_next(self, *, application_id=None):
        self.lease_requests.append(application_id)
        return self.lease

    def state_emitter(self, lease):
        assert lease is self.lease
        return self.emitter

    async def create_hold(self, lease, **kwargs) -> None:
        self.holds.append((lease, kwargs))

    async def record_retry(self, lease, *, safe_reason) -> None:
        self.retries.append((lease, safe_reason))

    async def record_skip(self, lease) -> None:
        self.skips.append(lease)

    async def release_startup_lease(self, lease) -> None:
        self.startup_releases.append(lease)

    async def map_approved_form_fields(self, lease, *, page_url, fields):
        assert lease is self.lease
        assert page_url.startswith("https://")
        assert len(fields) == 1
        return self.assignments


class _FakeCoordinator:
    def __init__(self, plan: NativeAccountPlan):
        self.plan = plan

    async def plan_workday_action(self, **kwargs):
        del kwargs
        return self.plan


class _FakeBrowser:
    def __init__(self, state: NativeAccountPageState):
        self.state = state
        self.opened = False
        self.next_steps = 0

    async def open_and_start_apply(self, target_url) -> None:
        assert "myworkdaysite.com" in target_url
        self.opened = True

    async def detect_account_state(self):
        return self.state

    async def scan_application_fields(self):
        return (
            "https://wd1.myworkdaysite.com/recruiting/wf/site/job/R-1/apply",
            [
                WorkdayFormField(
                    field_uid="0",
                    tag="input",
                    input_type="text",
                    name_attr="name",
                    id_attr="name",
                    label_text="Name",
                    placeholder=None,
                    aria_label=None,
                    required=True,
                    readonly=False,
                    disabled=False,
                    current_value="",
                    max_length=100,
                )
            ],
        )

    async def fill_and_verify_application_fields(self, assignments):
        if assignments:
            assert assignments[0].value == "Candidate"
        return WorkdayFormFillResult(
            filled_count=len(assignments), verified_count=len(assignments)
        )

    async def advance_to_next_application_step(self) -> None:
        self.next_steps += 1


class _FailingBrowser(_FakeBrowser):
    async def open_and_start_apply(self, target_url) -> None:
        del target_url
        raise WorkdayWorkerError(
            "No safe Apply selection.",
            safe_code="workday_apply_action_not_selected",
        )


class _SecondStepValidationFailureBrowser(_FakeBrowser):
    def __init__(self, state: NativeAccountPageState):
        super().__init__(state)
        self.fill_calls = 0

    async def fill_and_verify_application_fields(self, assignments):
        self.fill_calls += 1
        if self.fill_calls == 2:
            return WorkdayFormFillResult(
                filled_count=1,
                verified_count=0,
                failed_field_uids=("0",),
            )
        return await super().fill_and_verify_application_fields(assignments)


class _FakeRuntime:
    def __init__(self, browser: _FakeBrowser):
        self.browser = browser
        self.closed = False

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, *args) -> None:
        del args
        self.closed = True


class _FailingStartupRuntime:
    async def __aenter__(self):
        raise RuntimeError("browser unavailable")

    async def __aexit__(self, *args) -> None:
        del args


@dataclass
class _FakeUnit1Orchestrator:
    result: WorkdayUnit1Result
    leases: list[LeasedWorkdayApplication] = field(default_factory=list)

    async def run(self, lease: LeasedWorkdayApplication) -> WorkdayUnit1Result:
        self.leases.append(lease)
        return self.result


@pytest.mark.asyncio
async def test_runner_delegates_eligible_lease_to_unit1_orchestrator() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    orchestrator = _FakeUnit1Orchestrator(
        WorkdayUnit1Result(
            WorkdayUnit1Status.COMPLETE,
            final_state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            stable_observations=2,
        )
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_FakeCoordinator(
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            )
        ),
        unit1_orchestrator=orchestrator,
    ).run_once(application_id=lease.application_id)

    assert result.status is LocalWorkdayRunStatus.UNIT1_COMPLETE
    assert orchestrator.leases == [lease]
    assert api.lease_requests == [lease.application_id]


@pytest.mark.asyncio
async def test_runner_defers_ready_account_without_submitting() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    browser = _FakeBrowser(NativeAccountPageState.AUTHENTICATED)
    runtime = _FakeRuntime(browser)
    coordinator = _FakeCoordinator(
        NativeAccountPlan(
            NativeAccountAction.CONTINUE_APPLICATION,
            "workday:wf:wellsfargojobs",
        )
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=coordinator,
        browser_runtime=runtime,
    ).run_once(application_id=lease.application_id)

    assert result.status is LocalWorkdayRunStatus.FORM_STEP_FILLED_DEFERRED
    assert api.lease_requests == [lease.application_id]
    assert api.retries == [
        (lease, "workday_second_form_step_verified_navigation_pending")
    ]
    assert api.holds == []
    assert browser.opened is True
    assert browser.next_steps == 1
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_runner_creates_safe_hold_for_captcha() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    browser = _FakeBrowser(NativeAccountPageState.CAPTCHA)
    coordinator = _FakeCoordinator(
        NativeAccountPlan(
            NativeAccountAction.CREATE_HOLD,
            "workday:wf:wellsfargojobs",
            hold_code="captcha",
        )
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=coordinator,
        browser_runtime=_FakeRuntime(browser),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.HELD
    assert result.hold_code == "captcha"
    assert api.holds[0][1] == {
        "hold_code": "captcha",
        "remediation": (
            "The agent paused this job because CAPTCHA cannot be automated safely; "
            "continue processing other eligible jobs."
        ),
    }
    assert api.retries == []


@pytest.mark.asyncio
async def test_runner_preserves_first_missing_required_question_in_hold() -> None:
    lease = _lease()
    api = _FakeApi(lease, assignments=[])
    browser = _FakeBrowser(NativeAccountPageState.AUTHENTICATED)

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_FakeCoordinator(
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            )
        ),
        browser_runtime=_FakeRuntime(browser),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.HELD
    assert result.hold_code == "unknown_required_question"
    assert api.holds[0][1] == {
        "hold_code": "unknown_required_question",
        "remediation": (
            "Add and approve the missing required answer in AutoPilot before retrying."
        ),
        "question": "Name",
    }


@pytest.mark.asyncio
async def test_runner_preserves_failed_second_step_field_label() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    browser = _SecondStepValidationFailureBrowser(NativeAccountPageState.AUTHENTICATED)

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_FakeCoordinator(
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            )
        ),
        browser_runtime=_FakeRuntime(browser),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.HELD
    assert result.hold_code == "validation_failure"
    assert browser.next_steps == 1
    assert api.holds[0][1] == {
        "hold_code": "validation_failure",
        "remediation": (
            "A filled value did not remain committed; review the form before retrying."
        ),
        "question": "Name",
    }


@pytest.mark.asyncio
async def test_runner_terminally_skips_unavailable_job() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    browser = _FakeBrowser(NativeAccountPageState.JOB_UNAVAILABLE)
    coordinator = _FakeCoordinator(
        NativeAccountPlan(
            NativeAccountAction.CREATE_HOLD,
            "workday:wf:wellsfargojobs",
        )
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=coordinator,
        browser_runtime=_FakeRuntime(browser),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.SKIPPED
    assert api.skips == [lease]
    assert api.retries == []
    assert api.holds == []


@pytest.mark.asyncio
async def test_runner_records_safe_browser_stage_failure() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    browser = _FailingBrowser(NativeAccountPageState.UNKNOWN)
    coordinator = _FakeCoordinator(
        NativeAccountPlan(
            NativeAccountAction.CREATE_HOLD,
            "workday:wf:wellsfargojobs",
        )
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=coordinator,
        browser_runtime=_FakeRuntime(browser),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.RETRYING
    assert result.safe_reason == "workday_apply_action_not_selected"
    assert api.retries == [(lease, "workday_apply_action_not_selected")]


@pytest.mark.asyncio
async def test_runner_releases_both_leases_when_browser_startup_fails() -> None:
    lease = _lease()
    api = _FakeApi(lease)

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_FakeCoordinator(
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            )
        ),
        browser_runtime=_FailingStartupRuntime(),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.RETRYING
    assert result.safe_reason == "workday_browser_startup_failed"
    assert api.startup_releases == [lease]
    assert api.retries == []


@pytest.mark.asyncio
async def test_runner_builds_browser_runtime_from_leased_user() -> None:
    lease = _lease()
    api = _FakeApi(lease)
    runtime = _FakeRuntime(_FakeBrowser(NativeAccountPageState.AUTHENTICATED))
    received_leases = []

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_FakeCoordinator(
            NativeAccountPlan(
                NativeAccountAction.CONTINUE_APPLICATION,
                "workday:wf:wellsfargojobs",
            )
        ),
        browser_runtime_factory=lambda received: (
            received_leases.append(received) or runtime
        ),
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.FORM_STEP_FILLED_DEFERRED
    assert received_leases == [lease]
    assert runtime.closed is True
