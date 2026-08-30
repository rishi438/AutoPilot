"""Private evidence boundary for the authenticated Workday Unit 1 checkpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkdayPrivateCheckpointEvidence:
    """Boolean checkpoint evidence plus a safe structural signature."""

    approved_https_origin: bool
    canonical_tenant_verified: bool
    leased_job_context_matches: bool
    leased_application_context_matches: bool
    external_account_matches: bool
    no_login_or_auth_error: bool
    no_captcha_or_otp_or_lock: bool
    basic_information_control_hydrated: bool
    safe_signature: str

    def __post_init__(self) -> None:
        if not self.safe_signature:
            raise ValueError("A checkpoint evidence signature is required.")


class WorkdayPrivateCheckpointAdapter(Protocol):
    """Private browser adapter; only safe booleans leave this boundary."""

    async def capture_checkpoint_evidence(
        self,
        *,
        target_url: str,
        expected_tenant_scope: str,
        application_id: UUID,
        account_binding_verified: bool,
        application_context_matches: bool,
    ) -> WorkdayPrivateCheckpointEvidence: ...


class WorkdayUnit1CheckpointVerifier:
    """Ask one private adapter for fresh, non-serializable checkpoint evidence."""

    def __init__(self, adapter: WorkdayPrivateCheckpointAdapter) -> None:
        self._adapter = adapter

    async def verify(
        self,
        *,
        target_url: str,
        expected_tenant_scope: str,
        application_id: UUID,
        account_binding_verified: bool,
        application_context_matches: bool,
    ) -> WorkdayPrivateCheckpointEvidence:
        return await self._adapter.capture_checkpoint_evidence(
            target_url=target_url,
            expected_tenant_scope=expected_tenant_scope,
            application_id=application_id,
            account_binding_verified=account_binding_verified,
            application_context_matches=application_context_matches,
        )
