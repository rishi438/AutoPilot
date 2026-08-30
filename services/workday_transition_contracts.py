"""Shared, user-data-free contracts for Workday transition handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping
from uuid import UUID

from services.portal_control_resolver import PortalControlIntent


class WorkdayTransitionState(str, Enum):
    """Stable states in the bounded Workday account transition flow."""

    JOB_PAGE = "job_page"
    APPLY_CHOICES = "apply_choices"
    ACCOUNT_PAGE = "account_page"
    LOGIN_FORM = "login_form"
    AUTH_FORM_STRUCTURALLY_READY = "auth_form_structurally_ready"
    AUTH_OUTCOME_PENDING = "auth_outcome_pending"
    AUTHENTICATED_APPLICATION_READY = "authenticated_application_ready"


class WorkdayGateState(str, Enum):
    """Private account-gate lifecycle states."""

    OPEN = "open"
    COOLING_DOWN = "cooling_down"
    PROBE_IN_PROGRESS = "probe_in_progress"
    AUTH_OUTCOME_PENDING = "auth_outcome_pending"
    REVIEW_REQUIRED = "review_required"


class WorkdayTransitionStatus(str, Enum):
    """Execution eligibility for an immutable transition recipe."""

    VERIFIED = "verified"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class WorkdayTransitionRisk(str, Enum):
    """Risk boundary for one transition recipe."""

    NAVIGATION_ONLY = "navigation_only"
    AUTH_STRUCTURE = "auth_structure"


class WorkdayFailureClass(str, Enum):
    """First-match failure classes consumed by deterministic routing."""

    COOLDOWN_ACTIVE = "cooldown_active"
    WRONG_ORIGIN_OR_TENANT = "wrong_origin_or_tenant"
    POST_SUBMIT_VERIFIED_SUCCESS = "post_submit_verified_success"
    POST_SUBMIT_ACCOUNT_LOCKED = "post_submit_account_locked"
    POST_SUBMIT_CAPTCHA_OR_OTP = "post_submit_captcha_or_otp"
    POST_SUBMIT_AUTH_REJECTED = "post_submit_auth_rejected"
    POST_SUBMIT_OTHER_OR_UNKNOWN = "post_submit_other_or_unknown"
    PRE_SUBMIT_CAPTCHA_OR_OTP = "pre_submit_captcha_or_otp"
    JOB_UNAVAILABLE = "job_unavailable"
    PRE_SUBMIT_TRANSIENT = "pre_submit_transient"
    STRUCTURAL_DRIFT_PRE_AUTH = "structural_drift_pre_auth"
    ANYTHING_ELSE = "anything_else"


@dataclass(frozen=True, slots=True)
class WorkdayTransitionKey:
    """Compatibility key for shared transition-recipe lookup."""

    portal_family: str
    tenant_scope: str
    task_type: str
    from_state_signature: str
    action_intent: PortalControlIntent
    signature_version: int
    executor_policy_version: int


@dataclass(frozen=True, slots=True)
class WorkdayTransitionRecipe:
    """One immutable, versioned shared transition recipe."""

    version_id: UUID
    family_id: UUID
    parent_version_id: UUID | None
    safe_locator_strategy: Mapping[str, str | int | bool]
    expected_to_state: WorkdayTransitionState
    risk: WorkdayTransitionRisk
    status: WorkdayTransitionStatus
    recipe_version: int
    signature_version: int
    executor_policy_version: int


@dataclass(frozen=True, slots=True)
class WorkdaySafeCandidateMetadata:
    """Bounded semantic metadata for one safe, value-free control candidate."""

    candidate_id: str
    semantic_role: str
    intent_key: str
    scope_key: str


@dataclass(frozen=True, slots=True)
class WorkdayObservedState:
    """A versioned observation containing only safe structural facts."""

    state: WorkdayTransitionState
    safe_signature: str
    signature_version: int
    portal_family: str
    tenant_scope: str
    candidate_metadata: tuple[WorkdaySafeCandidateMetadata, ...] = ()


AUTH_SUBMIT_LIMIT_PER_ATTEMPT: Final[int] = 1
