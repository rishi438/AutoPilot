from dataclasses import FrozenInstanceError, fields
from uuid import UUID

import pytest
from pydantic import ValidationError

from config.settings import Settings
from services.portal_control_resolver import PortalControlIntent
from services.workday_transition_contracts import (
    AUTH_SUBMIT_LIMIT_PER_ATTEMPT,
    WorkdayFailureClass,
    WorkdayGateState,
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)


@pytest.mark.parametrize(
    ("enum_type", "expected"),
    (
        (
            WorkdayTransitionState,
            {
                "job_page",
                "apply_choices",
                "account_page",
                "login_form",
                "auth_form_structurally_ready",
                "auth_outcome_pending",
                "authenticated_application_ready",
            },
        ),
        (
            WorkdayGateState,
            {
                "open",
                "cooling_down",
                "probe_in_progress",
                "auth_outcome_pending",
                "review_required",
            },
        ),
        (WorkdayTransitionStatus, {"verified", "quarantined", "retired"}),
        (WorkdayTransitionRisk, {"navigation_only", "auth_structure"}),
        (
            WorkdayFailureClass,
            {
                "cooldown_active",
                "wrong_origin_or_tenant",
                "post_submit_verified_success",
                "post_submit_account_locked",
                "post_submit_captcha_or_otp",
                "post_submit_auth_rejected",
                "post_submit_other_or_unknown",
                "pre_submit_captcha_or_otp",
                "job_unavailable",
                "pre_submit_transient",
                "structural_drift_pre_auth",
                "anything_else",
            },
        ),
    ),
)
def test_enums_have_exact_required_values(enum_type, expected: set[str]) -> None:
    assert {member.value for member in enum_type} == expected
    assert len(enum_type) == len(expected)


def _contracts():
    key = WorkdayTransitionKey(
        portal_family="workday",
        tenant_scope="workday:tenant:site",
        task_type="open_existing_sign_in",
        from_state_signature="safe-signature",
        action_intent=PortalControlIntent.SIGN_IN,
        signature_version=1,
        executor_policy_version=1,
    )
    candidate = WorkdaySafeCandidateMetadata(
        candidate_id="candidate-1",
        semantic_role="button",
        intent_key="sign_in",
        scope_key="active_dialog",
    )
    recipe = WorkdayTransitionRecipe(
        version_id=UUID(int=1),
        family_id=UUID(int=2),
        parent_version_id=None,
        safe_locator_strategy={"schema_version": 1, "require_unique": True},
        expected_to_state=WorkdayTransitionState.LOGIN_FORM,
        risk=WorkdayTransitionRisk.NAVIGATION_ONLY,
        status=WorkdayTransitionStatus.VERIFIED,
        recipe_version=1,
        signature_version=1,
        executor_policy_version=1,
    )
    observed = WorkdayObservedState(
        state=WorkdayTransitionState.ACCOUNT_PAGE,
        safe_signature="safe-signature",
        signature_version=1,
        portal_family="workday",
        tenant_scope="workday:tenant:site",
        candidate_metadata=(candidate,),
    )
    return key, recipe, candidate, observed


@pytest.mark.parametrize("contract_index", range(4))
def test_contract_dataclasses_are_immutable(contract_index: int) -> None:
    contract = _contracts()[contract_index]
    field_name = fields(contract)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(contract, field_name, "changed")


def test_transition_settings_have_required_defaults() -> None:
    assert Settings.model_fields["workday_transition_history_limit"].default == 10
    assert Settings.model_fields["workday_account_lock_cooldown_hours"].default == 6


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("workday_transition_history_limit", 0),
        ("workday_transition_history_limit", 101),
        ("workday_account_lock_cooldown_hours", 0),
        ("workday_account_lock_cooldown_hours", 169),
    ),
)
def test_transition_settings_reject_out_of_range_values(
    field_name: str, invalid_value: int
) -> None:
    values = {
        "jwt_secret": "Strong-Test-JWT-Secret-1234567890!",
        "database_url": "postgresql+asyncpg://test:test@localhost/test",
        field_name: invalid_value,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_auth_submit_limit_is_fixed_at_one() -> None:
    assert AUTH_SUBMIT_LIMIT_PER_ATTEMPT == 1
    assert "auth_submit_limit_per_attempt" not in Settings.model_fields


def test_contract_fields_exclude_prohibited_user_data_names() -> None:
    prohibited = {
        "user_id",
        "application_id",
        "email",
        "credential",
        "credentials",
        "answer",
        "answers",
        "token",
        "tokens",
        "cookie",
        "cookies",
        "resume",
        "resume_data",
        "raw_page_text",
        "browser_state",
    }
    contract_types = (
        WorkdayTransitionKey,
        WorkdayTransitionRecipe,
        WorkdaySafeCandidateMetadata,
        WorkdayObservedState,
    )
    field_names = {
        field.name
        for contract_type in contract_types
        for field in fields(contract_type)
    }
    assert field_names.isdisjoint(prohibited)


def test_transition_key_contains_all_compatibility_fields() -> None:
    assert {field.name for field in fields(WorkdayTransitionKey)} == {
        "portal_family",
        "tenant_scope",
        "task_type",
        "from_state_signature",
        "action_intent",
        "signature_version",
        "executor_policy_version",
    }


def test_transition_recipe_contains_all_compatibility_fields() -> None:
    assert {
        "version_id",
        "family_id",
        "parent_version_id",
        "safe_locator_strategy",
        "expected_to_state",
        "risk",
        "status",
        "recipe_version",
        "signature_version",
        "executor_policy_version",
    } <= {field.name for field in fields(WorkdayTransitionRecipe)}
