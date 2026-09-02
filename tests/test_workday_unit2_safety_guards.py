from __future__ import annotations

import inspect
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import services.workday_form_step_policy as form_step_policy
import services.workday_unit2_checkpoint as checkpoint_module
import services.workday_unit2_orchestrator as orch_module
import services.workday_unit2_save_step as save_step_module
from api.automation import (
    WorkerUnit2FinalizeRequest,
    WorkerUnit2SaveClaimRequest,
    worker_claim_unit2_save,
    worker_finalize_unit2_application,
)
from models.database import (
    ApplicationAutomationEvent,
    ApplicationStatus,
    JobApplication,
    WorkdayUnit2Attempt,
)


def test_static_guard_no_forbidden_services_in_unit2() -> None:
    """Ensure Unit 2 modules never import or invoke forbidden services."""
    forbidden_symbols = [
        "WorkdayAuthBroker",
        "workday_auth_broker",
        "PortalControlResolver",
        "portal_control_resolver",
        "WorkdayGateStore",
        "workday_account_gate_store",
        "WorkdayUnit1Orchestrator",
        "workday_unit1_orchestrator",
        "genai",
        "openai",
        "anthropic",
        "langchain",
    ]

    unit2_modules = [
        orch_module,
        checkpoint_module,
        save_step_module,
        form_step_policy,
    ]

    for mod in unit2_modules:
        source = inspect.getsource(mod)
        for sym in forbidden_symbols:
            assert (
                sym not in source
            ), f"Forbidden symbol '{sym}' found in {mod.__name__}"


def test_static_guard_save_button_exact_match_only() -> None:
    """Ensure Save button regex only matches exact 'Save and Continue' / 'Save & Continue'."""
    pattern = save_step_module._SAVE_AND_CONTINUE_EXACT

    assert pattern.match("Save and Continue")
    assert pattern.match("Save & Continue")
    assert pattern.match("  save and continue  ")
    assert pattern.match("SAVE & CONTINUE")

    # Reject generic Next, Submit, Review, or partials
    assert not pattern.match("Next")
    assert not pattern.match("Save")
    assert not pattern.match("Continue")
    assert not pattern.match("Review and Submit")
    assert not pattern.match("Submit Application")
    assert not pattern.match("Save and Continue Later")


def test_static_guard_closed_next_section_allowlist() -> None:
    """Ensure next section classifier fails closed on Review, Submit, or Confirmation."""
    assert checkpoint_module.classify_next_section_heading("Review") is None
    assert checkpoint_module.classify_next_section_heading("Review and Submit") is None
    assert checkpoint_module.classify_next_section_heading("Submit") is None
    assert checkpoint_module.classify_next_section_heading("Submit Application") is None
    assert (
        checkpoint_module.classify_next_section_heading("Application Submitted") is None
    )
    assert checkpoint_module.classify_next_section_heading("Confirmation") is None


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
        if "from application_automation_events" in stmt_str:
            params = set(stmt.compile().params.values())
            return _MockResult(
                [event.id for event in self.events if event.event_type in params]
            )
        return _MockResult([])


@pytest.mark.asyncio
async def test_sequential_save_claim_and_finalize_replay_five_repetitions() -> None:
    """Exercise the unit simulation five times without claiming concurrency proof."""
    for _ in range(5):
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

        db = _MockDb(app, attempt)
        db.events.append(
            ApplicationAutomationEvent(
                id=uuid.uuid4(),
                application_id=app_id,
                event_type="workday_unit1_completed",
                created_at=now,
            )
        )
        req = WorkerUnit2SaveClaimRequest(attempt_id=attempt_id, lease_id=lease_id)

        # First claim: claimed_now
        res1 = await worker_claim_unit2_save(
            application_id=app_id,
            body=req,
            worker_user={"id": str(user_id)},
            db=db,
        )
        assert res1.status == "claimed_now"
        assert attempt.save_claim_count == 1

        # Second claim: already_claimed
        res2 = await worker_claim_unit2_save(
            application_id=app_id,
            body=req,
            worker_user={"id": str(user_id)},
            db=db,
        )
        assert res2.status == "already_claimed"
        assert attempt.save_claim_count == 1

        # Finalize complete: idempotent replay
        finalize_req = WorkerUnit2FinalizeRequest(
            attempt_id=attempt_id,
            lease_id=lease_id,
            outcome="complete",
            checkpoint_version="workday_unit2_v1",
        )
        fin_res1 = await worker_finalize_unit2_application(
            application_id=app_id,
            body=finalize_req,
            worker_user={"id": str(user_id)},
            db=db,
        )
        assert fin_res1.status == "completed"

        fin_res2 = await worker_finalize_unit2_application(
            application_id=app_id,
            body=finalize_req,
            worker_user={"id": str(user_id)},
            db=db,
        )
        assert fin_res2.status == "completed"
