"""Fail-closed native account gate for autonomous portal applications."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import unquote, urlsplit

from services.portal_credentials import (
    PortalCredentialError,
    PortalCredentialRepository,
    WorkerPortalCredential,
    normalize_portal_scope,
)

_WORKDAY_HOST = re.compile(r"(^|\.)(myworkdayjobs|myworkdaysite)\.com$")
_WORKDAY_CLUSTER = re.compile(r"^wd\d+$")
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_SCOPE_TOKEN = re.compile(r"[^a-z0-9._-]+")
logger = logging.getLogger(__name__)


class NativeAccountPageState(str, Enum):
    """Trusted observations emitted by a supported portal account adapter."""

    AUTHENTICATED = "authenticated"
    LOGIN_REQUIRED = "login_required"
    REGISTRATION_REQUIRED = "registration_required"
    LOGIN_COMPLETE = "login_complete"
    LOGIN_ACCOUNT_NOT_FOUND = "login_account_not_found"
    LOGIN_INVALID_CREDENTIALS = "login_invalid_credentials"
    REGISTRATION_ACCOUNT_EXISTS = "registration_account_exists"
    REGISTRATION_COMPLETE = "registration_complete"
    CAPTCHA = "captcha"
    OTP = "otp"
    ACCOUNT_TEMPORARILY_LOCKED = "account_temporarily_locked"
    TRANSIENT_FAILURE = "transient_failure"
    JOB_UNAVAILABLE = "job_unavailable"
    UNKNOWN = "unknown"


class NativeAccountAction(str, Enum):
    """One bounded action the local browser worker may take next."""

    CONTINUE_APPLICATION = "continue_application"
    ATTEMPT_LOGIN = "attempt_login"
    REGISTER_ACCOUNT = "register_account"
    CREATE_HOLD = "create_hold"
    RETRY_LATER = "retry_later"


@dataclass(frozen=True)
class NativeAccountPlan:
    """Credential-safe account action; secret representation is always redacted."""

    action: NativeAccountAction
    portal_scope: str
    hold_code: str | None = None
    account_ref: uuid.UUID | None = None
    credential: WorkerPortalCredential | None = field(default=None, repr=False)


def _scope_token(value: str) -> str:
    token = _SCOPE_TOKEN.sub("-", value.strip().lower()).strip("-._")
    if not token:
        raise PortalCredentialError("Workday tenant scope could not be determined.")
    return token


def derive_workday_portal_scope(job_or_apply_url: str) -> str:
    """Derive one stable credential scope per Workday tenant, never per job."""
    parsed = urlsplit(job_or_apply_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _WORKDAY_HOST.search(host):
        raise PortalCredentialError("A supported HTTPS Workday URL is required.")

    segments = [unquote(part).lower() for part in parsed.path.split("/") if part]
    if "recruiting" in segments:
        index = segments.index("recruiting")
        if len(segments) >= index + 3:
            tenant = _scope_token(segments[index + 1])
            site = _scope_token(segments[index + 2])
            return normalize_portal_scope(f"workday:{tenant}:{site}")

    host_parts = host.split(".")
    cluster_index = next(
        (
            index
            for index, part in enumerate(host_parts)
            if _WORKDAY_CLUSTER.fullmatch(part)
        ),
        None,
    )
    tenant = (
        _scope_token("-".join(host_parts[:cluster_index]))
        if cluster_index and host_parts[:cluster_index]
        else _scope_token(host)
    )
    site = next(
        (
            _scope_token(segment)
            for segment in segments
            if not _LOCALE_SEGMENT.fullmatch(segment)
            and segment not in {"job", "apply", "userhome"}
        ),
        "default",
    )
    return normalize_portal_scope(f"workday:{tenant}:{site}")


class NativePortalAccountCoordinator:
    """Choose login, registration, or a bounded hold from observed portal state."""

    def __init__(self, repository: PortalCredentialRepository):
        self._repository = repository

    async def plan_workday_action(
        self,
        *,
        user_id: str,
        job_or_apply_url: str,
        portal_name: str,
        account_email: str,
        page_state: NativeAccountPageState,
    ) -> NativeAccountPlan:
        scope = derive_workday_portal_scope(job_or_apply_url)
        logger.info(
            "portal_account_plan_started portal_scope=%s page_state=%s",
            scope,
            page_state.value,
        )

        if page_state is NativeAccountPageState.AUTHENTICATED:
            return NativeAccountPlan(NativeAccountAction.CONTINUE_APPLICATION, scope)
        if page_state in {
            NativeAccountPageState.LOGIN_COMPLETE,
            NativeAccountPageState.REGISTRATION_COMPLETE,
        }:
            method = (
                "login"
                if page_state is NativeAccountPageState.LOGIN_COMPLETE
                else "registration"
            )
            marked = await self._repository.mark_account_ready(
                user_id=user_id,
                portal_scope=scope,
                method=method,
            )
            logger.info(
                "portal_account_ready_marked portal_scope=%s method=%s marked=%s",
                scope,
                method,
                marked,
            )
            if not marked:
                return NativeAccountPlan(
                    NativeAccountAction.CREATE_HOLD,
                    scope,
                    hold_code="native_credentials_required",
                )
            return NativeAccountPlan(NativeAccountAction.CONTINUE_APPLICATION, scope)
        if page_state in {
            NativeAccountPageState.LOGIN_INVALID_CREDENTIALS,
            NativeAccountPageState.REGISTRATION_ACCOUNT_EXISTS,
        }:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD,
                scope,
                hold_code="native_credentials_required",
            )
        if page_state is NativeAccountPageState.CAPTCHA:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD, scope, hold_code="captcha"
            )
        if page_state is NativeAccountPageState.OTP:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD, scope, hold_code="otp"
            )
        if page_state is NativeAccountPageState.ACCOUNT_TEMPORARILY_LOCKED:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD,
                scope,
                hold_code="account_temporarily_locked",
            )
        if page_state is NativeAccountPageState.TRANSIENT_FAILURE:
            return NativeAccountPlan(NativeAccountAction.RETRY_LATER, scope)
        if page_state is NativeAccountPageState.UNKNOWN:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD,
                scope,
                hold_code="unknown_page_state",
            )

        if page_state not in {
            NativeAccountPageState.LOGIN_REQUIRED,
            NativeAccountPageState.LOGIN_ACCOUNT_NOT_FOUND,
            NativeAccountPageState.REGISTRATION_REQUIRED,
        }:
            return NativeAccountPlan(
                NativeAccountAction.CREATE_HOLD,
                scope,
                hold_code="unknown_page_state",
            )

        account = await self._repository.account_metadata_for_worker(
            user_id=user_id,
            portal_scope=scope,
        )
        logger.info(
            "portal_vault_entry_checked portal_scope=%s vault_entry=%s",
            scope,
            "present" if account is not None else "missing",
        )
        if account is not None and (
            page_state is NativeAccountPageState.LOGIN_REQUIRED
            or (
                page_state is NativeAccountPageState.REGISTRATION_REQUIRED
                and account.status == "active"
            )
        ):
            return NativeAccountPlan(
                NativeAccountAction.ATTEMPT_LOGIN,
                scope,
                account_ref=account.account_ref,
            )

        if account is None:
            logger.info(
                "portal_vault_entry_generation_started portal_scope=%s",
                scope,
            )
            await self._repository.generate_if_missing(
                user_id=user_id,
                portal_scope=scope,
                portal_name=portal_name,
                portal_login_url=job_or_apply_url,
                account_email=account_email,
            )
            account = await self._repository.account_metadata_for_worker(
                user_id=user_id,
                portal_scope=scope,
            )
            logger.info(
                "portal_vault_entry_generation_completed portal_scope=%s vault_entry=%s",
                scope,
                "present" if account is not None else "missing",
            )
        if account is None:
            raise PortalCredentialError("Portal credential could not be prepared.")
        if page_state is NativeAccountPageState.LOGIN_REQUIRED:
            return NativeAccountPlan(
                NativeAccountAction.ATTEMPT_LOGIN,
                scope,
                account_ref=account.account_ref,
            )
        credential = await self._repository.credential_for_worker(
            user_id=user_id,
            portal_scope=scope,
        )
        if credential is None:
            raise PortalCredentialError("Portal credential could not be prepared.")
        action = (
            NativeAccountAction.ATTEMPT_LOGIN
            if (
                page_state is NativeAccountPageState.REGISTRATION_REQUIRED
                and account.status == "active"
            )
            else NativeAccountAction.REGISTER_ACCOUNT
        )
        logger.info(
            "portal_account_plan_completed portal_scope=%s action=%s",
            scope,
            action.value,
        )
        if action is NativeAccountAction.ATTEMPT_LOGIN:
            return NativeAccountPlan(action, scope, account_ref=account.account_ref)
        return NativeAccountPlan(action, scope, credential=credential)
