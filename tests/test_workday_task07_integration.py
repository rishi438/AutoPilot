from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException

from api.automation import worker_lease_next_unit1_application
from services.local_workday_runner import LocalWorkdayRunStatus, LocalWorkdayRunner
from services.portal_account_automation import NativeAccountPageState
from services.portal_credentials import WorkerPortalCredential
from services.portal_control_resolver import PortalControlIntent
from services.workday_account_gate_store import WorkdayGateMutation
from services.workday_auth_broker import (
    WorkdayAuthAccountBinding,
    WorkdayAuthBrokerRequest,
    WorkdayPostSubmitObservation,
)
from services.workday_failure_router import WorkdayFailureOutcome
from services.workday_state_observer import (
    WorkdayControlScope,
    WorkdayPageStructure,
    WorkdaySemanticControl,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)
from services.workday_unit1_orchestrator import (
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1Status,
)
from services.workday_unit1_runtime import ProductionWorkdayUnit1Executor, _Persistence
from services.workday_worker_api import LeasedWorkdayApplication, WorkdayWorkerApi
from scripts.run_workday_account_gate import run_once


APP_ID = UUID("71000000-0000-0000-0000-000000000001")
LEASE_ID = UUID("71000000-0000-0000-0000-000000000002")
USER_ID = UUID("71000000-0000-0000-0000-000000000003")
GATE_ID = UUID("71000000-0000-0000-0000-000000000004")
ACCOUNT_REF = UUID("71000000-0000-0000-0000-000000000005")
SCOPE = "workday:wf:wellsfargojobs"
TARGET_URL = (
    "https://wd1.myworkdaysite.com/en-US/recruiting/wf/"
    "WellsFargoJobs/job/Engineer_R-1"
)


def _lease(*, decision: str = "allow") -> LeasedWorkdayApplication:
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
        gate_decision=decision,
        gate_lease_expires_at=None,
        gate_next_eligible_at=None,
    )


@dataclass
class _Page:
    stage: int = 0
    actions: list[PortalControlIntent] = field(default_factory=list)
    auth_fills: int = 0
    auth_submits: int = 0

    async def open_approved_job(self, target_url):
        assert target_url == TARGET_URL

    async def capture_page_condition(self):
        from services.workday_page_condition import WorkdayUnit1PageCondition

        return WorkdayUnit1PageCondition(native_state=NativeAccountPageState.UNKNOWN)

    async def capture_structure(self, *, limit):
        del limit
        controls = {
            0: (WorkdaySemanticControl(role="button", semantic_name="Apply"),),
            1: (WorkdaySemanticControl(role="button", semantic_name="Apply Manually"),),
            2: (WorkdaySemanticControl(role="button", semantic_name="Sign In"),),
            3: (
                WorkdaySemanticControl(
                    role="textbox", input_type="email", semantic_name="Email"
                ),
                WorkdaySemanticControl(
                    role="textbox", input_type="password", semantic_name="Password"
                ),
                WorkdaySemanticControl(role="button", semantic_name="Sign In"),
            ),
            4: (WorkdaySemanticControl(role="textbox", semantic_name="First name"),),
        }
        return WorkdayPageStructure(url=TARGET_URL, controls=controls[self.stage])

    async def execute_candidate(self, *, candidate_id, action_intent):
        assert candidate_id.startswith("wdc-")
        self.actions.append(action_intent)
        self.stage += 1

    async def verify_unique_auth_controls(self, *, expected_scope):
        assert expected_scope == SCOPE
        return True

    async def fill_verified_auth_controls(self, credential):
        assert credential.portal_scope == SCOPE
        self.auth_fills += 1

    async def click_verified_sign_in(self):
        self.auth_submits += 1
        self.stage = 4

    async def observe_post_submit(self):
        return WorkdayPostSubmitObservation(
            frozenset({WorkdayFailureClass.POST_SUBMIT_VERIFIED_SUCCESS})
        )

    async def wait_for_hydration(self, milliseconds):
        assert milliseconds == 250


class _Runtime:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        return self.page

    async def __aexit__(self, *args):
        del args


class _Catalog:
    def __init__(self):
        self.recipes = {
            PortalControlIntent.APPLY: (WorkdayTransitionState.APPLY_CHOICES, "apply"),
            PortalControlIntent.APPLY_MANUALLY: (
                WorkdayTransitionState.ACCOUNT_PAGE,
                "apply_manually",
            ),
            PortalControlIntent.SIGN_IN: (
                WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
                "sign_in",
            ),
        }

    async def get_current_and_history(self, key, limit):
        del limit
        expected_state, intent_key = self.recipes[key.action_intent]
        return (
            WorkdayTransitionRecipe(
                version_id=uuid4(),
                family_id=GATE_ID,
                parent_version_id=None,
                safe_locator_strategy={
                    "schema_version": 1,
                    "semantic_role": "button",
                    "intent_key": intent_key,
                    "scope_key": "page",
                    "require_unique": True,
                },
                expected_to_state=expected_state,
                risk=(
                    WorkdayTransitionRisk.AUTH_STRUCTURE
                    if key.action_intent is PortalControlIntent.SIGN_IN
                    else WorkdayTransitionRisk.NAVIGATION_ONLY
                ),
                status=WorkdayTransitionStatus.VERIFIED,
                recipe_version=1,
                signature_version=1,
                executor_policy_version=1,
            ),
        )

    async def ensure_family(self, key):
        del key
        return GATE_ID

    async def rollback_current(self, **kwargs):
        del kwargs
        return False


class _Gate:
    def __init__(self):
        self.submit_claims = 0

    async def verify_auth_lease(self, lease, binding):
        assert lease.gate_id == GATE_ID
        assert binding.account_ref == ACCOUNT_REF
        return True

    async def mark_secret_accessed(self, lease):
        del lease
        return WorkdayGateMutation(applied=True)

    async def claim_auth_submit(self, lease):
        del lease
        self.submit_claims += 1
        return WorkdayGateMutation(applied=True)

    async def claim_llm_repair(self, lease):
        del lease
        return WorkdayGateMutation(applied=False, stale=True)


class _Credentials:
    async def credential_for_auth_broker(self, *, user_id, account_ref, portal_scope):
        assert user_id == str(USER_ID)
        assert account_ref == ACCOUNT_REF
        assert portal_scope == SCOPE
        return WorkerPortalCredential(
            credential_id="synthetic-credential",
            portal_scope=SCOPE,
            account_email="candidate@example.com",
            status="active",
            password="synthetic-placeholder",
        )


class _PrivateContext:
    def __init__(self, verifier, session_factory):
        self.verifier = verifier
        self.session_factory = session_factory
        self.accepted = 0

    async def auth_request(self, *, lease, portal_scope):
        return WorkdayAuthBrokerRequest(
            user_id=lease.user_id,
            account_ref=ACCOUNT_REF,
            portal_scope=portal_scope,
            gate_lease=lease_to_gate(lease),
        )

    async def accept_auth_binding(self, binding: WorkdayAuthAccountBinding):
        assert binding.account_ref == ACCOUNT_REF
        self.accepted += 1

    async def checkpoint_facts(self, *, lease, observation):
        del lease, observation
        return WorkdayUnit1CheckpointFacts(
            approved_https_origin=True,
            canonical_tenant_verified=True,
            leased_job_context_matches=True,
            leased_application_context_matches=True,
            external_account_matches=True,
            no_login_or_auth_error=True,
            no_captcha_or_otp_or_lock=True,
            basic_information_control_hydrated=True,
            safe_signature="wdcp1:synthetic-basic-information",
        )


class _Api:
    def __init__(self):
        self.completions: list[bool] = []

    async def record_unit1_complete(self, lease, *, authentication_submitted):
        del lease
        self.completions.append(authentication_submitted)


def lease_to_gate(lease):
    from services.workday_account_gate_store import WorkdayGateLease

    return WorkdayGateLease(
        gate_id=lease.gate_id,
        application_id=lease.application_id,
        generation=lease.gate_generation,
        lease_token=lease.gate_lease_token,
    )


@pytest.mark.asyncio
async def test_unit1_lease_route_requires_application_scope_dependency():
    dependency = (
        signature(worker_lease_next_unit1_application)
        .parameters["worker_user"]
        .default.dependency
    )

    from utils.worker_auth import get_workday_application_worker_user

    assert dependency is get_workday_application_worker_user


@pytest.mark.asyncio
async def test_runner_requests_unit1_lease_when_executor_is_configured():
    lease = _lease()
    calls = []

    class Api:
        async def lease_next(self, *, application_id=None):
            calls.append(("legacy", application_id))
            return lease

        async def lease_next_unit1(self, *, application_id=None):
            calls.append(("unit1", application_id))
            return lease

    class Executor:
        async def run(self, received):
            assert received is lease
            from services.workday_unit1_orchestrator import WorkdayUnit1Result

            return WorkdayUnit1Result(WorkdayUnit1Status.COMPLETE)

    result = await LocalWorkdayRunner(
        api=Api(), coordinator=object(), unit1_orchestrator=Executor()
    ).run_once(application_id=APP_ID)

    assert result.status is LocalWorkdayRunStatus.UNIT1_COMPLETE
    assert calls == [("unit1", APP_ID)]


@pytest.mark.asyncio
async def test_cli_rejects_lower_scope_before_vault_or_resolver(monkeypatch):
    calls = []

    class Api:
        def __init__(self, **kwargs):
            del kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def lease_next_unit1(self, *, application_id=None):
            del application_id
            raise HTTPException(401, "Invalid worker scope")

    settings = SimpleNamespace(
        local_llm_model="model-a",
        local_llm_models=["model-a"],
        workday_transition_history_limit=10,
        workday_account_lock_cooldown_hours=6,
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.get_settings", lambda: settings
    )
    monkeypatch.setattr("scripts.run_workday_account_gate.WorkdayWorkerApi", Api)
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.LocalLLMPortalControlResolver",
        lambda *args, **kwargs: calls.append("resolver") or object(),
    )

    async def forbidden_vault(**kwargs):
        del kwargs
        calls.append("vault")
        raise AssertionError("vault must not open before scope rejection")

    monkeypatch.setattr(
        "scripts.run_workday_account_gate._open_vault_repository", forbidden_vault
    )
    args = SimpleNamespace(
        local_model="model-a",
        headless=False,
        accept_account_terms=False,
        log_control_decisions=False,
        api_url="http://127.0.0.1:8000",
        application_id=None,
        vault_port=27118,
    )

    with pytest.raises(HTTPException) as exc_info:
        await run_once(args, "synthetic-worker-token")

    assert exc_info.value.status_code == 401
    assert calls == []


@pytest.mark.asyncio
async def test_real_executor_composition_completes_known_path_with_one_submit():
    page = _Page()
    api = _Api()
    gate = _Gate()
    catalog = _Catalog()
    session_factory = lambda: None
    captured = {}

    def catalog_factory(received_session_factory):
        captured["catalog_session_factory"] = received_session_factory
        return catalog

    def gate_factory(received_session_factory, cooldown_hours):
        captured["gate_session_factory"] = received_session_factory
        captured["cooldown_hours"] = cooldown_hours
        return gate

    def checkpoint_factory(browser):
        captured["checkpoint_browser"] = browser
        return object()

    def private_factory(verifier, received_session_factory):
        captured["private_verifier"] = verifier
        captured["private_session_factory"] = received_session_factory
        return _PrivateContext(verifier, received_session_factory)

    executor = ProductionWorkdayUnit1Executor(
        api=api,
        credential_reader=_Credentials(),
        runtime_factory=lambda lease: _Runtime(page),
        resolver=SimpleNamespace(
            select_repair=lambda **kwargs: pytest.fail("LLM repair was invoked")
        ),
        history_limit=3,
        lock_cooldown_hours=12,
        session_factory=session_factory,
        catalog_factory=catalog_factory,
        gate_store_factory=gate_factory,
        checkpoint_verifier_factory=checkpoint_factory,
        private_context_factory=private_factory,
        persistence_factory=lambda received_api, received_gate: _Persistence(
            api=received_api, gate_store=received_gate
        ),
    )

    result = await executor.run(_lease())

    assert result.status is WorkdayUnit1Status.COMPLETE
    assert page.actions == [
        PortalControlIntent.APPLY,
        PortalControlIntent.APPLY_MANUALLY,
        PortalControlIntent.SIGN_IN,
    ]
    assert page.auth_fills == page.auth_submits == gate.submit_claims == 1
    assert api.completions == [True]
    assert captured["catalog_session_factory"] is session_factory
    assert captured["gate_session_factory"] is session_factory
    assert captured["private_session_factory"] is session_factory
    assert captured["cooldown_hours"] == 12


@pytest.mark.asyncio
async def test_production_private_context_observe_only_derives_account_binding() -> (
    None
):
    from services.workday_unit1_runtime import _PrivateContext as ProdPrivateContext
    from services.workday_unit1_checkpoint import WorkdayPrivateCheckpointEvidence

    lease = _lease(decision="observe_only")

    captured_verify = {}

    class _MockVerifier:
        async def verify(self, **kwargs):
            captured_verify.update(kwargs)
            return WorkdayPrivateCheckpointEvidence(
                approved_https_origin=True,
                canonical_tenant_verified=True,
                leased_job_context_matches=True,
                leased_application_context_matches=kwargs[
                    "application_context_matches"
                ],
                external_account_matches=kwargs["account_binding_verified"],
                no_login_or_auth_error=True,
                no_captcha_or_otp_or_lock=True,
                basic_information_control_hydrated=True,
                safe_signature="wdcp1:test",
            )

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def execute(self, statement):
            stmt_str = str(statement)
            if "workday_account_gates" in stmt_str:
                return _FakeResult(
                    SimpleNamespace(
                        id=GATE_ID,
                        user_id=USER_ID,
                        account_ref=str(ACCOUNT_REF),
                        portal_scope=SCOPE,
                    )
                )
            if "job_applications" in stmt_str:
                return _FakeResult(
                    SimpleNamespace(
                        id=APP_ID,
                        user_id=USER_ID,
                        workday_account_gate_id=GATE_ID,
                        job_url=TARGET_URL,
                        external_ats_url=None,
                        job_title="Engineer",
                        company_name="Wells Fargo",
                        deleted_at=None,
                    )
                )
            return _FakeResult(None)

    ctx = ProdPrivateContext(
        checkpoint_verifier=_MockVerifier(),
        session_factory=_FakeSession,
    )

    facts = await ctx.checkpoint_facts(lease=lease, observation=object())
    assert captured_verify["account_binding_verified"] is True
    assert captured_verify["application_context_matches"] is True
    assert facts.external_account_matches is True


@pytest.mark.asyncio
async def test_production_private_context_observe_only_missing_account_ref_fails() -> (
    None
):
    from services.workday_unit1_runtime import _PrivateContext as ProdPrivateContext
    from services.workday_unit1_checkpoint import WorkdayPrivateCheckpointEvidence

    lease = _lease(decision="observe_only")

    captured_verify = {}

    class _MockVerifier:
        async def verify(self, **kwargs):
            captured_verify.update(kwargs)
            return WorkdayPrivateCheckpointEvidence(
                approved_https_origin=True,
                canonical_tenant_verified=True,
                leased_job_context_matches=True,
                leased_application_context_matches=kwargs[
                    "application_context_matches"
                ],
                external_account_matches=kwargs["account_binding_verified"],
                no_login_or_auth_error=True,
                no_captcha_or_otp_or_lock=True,
                basic_information_control_hydrated=True,
                safe_signature="wdcp1:test",
            )

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def execute(self, statement):
            stmt_str = str(statement)
            if "workday_account_gates" in stmt_str:
                return _FakeResult(
                    SimpleNamespace(
                        id=GATE_ID,
                        user_id=USER_ID,
                        account_ref="",  # Missing account_ref
                        portal_scope=SCOPE,
                    )
                )
            if "job_applications" in stmt_str:
                return _FakeResult(
                    SimpleNamespace(
                        id=APP_ID,
                        user_id=USER_ID,
                        workday_account_gate_id=GATE_ID,
                        job_url=TARGET_URL,
                        external_ats_url=None,
                        job_title="Engineer",
                        company_name="Wells Fargo",
                        deleted_at=None,
                    )
                )
            return _FakeResult(None)

    ctx = ProdPrivateContext(
        checkpoint_verifier=_MockVerifier(),
        session_factory=_FakeSession,
    )

    facts = await ctx.checkpoint_facts(lease=lease, observation=object())
    assert captured_verify["account_binding_verified"] is False
    assert facts.external_account_matches is False
