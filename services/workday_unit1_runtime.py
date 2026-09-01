"""Production composition for one local Workday Unit 1 execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select

from config.settings import get_settings
from models.database import JobApplication, WorkdayAccountGate
from services.portal_account_automation import derive_workday_portal_scope
from services.portal_control_resolver import PortalControlResolver
from services.portal_credentials import PortalCredentialRepository
from services.workday_account_gate_store import (
    create_workday_account_gate_store,
    WorkdayAuthGateBinding,
    WorkdayGateLease,
)
from services.workday_auth_broker import WorkdayAuthBroker, WorkdayAuthBrokerRequest
from services.workday_auth_broker import WorkdayAuthAccountBinding
from services.workday_browser_runtime import PersistentWorkdayBrowserRuntime
from services.workday_failure_router import (
    WorkdayFailureOutcome,
    WorkdayFailureRoute,
    WorkdayGateOperation,
)
from services.workday_state_observer import WorkdayStateObserver
from services.workday_transition_catalog import SQLAlchemyWorkdayTransitionCatalog
from services.workday_transition_contracts import WorkdayFailureClass
from services.workday_transition_engine import WorkdayTransitionReplayEngine
from services.workday_transition_repair import WorkdayTransitionRepairCoordinator
from services.workday_unit1_orchestrator import (
    WorkdayExecutionPhase,
    WorkdayUnit1CheckpointFacts,
    WorkdayUnit1ExecutionError,
    WorkdayUnit1Orchestrator,
    WorkdayUnit1Result,
)
from services.workday_unit1_checkpoint import WorkdayUnit1CheckpointVerifier
from services.workday_worker_api import LeasedWorkdayApplication, WorkdayWorkerApi
from utils.database import get_session

logger = logging.getLogger(__name__)


class _Unit1Events:
    async def emit(self, event: dict[str, object]) -> None:
        logger.info("workday_unit1_event event=%s", event)


@dataclass
class _PrivateContext:
    checkpoint_verifier: WorkdayUnit1CheckpointVerifier
    session_factory: Callable[[], Any] = get_session
    account_ref: UUID | None = None
    account_binding_verified: bool = False

    async def auth_request(
        self, *, lease: LeasedWorkdayApplication, portal_scope: str
    ) -> WorkdayAuthBrokerRequest:
        async with self.session_factory() as session:
            gate = (
                await session.execute(
                    select(WorkdayAccountGate).where(
                        WorkdayAccountGate.id == lease.gate_id,
                        WorkdayAccountGate.user_id == lease.user_id,
                        WorkdayAccountGate.portal_scope == portal_scope,
                    )
                )
            ).scalar_one_or_none()
        if gate is None:
            raise RuntimeError("The leased Workday gate context is unavailable.")
        self.account_ref = UUID(gate.account_ref)
        return WorkdayAuthBrokerRequest(
            user_id=lease.user_id,
            account_ref=self.account_ref,
            portal_scope=portal_scope,
            gate_lease=_gate_lease(lease),
        )

    async def accept_auth_binding(self, binding: WorkdayAuthAccountBinding) -> None:
        if self.account_ref is None or binding.account_ref != self.account_ref:
            raise RuntimeError("The broker account binding does not match the gate.")
        self.account_binding_verified = True

    async def checkpoint_facts(self, *, lease, observation):
        del observation
        target = lease.to_workday_lease()
        portal_scope = derive_workday_portal_scope(target.target_url)
        async with self.session_factory() as session:
            application = (
                await session.execute(
                    select(WorkdayAccountGate).where(
                        WorkdayAccountGate.id == lease.gate_id,
                        WorkdayAccountGate.user_id == lease.user_id,
                        WorkdayAccountGate.portal_scope == portal_scope,
                    )
                )
            ).scalar_one_or_none()
            job = (
                await session.execute(
                    select(JobApplication).where(
                        JobApplication.id == lease.application_id,
                        JobApplication.user_id == lease.user_id,
                        JobApplication.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        gate = application
        account_ref: UUID | None = None
        if gate is not None and gate.account_ref:
            try:
                account_ref = UUID(gate.account_ref)
            except (ValueError, TypeError):
                account_ref = None
        application_context_matches = bool(
            job is not None
            and gate is not None
            and job.workday_account_gate_id == gate.id
            and job.job_url == lease.job_url
            and job.external_ats_url == lease.external_ats_url
            and job.job_title == lease.job_title
            and job.company_name == lease.company_name
        )
        if lease.gate_decision == "observe_only":
            account_binding_verified = bool(
                account_ref is not None and application_context_matches
            )
        else:
            account_binding_verified = bool(
                self.account_binding_verified
                and account_ref is not None
                and self.account_ref == account_ref
            )
        evidence = await self.checkpoint_verifier.verify(
            target_url=target.target_url,
            expected_tenant_scope=portal_scope,
            application_id=lease.application_id,
            account_binding_verified=account_binding_verified,
            application_context_matches=application_context_matches,
        )
        return WorkdayUnit1CheckpointFacts(
            approved_https_origin=evidence.approved_https_origin,
            canonical_tenant_verified=evidence.canonical_tenant_verified,
            leased_job_context_matches=evidence.leased_job_context_matches,
            leased_application_context_matches=(
                evidence.leased_application_context_matches
            ),
            external_account_matches=evidence.external_account_matches,
            no_login_or_auth_error=evidence.no_login_or_auth_error,
            no_captcha_or_otp_or_lock=evidence.no_captcha_or_otp_or_lock,
            basic_information_control_hydrated=evidence.basic_information_control_hydrated,
            safe_signature=evidence.safe_signature,
        )


class _CatalogStore:
    def __init__(self, *, session_factory: Callable[[], Any] = get_session) -> None:
        self._session_factory = session_factory

    async def ensure_family(self, key):
        async with self._session_factory() as session:
            return await SQLAlchemyWorkdayTransitionCatalog(session).ensure_family(key)

    async def get_current_and_history(self, key, limit):
        async with self._session_factory() as session:
            return await SQLAlchemyWorkdayTransitionCatalog(
                session
            ).get_current_and_history(key, limit)

    async def append_verified_and_set_current(self, **kwargs):
        async with self._session_factory() as session:
            return await SQLAlchemyWorkdayTransitionCatalog(
                session
            ).append_verified_and_set_current(**kwargs)

    async def rollback_current(self, **kwargs):
        async with self._session_factory() as session:
            return await SQLAlchemyWorkdayTransitionCatalog(session).rollback_current(
                **kwargs
            )


class _GateStore:
    def __init__(
        self,
        *,
        lock_cooldown_hours: int,
        session_factory: Callable[[], Any] = get_session,
    ) -> None:
        self._lock_cooldown_hours = lock_cooldown_hours
        self._session_factory = session_factory

    async def _call(self, method: str, *args, **kwargs):
        async with self._session_factory() as session:
            store = create_workday_account_gate_store(
                session, lock_cooldown_hours=self._lock_cooldown_hours
            )
            return await getattr(store, method)(*args, **kwargs)

    async def verify_auth_lease(
        self, lease: WorkdayGateLease, binding: WorkdayAuthGateBinding
    ) -> bool:
        return await self._call("verify_auth_lease", lease, binding)

    async def mark_secret_accessed(self, lease):
        return await self._call("mark_secret_accessed", lease)

    async def claim_auth_submit(self, lease):
        return await self._call("claim_auth_submit", lease)

    async def begin_auth_followup(self, lease):
        return await self._call("begin_auth_followup", lease)

    async def claim_llm_repair(self, lease):
        return await self._call("claim_llm_repair", lease)

    async def complete_success(self, lease):
        return await self._call("complete_success", lease)

    async def confirm_account_lock(self, lease, **kwargs):
        return await self._call("confirm_account_lock", lease, **kwargs)

    async def mark_review_required(self, lease):
        return await self._call("mark_review_required", lease)

    async def start_bounded_backoff(self, lease, **kwargs):
        return await self._call("start_bounded_backoff", lease, **kwargs)

    async def release_unsubmitted(self, lease):
        return await self._call("release_unsubmitted", lease)


class _Persistence:
    def __init__(self, *, api: WorkdayWorkerApi, gate_store):
        self._api = api
        self._gate_store = gate_store

    async def apply_route(
        self,
        *,
        lease: LeasedWorkdayApplication,
        route: WorkdayFailureRoute,
        review_gate_mutation_applied: bool = False,
    ) -> None:
        gate_lease = _gate_lease(lease)
        post_submit = route.failure_class.value.startswith("post_submit_")
        if review_gate_mutation_applied and (
            post_submit
            or route.gate_operation is not WorkdayGateOperation.MARK_REVIEW_REQUIRED
        ):
            raise ValueError(
                "A pre-applied review mutation requires a pre-submit review route."
            )
        if not post_submit:
            operation = route.gate_operation
            mutation = None
            if (
                operation is WorkdayGateOperation.MARK_REVIEW_REQUIRED
                and not review_gate_mutation_applied
            ):
                mutation = await self._gate_store.mark_review_required(gate_lease)
            elif operation is WorkdayGateOperation.RELEASE_UNSUBMITTED:
                mutation = await self._gate_store.release_unsubmitted(gate_lease)
            elif operation is WorkdayGateOperation.START_BOUNDED_BACKOFF:
                mutation = await self._gate_store.start_bounded_backoff(
                    gate_lease, backoff=timedelta(minutes=5)
                )
            if mutation is not None and not mutation.applied:
                raise RuntimeError("The Workday gate mutation was stale.")

        if route.outcome is WorkdayFailureOutcome.SKIP_APPLICATION:
            await self._api.record_skip(lease)
        elif route.outcome is WorkdayFailureOutcome.BOUNDED_BACKOFF:
            await self._api.record_retry(lease, safe_reason=route.outcome.value)
        elif route.outcome in {
            WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN,
            WorkdayFailureOutcome.DEFER,
        }:
            await self._api.release_cooldown_or_defer(lease, reason=route.outcome.value)
        else:
            if route.failure_class in {
                WorkdayFailureClass.POST_SUBMIT_ACCOUNT_EXISTS,
                WorkdayFailureClass.ACCOUNT_DISCOVERY_ACCOUNT_EXISTS,
            }:
                hold_code = "existing_account_credentials_required"
                remediation = (
                    "This Workday account already exists. Import its correct password "
                    "in AutoPilot Credential Vault, or reset it on Workday, then retry."
                )
            elif (
                route.failure_class
                is WorkdayFailureClass.ACCOUNT_DISCOVERY_RETRY_EXHAUSTED
            ):
                hold_code = "account_discovery_retry_exhausted"
                remediation = (
                    "AutoPilot reached the three-attempt limit for this unconfirmed "
                    "Workday account. Review or import the credential before retrying."
                )
            else:
                hold_code = {
                    WorkdayFailureOutcome.CREDENTIAL_HOLD: "native_credentials_required",
                    WorkdayFailureOutcome.USER_HOLD: "unknown_page_state",
                    WorkdayFailureOutcome.REVIEW_REQUIRED: "unknown_page_state",
                    WorkdayFailureOutcome.SECURITY_HOLD: "unknown_page_state",
                    WorkdayFailureOutcome.SAFE_HOLD: "unknown_page_state",
                }.get(route.outcome, "unknown_page_state")
                remediation = (
                    "Review this bounded Workday Unit 1 blocker before retrying."
                )
            await self._api.create_hold(
                lease,
                hold_code=hold_code,
                remediation=remediation,
            )

    async def complete_unit(
        self, *, lease: LeasedWorkdayApplication, authentication_submitted: bool
    ) -> None:
        await self._api.record_unit1_complete(
            lease, authentication_submitted=authentication_submitted
        )

    async def review_unit(
        self, *, lease: LeasedWorkdayApplication, authentication_submitted: bool
    ) -> None:
        await self._api.record_unit1_review(
            lease, authentication_submitted=authentication_submitted
        )

    async def recover_observe_only(
        self,
        *,
        lease: LeasedWorkdayApplication,
        route: WorkdayFailureRoute,
        trusted_portal_until=None,
    ) -> None:
        """Apply one submitted-attempt recovery before releasing the app lease."""
        gate_lease = _gate_lease(lease)
        if route.gate_operation is WorkdayGateOperation.CONFIRM_ACCOUNT_LOCK:
            mutation = await self._gate_store.confirm_account_lock(
                gate_lease,
                trusted_portal_until=trusted_portal_until,
            )
            if not mutation.applied:
                raise WorkdayUnit1ExecutionError(
                    "The Workday lock mutation was stale.",
                    phase=WorkdayExecutionPhase.STALE_AUTHORITY,
                )
            await self._api.release_cooldown_or_defer(lease, reason=route.outcome.value)
            return
        if route.outcome in {
            WorkdayFailureOutcome.USER_HOLD,
            WorkdayFailureOutcome.CREDENTIAL_HOLD,
            WorkdayFailureOutcome.REVIEW_REQUIRED,
            WorkdayFailureOutcome.SECURITY_HOLD,
            WorkdayFailureOutcome.SAFE_HOLD,
        }:
            await self._api.record_unit1_review(
                lease,
                authentication_submitted=True,
                hold_code=(
                    "existing_account_credentials_required"
                    if route.failure_class
                    is WorkdayFailureClass.POST_SUBMIT_ACCOUNT_EXISTS
                    else (
                        "native_credentials_required"
                        if route.outcome is WorkdayFailureOutcome.CREDENTIAL_HOLD
                        else "unknown_page_state"
                    )
                ),
            )
            return
        raise WorkdayUnit1ExecutionError(
            "The observe-only route is not a submitted recovery.",
            phase=WorkdayExecutionPhase.STALE_AUTHORITY,
        )


def _gate_lease(lease: LeasedWorkdayApplication) -> WorkdayGateLease:
    return WorkdayGateLease(
        gate_id=lease.gate_id,
        application_id=lease.application_id,
        generation=lease.gate_generation,
        lease_token=lease.gate_lease_token,
    )


class ProductionWorkdayUnit1Executor:
    """Open one browser and compose the existing Tasks 05-13 services."""

    def __init__(
        self,
        *,
        api: WorkdayWorkerApi,
        credential_reader: PortalCredentialRepository,
        runtime_factory: Callable[
            [LeasedWorkdayApplication], PersistentWorkdayBrowserRuntime
        ],
        resolver: PortalControlResolver,
        history_limit: int,
        lock_cooldown_hours: int | None = None,
        session_factory: Callable[[], Any] | None = None,
        catalog_factory: Callable[[Callable[[], Any]], Any] | None = None,
        gate_store_factory: Callable[[Callable[[], Any], int], Any] | None = None,
        checkpoint_verifier_factory: Callable[[Any], Any] | None = None,
        private_context_factory: Callable[[Any, Callable[[], Any]], Any] | None = None,
        persistence_factory: Callable[[WorkdayWorkerApi, Any], Any] | None = None,
    ) -> None:
        self._api = api
        self._credential_reader = credential_reader
        self._runtime_factory = runtime_factory
        self._resolver = resolver
        self._history_limit = history_limit
        self._lock_cooldown_hours = (
            get_settings().workday_account_lock_cooldown_hours
            if lock_cooldown_hours is None
            else lock_cooldown_hours
        )
        self._session_factory = session_factory or get_session
        self._catalog_factory = catalog_factory
        self._gate_store_factory = gate_store_factory
        self._checkpoint_verifier_factory = checkpoint_verifier_factory
        self._private_context_factory = private_context_factory
        self._persistence_factory = persistence_factory

    async def run(self, lease: LeasedWorkdayApplication) -> WorkdayUnit1Result:
        portal_scope = derive_workday_portal_scope(lease.to_workday_lease().target_url)
        runtime_entered = False
        try:
            runtime = self._runtime_factory(lease)
            async with runtime as browser:
                runtime_entered = True
                return await self._run_with_browser(
                    lease=lease, browser=browser, portal_scope=portal_scope
                )
        except WorkdayUnit1ExecutionError:
            raise
        except Exception as exc:
            # Errors from __aenter__ happen before a browser action. Exceptions
            # from the orchestrator happen inside the entered context and are
            # deliberately not mislabeled as startup failures.
            if runtime_entered:
                raise
            raise WorkdayUnit1ExecutionError(
                "The Workday browser runtime could not start.",
                phase=WorkdayExecutionPhase.BEFORE_BROWSER_ACTION,
            ) from exc

    async def _run_with_browser(
        self,
        *,
        lease: LeasedWorkdayApplication,
        browser,
        portal_scope: str,
    ) -> WorkdayUnit1Result:
        """Compose the existing Unit 1 services after runtime startup."""
        metadata_reader = getattr(
            self._credential_reader, "account_metadata_for_worker", None
        )
        account_metadata = (
            await metadata_reader(
                user_id=str(lease.user_id),
                portal_scope=portal_scope,
            )
            if callable(metadata_reader)
            else None
        )
        discovery_state = (
            account_metadata.discovery_state if account_metadata is not None else None
        )
        registration_required = bool(
            account_metadata is not None
            and account_metadata.status == "pending_registration"
            and (discovery_state or "login_pending")
            not in {"login_pending", "registration_submitted"}
        )
        logger.info(
            "workday_account_operation_selected portal_scope=%s operation=%s",
            portal_scope,
            "registration" if registration_required else "login",
        )
        observer = WorkdayStateObserver(browser, expected_tenant_scope=portal_scope)
        catalog = (
            self._catalog_factory(self._session_factory)
            if self._catalog_factory is not None
            else _CatalogStore(session_factory=self._session_factory)
        )
        gate_store = (
            self._gate_store_factory(self._session_factory, self._lock_cooldown_hours)
            if self._gate_store_factory is not None
            else _GateStore(
                lock_cooldown_hours=self._lock_cooldown_hours,
                session_factory=self._session_factory,
            )
        )
        checkpoint_verifier = (
            self._checkpoint_verifier_factory(browser)
            if self._checkpoint_verifier_factory is not None
            else WorkdayUnit1CheckpointVerifier(browser)
        )
        private_context = (
            self._private_context_factory(checkpoint_verifier, self._session_factory)
            if self._private_context_factory is not None
            else _PrivateContext(
                checkpoint_verifier=checkpoint_verifier,
                session_factory=self._session_factory,
            )
        )
        replay = WorkdayTransitionReplayEngine(
            observer=observer,
            browser_actions=browser,
            catalog=catalog,
            history_limit=self._history_limit,
        )
        repair = WorkdayTransitionRepairCoordinator(
            observer=observer,
            browser_actions=browser,
            catalog=catalog,
            gate_store=gate_store,
            resolver=self._resolver,
            history_limit=self._history_limit,
        )
        broker = WorkdayAuthBroker(
            gate_store=gate_store,
            credential_reader=self._credential_reader,
            page_actions=browser,
            state_observer=observer,
            outcome_observer=browser,
        )
        persistence = (
            self._persistence_factory(self._api, gate_store)
            if self._persistence_factory is not None
            else _Persistence(api=self._api, gate_store=gate_store)
        )
        return await WorkdayUnit1Orchestrator(
            page=browser,
            observer=observer,
            replay=replay,
            repair=repair,
            auth_broker=broker,
            private_context=private_context,
            persistence=persistence,
            events=_Unit1Events(),
            registration_required=registration_required,
        ).run(lease)
