"""Authentication for narrowly scoped local automation worker devices."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import AutomationWorkerDevice, User
from utils.database import get_database

WORKDAY_ACCOUNT_GATE_SCOPE = "workday_account_gate"
WORKDAY_APPLICATION_SCOPE = "workday_application"
WORKER_TOKEN_PREFIX = "apw"
_WORKER_TOKEN_PATTERN = re.compile(
    rf"^{WORKER_TOKEN_PREFIX}_([0-9a-f]{{32}})_([A-Za-z0-9_-]{{40,64}})$"
)
_worker_security = HTTPBearer(auto_error=False)


def issue_worker_token(device_id: uuid.UUID) -> tuple[str, str]:
    """Return a one-time plaintext token and the digest safe to persist."""
    token = f"{WORKER_TOKEN_PREFIX}_{device_id.hex}_{secrets.token_urlsafe(32)}"
    return token, hash_worker_token(token)


def hash_worker_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def worker_token_device_id(token: str) -> uuid.UUID | None:
    match = _WORKER_TOKEN_PATTERN.fullmatch(token)
    if match is None:
        return None
    try:
        return uuid.UUID(hex=match.group(1))
    except ValueError:
        return None


async def get_workday_worker_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_worker_security),
    db: AsyncSession = Depends(get_database),
) -> dict[str, Any]:
    """Authenticate an active Workday device for account-gate operations."""
    return await _get_workday_worker_user(
        credentials=credentials,
        db=db,
        allowed_scopes={WORKDAY_ACCOUNT_GATE_SCOPE, WORKDAY_APPLICATION_SCOPE},
    )


async def get_workday_application_worker_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_worker_security),
    db: AsyncSession = Depends(get_database),
) -> dict[str, Any]:
    """Authenticate only a device explicitly granted application-form access."""
    return await _get_workday_worker_user(
        credentials=credentials,
        db=db,
        allowed_scopes={WORKDAY_APPLICATION_SCOPE},
    )


async def _get_workday_worker_user(
    *,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    allowed_scopes: set[str],
) -> dict[str, Any]:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or inactive worker device credential.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    token = credentials.credentials
    device_id = worker_token_device_id(token)
    if device_id is None:
        raise unauthorized

    row = (
        await db.execute(
            select(AutomationWorkerDevice, User)
            .join(User, User.id == AutomationWorkerDevice.user_id)
            .where(AutomationWorkerDevice.id == device_id)
        )
    ).one_or_none()
    if row is None:
        raise unauthorized
    device, user = row
    now = datetime.now(UTC)
    if (
        device.revoked_at is not None
        or device.expires_at <= now
        or device.scope not in allowed_scopes
        or not user.profile_completed
        or not hmac.compare_digest(device.token_digest, hash_worker_token(token))
    ):
        raise unauthorized
    return user.to_dict()
