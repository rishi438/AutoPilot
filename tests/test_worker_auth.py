from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from utils.worker_auth import (
    WORKDAY_ACCOUNT_GATE_SCOPE,
    WORKDAY_APPLICATION_SCOPE,
    get_workday_application_worker_user,
    get_workday_worker_user,
    hash_worker_token,
    issue_worker_token,
    worker_token_device_id,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _Database:
    def __init__(self, row):
        self._row = row

    async def execute(self, statement):
        del statement
        return _Result(self._row)


def _user(*, profile_completed: bool = True):
    user_id = uuid.uuid4()
    return SimpleNamespace(
        id=user_id,
        profile_completed=profile_completed,
        to_dict=lambda: {
            "id": str(user_id),
            "email": "candidate@example.com",
            "profile_completed": profile_completed,
        },
    )


def test_worker_token_is_parseable_but_only_digest_is_persistable() -> None:
    device_id = uuid.uuid4()
    token, digest = issue_worker_token(device_id)

    assert worker_token_device_id(token) == device_id
    assert digest == hash_worker_token(token)
    assert token not in digest
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_active_scoped_worker_token_authenticates_its_user() -> None:
    device_id = uuid.uuid4()
    token, digest = issue_worker_token(device_id)
    user = _user()
    device = SimpleNamespace(
        id=device_id,
        token_digest=digest,
        scope=WORKDAY_ACCOUNT_GATE_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=None,
    )

    principal = await get_workday_worker_user(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db=_Database((device, user)),
    )

    assert principal["id"] == str(user.id)
    assert principal["profile_completed"] is True


@pytest.mark.asyncio
async def test_application_scope_can_use_both_application_and_account_gate_routes() -> (
    None
):
    device_id = uuid.uuid4()
    token, digest = issue_worker_token(device_id)
    user = _user()
    device = SimpleNamespace(
        id=device_id,
        token_digest=digest,
        scope=WORKDAY_APPLICATION_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=None,
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    assert (
        await get_workday_worker_user(
            credentials=credentials, db=_Database((device, user))
        )
    )["id"] == str(user.id)
    assert (
        await get_workday_application_worker_user(
            credentials=credentials, db=_Database((device, user))
        )
    )["id"] == str(user.id)


@pytest.mark.asyncio
async def test_account_gate_scope_cannot_read_application_form_values() -> None:
    device_id = uuid.uuid4()
    token, digest = issue_worker_token(device_id)
    device = SimpleNamespace(
        id=device_id,
        token_digest=digest,
        scope=WORKDAY_ACCOUNT_GATE_SCOPE,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_workday_application_worker_user(
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            ),
            db=_Database((device, _user())),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "revoked", "expired", "digest"])
async def test_inactive_or_invalid_worker_token_is_rejected(failure: str) -> None:
    device_id = uuid.uuid4()
    token, digest = issue_worker_token(device_id)
    device = SimpleNamespace(
        id=device_id,
        token_digest=("0" * 64 if failure == "digest" else digest),
        scope=WORKDAY_ACCOUNT_GATE_SCOPE,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if failure == "expired"
            else datetime.now(UTC) + timedelta(days=1)
        ),
        revoked_at=(datetime.now(UTC) if failure == "revoked" else None),
    )
    row = None if failure == "missing" else (device, _user())

    with pytest.raises(HTTPException) as exc_info:
        await get_workday_worker_user(
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            ),
            db=_Database(row),
        )

    assert exc_info.value.status_code == 401
