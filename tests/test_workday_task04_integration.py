from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from api.automation import CompleteWorkdayUnit1Request, worker_complete_unit1
from services.portal_account_automation import NativeAccountPageState
from services.workday_account_gate_store import WorkdayGateMutation
from services.workday_auth_broker import WorkdayAuthAccountBinding
from services.workday_page_condition import WorkdayUnit1PageCondition
from services.workday_transition_contracts import (
    WorkdayObservedState,
    WorkdayTransitionState,
)
from services.workday_unit1_checkpoint import (
    WorkdayPrivateCheckpointEvidence,
    WorkdayUnit1CheckpointVerifier,
)
from services.workday_unit1_orchestrator import (
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1Orchestrator,
    WorkdayUnit1Status,
)
from services.workday_worker_api import LeasedWorkdayApplication


APP_ID = UUID("51000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("51000000-0000-0000-0000-000000000002")
USER_ID = UUID("51000000-0000-0000-0000-000000000003")
GATE_ID = UUID("51000000-0000-0000-0000-000000000004")
ACCOUNT_REF = UUID("51000000-0000-0000-0000-000000000005")
SCOPE = "workday:wf:wellsfargojobs"
TARGET_URL = (
    "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
    "WellsFargoJobs/job/Engineer_R-1"
)


def _lease() -> LeasedWorkdayApplication:
    return LeasedWorkdayApplication(
        application_id=APP_ID,
        lease_id=LEASE_ID,
        user_id=USER_ID,
        portal="workday",
        job_url=TARGET_URL,
        external_ats_url=None,
        job_title="Engineer",
        company_name="Wells Fargo",
        gate_id=GATE_ID,
        gate_generation=1,
        gate_lease_token="opaque-gate-token",
        gate_decision="allow",
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


def _evidence(**overrides: object) -> WorkdayPrivateCheckpointEvidence:
    values = {
        "approved_https_origin": True,
        "canonical_tenant_verified": True,
        "leased_job_context_matches": True,
        "leased_application_context_matches": True,
        "external_account_matches": True,
        "no_login_or_auth_error": True,
        "no_captcha_or_otp_or_lock": True,
        "basic_information_control_hydrated": True,
        "safe_signature": "wdcp1:stable",
    }
    values.update(overrides)
    return WorkdayPrivateCheckpointEvidence(**values)


@dataclass
class _PrivateAdapter:
    observations: list[WorkdayPrivateCheckpointEvidence]

    async def capture_checkpoint_evidence(self, **kwargs):
        del kwargs
        return self.observations.pop(0)


@dataclass
class _Page:
    condition: WorkdayUnit1PageCondition

    async def open_approved_job(self, target_url):
        assert target_url == TARGET_URL

    async def capture_page_condition(self):
        return self.condition

    async def wait_for_hydration(self, milliseconds):
        assert milliseconds == 250


@dataclass
class _Observer:
    calls: int = 0

    async def observe(self, *, include_candidates=False):
        del include_candidates
        self.calls += 1
        return WorkdayObservedState(
            state=WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
            safe_signature="wds1:stable",
            signature_version=1,
            portal_family="workday",
            tenant_scope=SCOPE,
        )


@dataclass
class _Context:
    verifier: WorkdayUnit1CheckpointVerifier
    facts: list[WorkdayPrivateCheckpointEvidence]

    async def checkpoint_facts(self, *, lease, observation):
        del observation
        evidence = await self.verifier.verify(
            target_url=lease.to_workday_lease().target_url,
            expected_tenant_scope=SCOPE,
            application_id=lease.application_id,
            account_binding_verified=True,
            application_context_matches=True,
        )
        self.facts.append(evidence)
        return WorkdayUnit1CheckpointFacts(
            approved_https_origin=evidence.approved_https_origin,
            canonical_tenant_verified=evidence.canonical_tenant_verified,
            leased_job_context_matches=evidence.leased_job_context_matches,
            leased_application_context_matches=evidence.leased_application_context_matches,
            external_account_matches=evidence.external_account_matches,
            no_login_or_auth_error=evidence.no_login_or_auth_error,
            no_captcha_or_otp_or_lock=evidence.no_captcha_or_otp_or_lock,
            basic_information_control_hydrated=evidence.basic_information_control_hydrated,
            safe_signature=evidence.safe_signature,
        )


@dataclass
class _Persistence:
    routes: list[object] = field(default_factory=list)
    completions: list[bool] = field(default_factory=list)
    reviews: list[bool] = field(default_factory=list)

    async def apply_route(self, *, lease, route):
        del lease
        self.routes.append(route)

    async def complete_unit(self, *, lease, authentication_submitted):
        del lease
        self.completions.append(authentication_submitted)

    async def review_unit(self, *, lease, authentication_submitted):
        del lease
        self.reviews.append(authentication_submitted)


class _NoAuth:
    async def authenticate(self, request):
        raise AssertionError(f"authentication must not run: {request}")


def _run_with_evidence(evidence: list[WorkdayPrivateCheckpointEvidence]):
    adapter = _PrivateAdapter(evidence)
    context = _Context(WorkdayUnit1CheckpointVerifier(adapter), [])
    persistence = _Persistence()
    orchestrator = WorkdayUnit1Orchestrator(
        page=_Page(
            WorkdayUnit1PageCondition(
                native_state=NativeAccountPageState.AUTHENTICATED,
                already_authenticated=True,
            )
        ),
        observer=_Observer(),
        replay=SimpleNamespace(),
        repair=SimpleNamespace(),
        auth_broker=_NoAuth(),
        private_context=context,
        persistence=persistence,
        events=SimpleNamespace(emit=lambda event: None),
    )
    return orchestrator, persistence, context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fact",
    [
        "approved_https_origin",
        "canonical_tenant_verified",
        "leased_job_context_matches",
        "leased_application_context_matches",
        "external_account_matches",
        "no_login_or_auth_error",
        "no_captcha_or_otp_or_lock",
        "basic_information_control_hydrated",
    ],
)
async def test_each_private_checkpoint_fact_blocks_completion(fact):
    evidence = _evidence(**{fact: False})
    orchestrator, persistence, context = _run_with_evidence([evidence, evidence])

    result = await orchestrator.run(_lease())

    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.completions == []
    assert len(context.facts) == 1


@pytest.mark.asyncio
async def test_generic_registration_or_otp_control_is_not_basic_information():
    evidence = _evidence(
        basic_information_control_hydrated=False,
        no_captcha_or_otp_or_lock=False,
        safe_signature="wdcp1:registration-or-otp",
    )
    orchestrator, persistence, _ = _run_with_evidence([evidence])

    result = await orchestrator.run(_lease())

    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.completions == []


@pytest.mark.asyncio
async def test_checkpoint_requires_two_equal_private_observations():
    changed = _evidence(safe_signature="wdcp1:changed")
    orchestrator, persistence, context = _run_with_evidence([_evidence(), changed])

    result = await orchestrator.run(_lease())

    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.completions == []
    assert len(context.facts) == 2


@pytest.mark.asyncio
async def test_two_equal_private_observations_complete():
    evidence = _evidence()
    orchestrator, persistence, context = _run_with_evidence([evidence, evidence])

    result = await orchestrator.run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert persistence.completions == [False]
    assert len(context.facts) == 2


@pytest.mark.asyncio
async def test_existing_session_without_private_account_binding_reviews_without_submit():
    evidence = _evidence(external_account_matches=False)
    orchestrator, persistence, _ = _run_with_evidence([evidence])

    result = await orchestrator.run(_lease())

    assert result.status is WorkdayUnit1Status.REVIEW_REQUIRED
    assert persistence.completions == []
    assert persistence.reviews == []


@dataclass
class _DbResult:
    value: object | None

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, application):
        self.application = application
        self.execute_count = 0
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        del statement
        self.execute_count += 1
        return _DbResult(self.application if self.execute_count == 1 else None)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FinalizationStore:
    def __init__(self, applied=True):
        self.applied = applied
        self.complete_calls = []
        self.review_calls = []

    async def complete_success_in_transaction(self, lease, *, authentication_submitted):
        self.complete_calls.append((lease, authentication_submitted))
        return WorkdayGateMutation(applied=self.applied, stale=not self.applied)

    async def mark_review_required_in_transaction(
        self, lease, *, authentication_submitted
    ):
        self.review_calls.append((lease, authentication_submitted))
        return WorkdayGateMutation(applied=self.applied, stale=not self.applied)


def _application():
    return SimpleNamespace(
        id=APP_ID,
        user_id=USER_ID,
        deleted_at=None,
        automation_lease_id=LEASE_ID,
        automation_lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        workday_account_gate_id=GATE_ID,
        status="preparing",
        automation_batch_id=uuid4(),
        portal="workday",
    )


def _request(*, outcome="complete", submitted=True):
    return CompleteWorkdayUnit1Request(
        lease_id=LEASE_ID,
        gate_id=GATE_ID,
        gate_generation=1,
        gate_lease_token="opaque-gate-token",
        authentication_submitted=submitted,
        outcome=outcome,
    )


@pytest.mark.asyncio
async def test_guarded_server_finalization_completes_gate_and_application_atomically(
    monkeypatch,
):
    application = _application()
    database = _Db(application)
    store = _FinalizationStore()
    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore", lambda db: store
    )

    result = await worker_complete_unit1(
        application_id=APP_ID,
        body=_request(),
        worker_user={"id": str(USER_ID)},
        db=database,
    )

    assert result["status"] == "applying"
    assert application.automation_lease_id is None
    assert application.automation_lease_expires_at is None
    assert database.commits == 1
    assert store.complete_calls[0][1] is True


@pytest.mark.asyncio
async def test_failed_submitted_proof_reviews_gate_and_blocks_only_application(
    monkeypatch,
):
    application = _application()
    database = _Db(application)
    store = _FinalizationStore()
    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore", lambda db: store
    )

    result = await worker_complete_unit1(
        application_id=APP_ID,
        body=_request(outcome="review"),
        worker_user={"id": str(USER_ID)},
        db=database,
    )

    assert result["status"] == "blocked"
    assert application.automation_lease_id is None
    assert database.commits == 1
    assert store.review_calls[0][1] is True
    assert any(
        getattr(item, "hold_code", None) == "unknown_page_state"
        for item in database.added
    )


@pytest.mark.asyncio
async def test_stale_finalization_cannot_change_application_status(monkeypatch):
    application = _application()
    database = _Db(application)
    store = _FinalizationStore(applied=False)
    monkeypatch.setattr(
        "api.automation.SQLAlchemyWorkdayAccountGateStore", lambda db: store
    )

    with pytest.raises(HTTPException) as exc_info:
        await worker_complete_unit1(
            application_id=APP_ID,
            body=_request(),
            worker_user={"id": str(USER_ID)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert application.status == "preparing"
    assert application.automation_lease_id == LEASE_ID
    assert database.commits == 0


def test_private_account_binding_is_not_in_shared_result_repr():
    binding = WorkdayAuthAccountBinding(ACCOUNT_REF)
    assert str(ACCOUNT_REF) not in repr(binding)
