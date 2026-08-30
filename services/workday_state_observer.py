"""Read-only, value-free observation of bounded Workday page structure."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence

from services.portal_account_automation import derive_workday_portal_scope
from services.portal_control_resolver import PortalControlIntent
from services.portal_credentials import PortalCredentialError, normalize_portal_scope
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionState,
)
from services.workday_page_condition import WorkdayPageConditionError

logger = logging.getLogger(__name__)

SIGNATURE_VERSION = 1
MAX_OBSERVED_CONTROLS = 80
MAX_SAFE_CANDIDATES = 40

_SAFE_ROLES = frozenset(
    {
        "alert",
        "button",
        "checkbox",
        "combobox",
        "form",
        "link",
        "progressbar",
        "radio",
        "status",
        "textbox",
    }
)
_ACTION_ROLES = frozenset({"button", "link"})
_FIELD_ROLES = frozenset({"checkbox", "combobox", "radio", "textbox"})
_INTENT_PATTERNS: tuple[tuple[PortalControlIntent, re.Pattern[str]], ...] = (
    (
        PortalControlIntent.APPLY_MANUALLY,
        re.compile(r"^apply\s+manually$", re.IGNORECASE),
    ),
    (
        PortalControlIntent.APPLY,
        re.compile(r"^apply(?:\s+now)?$", re.IGNORECASE),
    ),
    (
        PortalControlIntent.OPEN_REGISTRATION,
        re.compile(r"^(?:create\s+account|register)$", re.IGNORECASE),
    ),
    (
        PortalControlIntent.SIGN_IN,
        re.compile(r"^(?:sign\s+in|log\s+in)$", re.IGNORECASE),
    ),
    (
        PortalControlIntent.CREATE_ACCOUNT,
        re.compile(r"^(?:create\s+account|register)$", re.IGNORECASE),
    ),
    (
        PortalControlIntent.NEXT_APPLICATION_STEP,
        re.compile(r"^(?:next|save\s+and\s+continue)$", re.IGNORECASE),
    ),
)


class WorkdayStateObservationError(RuntimeError):
    """Raised when a page cannot be converted to a supported safe state."""


class WorkdayStateSecurityError(WorkdayStateObservationError):
    """Typed fail-closed result for an untrusted origin or tenant."""

    failure_class = WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT

    def __init__(self) -> None:
        super().__init__("Workday observation failed origin or tenant verification.")


class WorkdayControlScope(str, Enum):
    """Safe structural scope used to prevent covered controls winning."""

    PAGE = "page"
    ACTIVE_DIALOG = "active_dialog"
    ACTIVE_ACCOUNT_FORM = "active_account_form"


_SAFE_DIAGNOSTIC_ROLE_KEYS = (
    "account_consent",
    "application_field",
    "auth_challenge_field",
    "auth_dialog_control",
    "auth_pending",
    "button",
    "email_field",
    "form",
    "link",
    "password_field",
    "portal_alert",
    "progressbar",
    "status",
)
_SAFE_DIAGNOSTIC_INTENT_KEYS = (
    PortalControlIntent.APPLY_MANUALLY.value,
    PortalControlIntent.APPLY.value,
    PortalControlIntent.OPEN_REGISTRATION.value,
    PortalControlIntent.SIGN_IN.value,
    PortalControlIntent.CREATE_ACCOUNT.value,
    PortalControlIntent.NEXT_APPLICATION_STEP.value,
)
_SAFE_DIAGNOSTIC_SCOPE_KEYS = tuple(scope.value for scope in WorkdayControlScope)


@dataclass(frozen=True, slots=True)
class WorkdaySemanticControl:
    """One adapter-provided semantic control; values and DOM handles are forbidden."""

    role: str
    semantic_name: str = ""
    input_type: str = ""
    visible: bool = True
    enabled: bool = True
    busy: bool = False
    scope_kind: WorkdayControlScope = WorkdayControlScope.PAGE


@dataclass(frozen=True, slots=True)
class WorkdayPageStructure:
    """Fresh, bounded adapter snapshot used only during one observation."""

    url: str
    controls: tuple[WorkdaySemanticControl, ...]


class WorkdayPageObserverAdapter(Protocol):
    """Small read-only page surface; implementations must capture fresh structure."""

    async def capture_structure(self, *, limit: int) -> WorkdayPageStructure: ...


@dataclass(frozen=True, slots=True)
class _SafeFact:
    role: str
    intent: PortalControlIntent | None = None
    scope_kind: WorkdayControlScope = WorkdayControlScope.PAGE
    executable: bool = True


def _normalize_role(value: str) -> str | None:
    role = value.strip().casefold()
    return role if role in _SAFE_ROLES else None


def _normalize_scope(value: WorkdayControlScope | str) -> WorkdayControlScope | None:
    try:
        return (
            value
            if isinstance(value, WorkdayControlScope)
            else WorkdayControlScope(value)
        )
    except ValueError:
        return None


def _is_challenge_control(control: WorkdaySemanticControl) -> bool:
    return (
        control.input_type.strip().casefold()
        in {
            "one-time-code",
            "otp",
        }
        or "verification" in control.semantic_name.casefold()
    )


def _is_consent_control(control: WorkdaySemanticControl) -> bool:
    normalized = control.semantic_name.casefold()
    return "consent" in normalized or "terms and conditions" in normalized


def _normalize_intent(name: str) -> PortalControlIntent | None:
    normalized = " ".join(name.split())[:160]
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.fullmatch(normalized):
            return intent
    return None


def _safe_facts(
    controls: Sequence[WorkdaySemanticControl],
) -> tuple[list[_SafeFact], Counter[str], Counter[str], Counter[str]]:
    facts: list[_SafeFact] = []
    roles: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    for control in controls[:MAX_OBSERVED_CONTROLS]:
        if not control.visible:
            continue
        role = _normalize_role(control.role)
        if role is None:
            continue
        scope_kind = _normalize_scope(control.scope_kind)
        if scope_kind is None:
            continue
        intent = (
            _normalize_intent(control.semantic_name) if role in _ACTION_ROLES else None
        )
        structural_disabled_sign_in = (
            not control.enabled
            and scope_kind
            in {
                WorkdayControlScope.ACTIVE_DIALOG,
                WorkdayControlScope.ACTIVE_ACCOUNT_FORM,
            }
            and role in _ACTION_ROLES
            and intent is PortalControlIntent.SIGN_IN
        )
        if not control.enabled and not structural_disabled_sign_in:
            continue
        input_type = control.input_type.strip().casefold()
        if _is_challenge_control(control):
            semantic_role = "auth_challenge_field"
        elif _is_consent_control(control):
            semantic_role = "account_consent"
        elif role == "textbox" and input_type in {"email", "password"}:
            semantic_role = f"{input_type}_field"
        elif (
            scope_kind
            in {
                WorkdayControlScope.ACTIVE_DIALOG,
                WorkdayControlScope.ACTIVE_ACCOUNT_FORM,
            }
            and role in _FIELD_ROLES
        ):
            semantic_role = "auth_dialog_control"
        elif role in _FIELD_ROLES:
            semantic_role = "application_field"
        elif role in {"progressbar", "status"} and control.busy:
            semantic_role = "auth_pending"
        elif role == "alert":
            semantic_role = "portal_alert"
        else:
            semantic_role = role
        facts.append(
            _SafeFact(
                semantic_role,
                intent,
                scope_kind,
                executable=control.enabled,
            )
        )
        roles[semantic_role] += 1
        scopes[scope_kind.value] += 1
        if intent is not None:
            intents[intent.value] += 1
    if (
        roles["email_field"]
        and roles["password_field"] >= 2
        and intents[PortalControlIntent.OPEN_REGISTRATION.value]
    ):
        facts = [
            (
                _SafeFact(
                    fact.role,
                    PortalControlIntent.CREATE_ACCOUNT,
                    fact.scope_kind,
                    fact.executable,
                )
                if fact.intent is PortalControlIntent.OPEN_REGISTRATION
                else fact
            )
            for fact in facts
        ]
        count = intents.pop(PortalControlIntent.OPEN_REGISTRATION.value)
        intents[PortalControlIntent.CREATE_ACCOUNT.value] += count
    return facts, roles, intents, scopes


def _safe_count_summary(counts: Counter[str], *, allowed_keys: Sequence[str]) -> str:
    """Format bounded counts whose keys come only from a fixed safe allowlist."""
    parts = [
        f"{key}:{min(counts[key], MAX_OBSERVED_CONTROLS)}"
        for key in allowed_keys
        if counts[key] > 0
    ]
    return ",".join(parts) or "none"


def _classify_state(
    roles: Counter[str], intents: Counter[str]
) -> WorkdayTransitionState:
    if roles["auth_pending"]:
        return WorkdayTransitionState.AUTH_OUTCOME_PENDING
    if roles["auth_challenge_field"] or roles["account_consent"]:
        return WorkdayTransitionState.AUTH_OUTCOME_PENDING
    if (
        roles["application_field"]
        or intents[PortalControlIntent.NEXT_APPLICATION_STEP.value]
    ):
        return WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY

    has_email = roles["email_field"] > 0
    password_count = roles["password_field"]
    has_sign_in = intents[PortalControlIntent.SIGN_IN.value] > 0
    has_create = intents[PortalControlIntent.CREATE_ACCOUNT.value] > 0
    if (has_email and password_count >= 1 and has_sign_in) or (
        has_email and password_count >= 2 and has_create
    ):
        return WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
    if has_email or password_count or (has_sign_in and roles["form"]):
        return WorkdayTransitionState.LOGIN_FORM
    if intents[PortalControlIntent.APPLY_MANUALLY.value]:
        return WorkdayTransitionState.APPLY_CHOICES
    if intents[PortalControlIntent.APPLY.value]:
        return WorkdayTransitionState.JOB_PAGE
    if (
        has_sign_in
        or has_create
        or intents[PortalControlIntent.OPEN_REGISTRATION.value]
    ):
        return WorkdayTransitionState.ACCOUNT_PAGE
    if roles["portal_alert"]:
        return WorkdayTransitionState.AUTH_OUTCOME_PENDING
    raise WorkdayStateObservationError(
        "The visible Workday structure does not match a supported Unit 1 state."
    )


def _auth_pending_reason_codes(roles: Counter[str]) -> tuple[str, ...]:
    """Return only value-free structural reasons for a pending auth state."""
    reason_roles = (
        "auth_pending",
        "auth_challenge_field",
        "account_consent",
        "portal_alert",
    )
    return tuple(role for role in reason_roles if roles[role])


def _signature(
    *,
    state: WorkdayTransitionState,
    roles: Counter[str],
    intents: Counter[str],
    scopes: Counter[str],
) -> str:
    payload = {
        "intents": sorted(intents.items()),
        "roles": sorted(roles.items()),
        "scopes": sorted(
            (scope, count) for scope, count in scopes.items() if scope != "page"
        ),
        "state": state.value,
        "version": SIGNATURE_VERSION,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return f"wds{SIGNATURE_VERSION}:{hashlib.sha256(encoded).hexdigest()}"


def _candidate_metadata(
    facts: Sequence[_SafeFact],
) -> tuple[WorkdaySafeCandidateMetadata, ...]:
    candidates: list[WorkdaySafeCandidateMetadata] = []
    ordinals: Counter[tuple[str, str]] = Counter()
    for fact in facts:
        if not fact.executable or fact.intent is None or fact.role not in _ACTION_ROLES:
            continue
        key = (fact.role, fact.intent.value)
        ordinal = ordinals[key]
        ordinals[key] += 1
        digest = hashlib.sha256(
            f"{key[0]}:{key[1]}:{ordinal}".encode("ascii")
        ).hexdigest()[:16]
        candidates.append(
            WorkdaySafeCandidateMetadata(
                candidate_id=f"wdc-{digest}",
                semantic_role=fact.role,
                intent_key=fact.intent.value,
                scope_key=fact.scope_kind.value,
            )
        )
        if len(candidates) >= MAX_SAFE_CANDIDATES:
            break
    return tuple(candidates)


class WorkdayStateObserver:
    """Re-observe and emit only deterministic, non-private Workday facts."""

    def __init__(
        self,
        adapter: WorkdayPageObserverAdapter,
        *,
        expected_tenant_scope: str,
        portal_family: str = "workday",
    ) -> None:
        self._adapter = adapter
        self._expected_tenant_scope = normalize_portal_scope(expected_tenant_scope)
        if not self._expected_tenant_scope.startswith("workday:"):
            raise ValueError("A canonical Workday tenant scope is required.")
        if portal_family != "workday":
            raise ValueError("The observer supports only the Workday portal family.")
        self._portal_family = portal_family

    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState:
        """Capture fresh structure without mutating the page or retaining raw data."""
        capture_condition = getattr(self._adapter, "capture_page_condition", None)
        if callable(capture_condition):
            condition = await capture_condition()
            if condition.pre_submit_failure is not None:
                raise WorkdayPageConditionError(condition)
        structure = await self._adapter.capture_structure(limit=MAX_OBSERVED_CONTROLS)
        try:
            current_scope = derive_workday_portal_scope(structure.url)
        except PortalCredentialError as exc:
            raise WorkdayStateSecurityError() from exc
        if current_scope != self._expected_tenant_scope:
            raise WorkdayStateSecurityError()

        facts, roles, intents, scopes = _safe_facts(structure.controls)
        try:
            state = _classify_state(roles, intents)
        except WorkdayStateObservationError:
            captured_control_count = min(len(structure.controls), MAX_OBSERVED_CONTROLS)
            safe_fact_count = min(len(facts), MAX_OBSERVED_CONTROLS)
            logger.info(
                "workday_state_observation_unsupported "
                "captured_control_count=%s safe_fact_count=%s "
                "discarded_control_count=%s role_counts=%s "
                "intent_counts=%s scope_counts=%s",
                captured_control_count,
                safe_fact_count,
                max(captured_control_count - safe_fact_count, 0),
                _safe_count_summary(roles, allowed_keys=_SAFE_DIAGNOSTIC_ROLE_KEYS),
                _safe_count_summary(intents, allowed_keys=_SAFE_DIAGNOSTIC_INTENT_KEYS),
                _safe_count_summary(scopes, allowed_keys=_SAFE_DIAGNOSTIC_SCOPE_KEYS),
            )
            raise
        if state is WorkdayTransitionState.AUTH_OUTCOME_PENDING:
            logger.info(
                "workday_state_observed state=%s safe_reasons=%s",
                state.value,
                ",".join(_auth_pending_reason_codes(roles)) or "unknown",
            )
        return WorkdayObservedState(
            state=state,
            safe_signature=_signature(
                state=state, roles=roles, intents=intents, scopes=scopes
            ),
            signature_version=SIGNATURE_VERSION,
            portal_family=self._portal_family,
            tenant_scope=self._expected_tenant_scope,
            candidate_metadata=(
                _candidate_metadata(facts) if include_candidates else ()
            ),
        )
