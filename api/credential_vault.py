"""Authenticated API for the isolated portal credential vault."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import _bcrypt_safe, pwd_context
from config.settings import get_settings
from models.database import User
from services.portal_credentials import (
    PortalCredentialError,
    PortalCredentialRepository,
    normalize_portal_login_url,
    normalize_portal_scope,
)
from utils.auth import get_current_user
from utils.cache import check_rate_limit_with_headers
from utils.database import get_database
from utils.error_responses import (
    APIError,
    ErrorCode,
    external_service_error,
    not_found_error,
    rate_limit_error,
    validation_error,
)
from utils.portal_vault_database import get_portal_vault_collections

logger = logging.getLogger(__name__)
router = APIRouter()


class PortalCredentialBase(BaseModel):
    """Safe portal account metadata shared by generated and imported credentials."""

    model_config = ConfigDict(extra="forbid")

    portal_scope: str = Field(min_length=3, max_length=200)
    portal_name: str = Field(min_length=1, max_length=120)
    portal_login_url: str = Field(min_length=1, max_length=2000)
    account_email: EmailStr

    @field_validator("portal_scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return normalize_portal_scope(value)

    @field_validator("portal_login_url")
    @classmethod
    def validate_login_url(cls, value: str) -> str:
        return normalize_portal_login_url(value)

    @field_validator("portal_name")
    @classmethod
    def normalize_portal_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Portal name must not be blank.")
        return normalized


class GeneratePortalCredentialRequest(PortalCredentialBase):
    """Generate a credential only when the portal scope has no saved account."""


class StoreExistingPortalCredentialRequest(PortalCredentialBase):
    """Credentials supplied by the user through the protected vault page."""

    password: SecretStr = Field(min_length=8, max_length=256)


class RevealPortalCredentialRequest(BaseModel):
    """Fresh AutoPilot password verification required for each reveal."""

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=128)


def _user_id(current_user: dict[str, Any]) -> str:
    return str(uuid.UUID(str(current_user.get("id") or current_user.get("_id"))))


def _repository() -> PortalCredentialRepository:
    settings = get_settings()
    if (
        not settings.portal_vault_enabled
        or settings.portal_vault_encryption_key is None
        or settings.portal_vault_password_recovery_key is None
    ):
        raise external_service_error("Portal credential vault is not configured.")
    try:
        collections = get_portal_vault_collections()
    except RuntimeError as exc:
        raise external_service_error("Portal credential vault is unavailable.") from exc
    return PortalCredentialRepository(
        collections,
        settings.portal_vault_encryption_key.get_secret_value(),
        settings.portal_vault_password_recovery_key.get_secret_value(),
    )


def _disable_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _enforce_reveal_rate_limit(user_id: str, response: Response) -> None:
    result = await check_rate_limit_with_headers(
        identifier=f"{user_id}:portal_credential_reveal",
        limit=5,
        window_seconds=3600,
    )
    response.headers.update(result.get_headers())
    if not result.allowed:
        raise rate_limit_error(
            "Too many credential reveal attempts. Try again later.",
            retry_after=result.reset_seconds,
        )


async def _verify_autopilot_password(
    *,
    user_id: str,
    current_password: str,
    db: AsyncSession,
) -> None:
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise not_found_error("User not found.")
    if not user.password_hash:
        raise validation_error(
            "Set an AutoPilot password before revealing portal credentials."
        )
    if not pwd_context.verify(_bcrypt_safe(current_password), user.password_hash):
        raise APIError(
            ErrorCode.AUTH_INVALID_CREDENTIALS,
            "Current AutoPilot password is incorrect.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


@router.get("")
async def list_portal_credentials(
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    repository: PortalCredentialRepository = Depends(_repository),
) -> dict[str, Any]:
    """List owned vault metadata without ciphertext or passwords."""
    _disable_secret_caching(response)
    try:
        credentials = await repository.list_for_user(_user_id(current_user))
    except Exception as exc:
        logger.exception("Portal credential metadata listing failed")
        raise external_service_error("Portal credential vault is unavailable.") from exc
    return {"credentials": credentials}


@router.post("/generated")
async def generate_portal_credential(
    body: GeneratePortalCredentialRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    repository: PortalCredentialRepository = Depends(_repository),
) -> dict[str, Any]:
    """Create one encrypted random password per user and portal scope."""
    _disable_secret_caching(response)
    try:
        credential, created = await repository.generate_if_missing(
            user_id=_user_id(current_user),
            **body.model_dump(mode="json"),
        )
    except PortalCredentialError as exc:
        raise validation_error(str(exc)) from exc
    except Exception as exc:
        logger.exception("Generated portal credential storage failed")
        raise external_service_error("Portal credential vault is unavailable.") from exc
    return {"credential": credential, "created": created}


@router.put("/existing")
async def store_existing_portal_credential(
    body: StoreExistingPortalCredentialRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    repository: PortalCredentialRepository = Depends(_repository),
) -> dict[str, Any]:
    """Encrypt credentials entered by their owner; never echo the password."""
    _disable_secret_caching(response)
    values = body.model_dump(mode="json", exclude={"password"})
    try:
        credential = await repository.store_existing(
            user_id=_user_id(current_user),
            password=body.password.get_secret_value(),
            **values,
        )
    except PortalCredentialError as exc:
        raise validation_error(str(exc)) from exc
    except Exception as exc:
        logger.exception("Existing portal credential storage failed")
        raise external_service_error("Portal credential vault is unavailable.") from exc
    return {"credential": credential}


@router.post("/{credential_id}/reveal")
async def reveal_portal_credential(
    credential_id: uuid.UUID,
    body: RevealPortalCredentialRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_database),
    repository: PortalCredentialRepository = Depends(_repository),
) -> JSONResponse:
    """Reveal one owned password after fresh password verification."""
    user_id = _user_id(current_user)
    _disable_secret_caching(response)
    await _enforce_reveal_rate_limit(user_id, response)
    await _verify_autopilot_password(
        user_id=user_id,
        current_password=body.current_password.get_secret_value(),
        db=db,
    )
    try:
        password = await repository.reveal_for_user(
            user_id=user_id,
            credential_id=str(credential_id),
        )
    except PortalCredentialError as exc:
        logger.warning("Owned portal credential decryption failed")
        raise external_service_error("Portal credential cannot be decrypted.") from exc
    except Exception as exc:
        logger.exception("Portal credential reveal failed")
        raise external_service_error("Portal credential vault is unavailable.") from exc
    if password is None:
        raise not_found_error("Portal credential not found.")
    headers = dict(response.headers)
    headers["Cache-Control"] = "no-store, max-age=0"
    headers["Pragma"] = "no-cache"
    headers["Referrer-Policy"] = "no-referrer"
    return JSONResponse(
        {"credential_id": str(credential_id), "password": password},
        headers=headers,
    )
