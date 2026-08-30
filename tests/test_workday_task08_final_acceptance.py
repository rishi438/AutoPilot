"""Static acceptance guards for the production Unit 1 composition."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from services.workday_transition_contracts import (
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
)


ROOT = Path(__file__).resolve().parents[1]


def _module_source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _function(relative_path: str, name: str) -> tuple[str, ast.AST]:
    source = _module_source(relative_path)
    tree = ast.parse(source, filename=relative_path)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == name
        ):
            return source, node
    raise AssertionError(f"{relative_path} does not define {name}")


def _called_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        if isinstance(function, ast.Name):
            names.append(function.id)
        elif isinstance(function, ast.Attribute):
            names.append(function.attr)
    return names


def test_known_job_readiness_cannot_use_the_resolver() -> None:
    _source, function = _function(
        "services/workday_playwright_worker.py", "open_approved_job"
    )
    calls = _called_names(function)

    assert "_wait_for_known_apply_readiness" in calls
    assert "_resolve_action" not in calls
    assert "select" not in calls


def test_production_checkpoint_facts_are_verifier_derived() -> None:
    source, function = _function(
        "services/workday_unit1_runtime.py", "checkpoint_facts"
    )
    assert "self.checkpoint_verifier.verify" in ast.get_source_segment(source, function)

    checkpoint_fields = {
        "approved_https_origin",
        "canonical_tenant_verified",
        "leased_job_context_matches",
        "leased_application_context_matches",
        "external_account_matches",
        "no_login_or_auth_error",
        "no_captcha_or_otp_or_lock",
        "basic_information_control_hydrated",
    }
    hard_coded_successes = [
        child
        for child in ast.walk(function)
        if isinstance(child, ast.keyword)
        and child.arg in checkpoint_fields
        and isinstance(child.value, ast.Constant)
        and child.value.value is True
    ]
    assert hard_coded_successes == []


def test_auth_success_is_provisional_until_checkpoint_completion() -> None:
    source, broker_route = _function(
        "services/workday_auth_broker.py", "_route_and_apply"
    )
    route_source = ast.get_source_segment(source, broker_route)
    assert route_source is not None
    assert "provisional_success=True" in route_source
    assert "complete_success" not in _called_names(broker_route)

    _orchestrator_source, run = _function(
        "services/workday_unit1_orchestrator.py", "run"
    )
    calls = [
        child
        for child in ast.walk(run)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in {"_prove_checkpoint", "complete_unit"}
    ]
    calls.sort(key=lambda child: child.lineno)
    assert [child.func.attr for child in calls] == [
        "_prove_checkpoint",
        "complete_unit",
    ]

    _orchestrator_source, orchestrator_run = _function(
        "services/workday_unit1_orchestrator.py", "run"
    )
    for child in ast.walk(orchestrator_run):
        if not isinstance(child, ast.ExceptHandler):
            continue
        if isinstance(child.type, ast.Name) and child.type.id == "Exception":
            handler_source = ast.get_source_segment(_orchestrator_source, child)
            assert handler_source is not None
            assert "auth_submit_count=1" not in handler_source


def test_observe_only_and_cooldown_deferral_cannot_record_retry() -> None:
    _source, observe_only = _function(
        "services/workday_unit1_orchestrator.py", "_run_observe_only"
    )
    observe_calls = set(_called_names(observe_only))
    assert observe_calls.isdisjoint(
        {
            "open_approved_job",
            "execute_candidate",
            "goto",
            "reload",
            "fill",
            "click",
            "record_retry",
            "credential_for_auth_broker",
            "select_repair",
        }
    )

    persistence_source, apply_route = _function(
        "services/workday_unit1_runtime.py", "apply_route"
    )
    apply_source = ast.get_source_segment(persistence_source, apply_route)
    assert apply_source is not None
    assert "WorkdayFailureOutcome.BOUNDED_BACKOFF" in apply_source
    assert "WorkdayFailureOutcome.DEFER" in apply_source
    assert "WorkdayFailureOutcome.START_OR_REFRESH_COOLDOWN" in apply_source
    assert "release_cooldown_or_defer" in apply_source


def test_cooldown_and_trusted_portal_time_remain_wired() -> None:
    runtime = _module_source("services/workday_unit1_runtime.py")
    browser = _module_source("services/workday_playwright_worker.py")

    assert "lock_cooldown_hours=self._lock_cooldown_hours" in runtime
    assert "create_workday_account_gate_store(" in runtime
    assert "extract_trusted_workday_unlock_time(" in browser
    assert "trusted_portal_until=trusted_portal_until" in browser


def test_unit1_leasing_requires_application_scope() -> None:
    source, function = _function(
        "api/automation.py", "worker_lease_next_unit1_application"
    )
    function_source = ast.get_source_segment(source, function)
    assert function_source is not None
    assert "get_workday_application_worker_user" in function_source

    worker_api = _module_source("services/workday_worker_api.py")
    assert "/worker/unit1/queue/next" in worker_api


def test_shared_contracts_have_structural_fields_only() -> None:
    prohibited_fragments = {
        "user",
        "application",
        "account",
        "email",
        "credential",
        "secret",
        "answer",
        "cookie",
        "token",
        "raw_page",
    }
    for contract in (
        WorkdayTransitionKey,
        WorkdayTransitionRecipe,
        WorkdaySafeCandidateMetadata,
        WorkdayObservedState,
    ):
        field_names = {field.name.casefold() for field in fields(contract)}
        assert all(
            not any(fragment in field_name for fragment in prohibited_fragments)
            for field_name in field_names
        )


def test_unit1_orchestrator_exposes_no_later_navigation_or_submit() -> None:
    source = _module_source("services/workday_unit1_orchestrator.py")
    assert "advance_to_next_application_step" not in source
    assert "submit_application" not in source
    assert "Basic Information Next" not in source
