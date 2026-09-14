from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from services.local_workday_runner import (
    LocalWorkdayRunStatus,
    LocalWorkdayRunner,
)
from services.workday_auth_broker import WorkdayPostSubmitObservation
from services.workday_failure_router import (
    WorkdayFailureOutcome,
    WorkdayFailureRoute,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdayTransitionState,
)
from services.workday_unit1_orchestrator import (
    WorkdayExecutionPhase,
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1ExecutionError,
    WorkdayUnit1Orchestrator,
    WorkdayUnit1Status,
)
from services.workday_unit1_runtime import ProductionWorkdayUnit1Executor
from services.workday_worker_api import LeasedWorkdayApplication


APP_ID = UUID("61000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("61000000-0000-0000-0000-000000000002")
USER_ID = UUID("61000000-0000-0000-0000-000000000003")
GATE_ID = UUID("61000000-0000-0000-0000-000000000004")
SCOPE = "workday:wf:wellsfargojobs"


def _lease(decision: str = "observe_only") -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=APP_ID,
        lease_id=LEASE_ID,
        user_id=USER_ID,
        portal="workday",
        job_url=(
            "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=GATE_ID,
        gate_generation=4,
        gate_lease_token="opaque-gate-token",
        gate_decision=decision,
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


def _ready() -> WorkdayObservedState:
    return WorkdayObservedState(
        state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        safe_signature="wds1:basic-information",
        signature_version=1,
        portal_family="workday",
        tenant_scope=SCOPE,
    )


def _facts() -> WorkdayUnit1CheckpointFacts:
    return WorkdayUnit1CheckpointFacts(
        approved_https_origin=True,
        canonical_tenant_verified=True,
        leased_job_context_matches=True,
        leased_application_context_matches=True,
        external_account_matches=True,
        no_login_or_auth_error=True,
        no_captcha_or_otp_or_lock=True,
        basic_information_control_hydrated=True,
        safe_signature="wdcp1:basic-information",
    )


@dataclass
class _ObservePage:
    observation: WorkdayPostSubmitObservation | Exception
    post_submit_calls: int = 0
    navigation_calls: int = 0
    mutation_calls: int = 0

    async def observe_post_submit(self) -> WorkdayPostSubmitObservation:
        self.post_submit_calls += 1
        if isinstance(self.observation, Exception):
            raise self.observation
        return self.observation

    async def open_approved_job(self, target_url: str) -> None:
        del target_url
        self.navigation_calls += 1

    async def click(self) -> None:
        self.mutation_calls += 1


@dataclass
class _Observer:
    states: list[WorkdayObservedState] = field(
        default_factory=lambda: [_ready(), _ready()]
    )
    calls: int = 0

    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState:
        assert include_candidates is False
        self.calls += 1
        return self.states[min(self.calls - 1, len(self.states) - 1)]


@dataclass
class _PrivateContext:
    facts: WorkdayUnit1CheckpointFacts = field(default_factory=_facts)
    checkpoint_calls: int = 0

    async def checkpoint_facts(self, *, lease, observation):
        del lease, observation
        self.checkpoint_calls += 1
        return self.facts


@dataclass
class _Persistence:
    recovered: list[WorkdayFailureRoute] = field(default_factory=list)
    completions: list[bool] = field(default_factory=list)
    reviews: list[bool] = field(default_factory=list)
    retries: int = 0

    async def recover_observe_only(self, *, lease, route, trusted_portal_until=None):
        del lease, trusted_portal_until
        self.recovered.append(route)
        if route.outcome is WorkdayFailureOutcome.REVIEW_REQUIRED:
            self.reviews.append(True)

    async def complete_unit(self, *, lease, authentication_submitted):
        del lease
        self.completions.append(authentication_submitted)

    async def review_unit(self, *, lease, authentication_submitted):
        del lease
        self.reviews.append(authentication_submitted)

    async def apply_route(self, *, lease, route):
        del lease, route
        self.retries += 1


def _orchestrator(page, observer, private, persistence):
    return WorkdayUnit1Orchestrator(
        page=page,
        observer=observer,
        replay=object(),
        repair=object(),
        auth_broker=object(),
        private_context=private,
        persistence=persistence,
        events=object(),
    )


@pytest.mark.asyncio
async def test_observe_only_verified_success_uses_only_read_only_checkpoint():
    page = _ObservePage(
        WorkdayPostSubmitObservation(
            frozenset({WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS})
        )
    )
    observer = _Observer()
    private = _PrivateContext()
    persistence = _Persistence()

    result = await _orchestrator(page, observer, private, persistence).run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert persistence.completions == [True]
    assert persistence.recovered == []
    assert page.post_submit_calls == 1
    assert page.navigation_calls == page.mutation_calls == 0
    assert observer.calls == private.checkpoint_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation",
    [
        WorkdayPostSubmitObservation(
            frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN})
        ),
        RuntimeError("missing page"),
    ],
)
async def test_observe_only_unknown_or_missing_page_requires_review(observation):
    persistence = _Persistence()
    result = await _orchestrator(
        _ObservePage(observation), _Observer(), _PrivateContext(), persistence
    ).run(_lease())

    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.reviews == [True]
    assert persistence.retries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_class", "outcome"),
    [
        (
            WorkdayFailureClass.POST_SUBMIT_ACCOUNT_LOCKED,
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_CAPTCHA_OR_OTP,
            WorkdayFailureOutcome.USER_HOLD,
        ),
        (
            WorkdayFailureClass.POST_SUBMIT_AUTH_REJECTED,
            WorkdayFailureOutcome.CREDENTIAL_HOLD,
        ),
    ],
)
async def test_observe_only_routes_lock_challenge_and_rejection_once(
    failure_class, outcome
):
    persistence = _Persistence()
    result = await _orchestrator(
        _ObservePage(WorkdayPostSubmitObservation(frozenset({failure_class}))),
        _Observer(),
        _PrivateContext(),
        persistence,
    ).run(_lease())

    assert result.route.outcome is outcome
    assert persistence.recovered == [result.route]
    assert persistence.retries == 0


@pytest.mark.asyncio
async def test_repeated_observe_only_requests_do_not_become_retry_loop():
    persistence = _Persistence()
    orchestrator = _orchestrator(
        _ObservePage(
            WorkdayPostSubmitObservation(
                frozenset({WorkdayFailureClass.POST_SUBMIT_OTHER_OR_UNKNOWN})
            )
        ),
        _Observer(),
        _PrivateContext(),
        persistence,
    )

    first = await orchestrator.run(_lease())
    second = await orchestrator.run(_lease())

    assert first.status is second.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.retries == 0
    assert persistence.reviews == [True, True]


@dataclass
class _RunnerApi:
    startup_releases: list[LeasedWorkdayApplication] = field(default_factory=list)
    reviews: list[tuple[LeasedWorkdayApplication, bool]] = field(default_factory=list)

    async def lease_next(self, *, application_id=None):
        del application_id
        return self.lease

    async def release_startup_lease(self, lease):
        self.startup_releases.append(lease)

    async def record_unit1_review(
        self, lease, *, authentication_submitted, hold_code="unknown_page_state"
    ):
        del hold_code
        self.reviews.append((lease, authentication_submitted))


class _FailingRuntime:
    async def __aenter__(self):
        raise RuntimeError("browser startup failed")

    async def __aexit__(self, *args):
        del args


class _UnusedCoordinator:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "one_probe"])
async def test_allow_and_probe_startup_failures_release_both_authorities(decision):
    api = _RunnerApi()
    api.lease = _lease(decision)
    executor = ProductionWorkdayUnit1Executor(
        api=api,
        credential_reader=object(),
        runtime_factory=lambda lease: _FailingRuntime(),
        resolver=object(),
        history_limit=3,
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_UnusedCoordinator(),
        unit1_orchestrator=executor,
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.RETRYING
    assert api.startup_releases == [api.lease]
    assert api.reviews == []


@pytest.mark.asyncio
async def test_observe_only_startup_failure_reviews_without_releasing_attempt():
    api = _RunnerApi()
    api.lease = _lease()
    executor = ProductionWorkdayUnit1Executor(
        api=api,
        credential_reader=object(),
        runtime_factory=lambda lease: _FailingRuntime(),
        resolver=object(),
        history_limit=3,
    )

    result = await LocalWorkdayRunner(
        api=api,
        coordinator=_UnusedCoordinator(),
        unit1_orchestrator=executor,
    ).run_once()

    assert result.status is LocalWorkdayRunStatus.REVIEW_REQUIRED
    assert api.startup_releases == []
    assert api.reviews == [(api.lease, True)]


def test_execution_phases_do_not_conflate_submit_with_startup():
    startup = WorkdayUnit1ExecutionError(
        "startup", phase=WorkdayExecutionPhase.BEFORE_BROWSER_ACTION
    )
    pending = WorkdayUnit1ExecutionError(
        "pending", phase=WorkdayExecutionPhase.SUBMIT_CLAIMED_PENDING
    )
    assert startup.phase is not pending.phase
