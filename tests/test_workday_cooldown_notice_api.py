from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api import automation
from services.workday_cooldown_notices import (
    WorkdayCooldownDecisionResult,
    WorkdayCooldownNoticeNotFoundError,
    WorkdayCooldownNoticeView,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _view():
    return WorkdayCooldownNoticeView(
        id=uuid4(),
        application_id=uuid4(),
        status="pending",
        safe_next_attempt_at=NOW + timedelta(hours=6),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_list_endpoint_uses_authenticated_owner_and_exposes_safe_fields(
    monkeypatch,
) -> None:
    user_id = uuid4()
    notice = _view()
    captured = SimpleNamespace(user_id=None)

    class _Store:
        def __init__(self, _db) -> None:
            pass

        async def list_pending(self, *, user_id):
            captured.user_id = user_id
            return [notice]

    monkeypatch.setattr(automation, "SQLAlchemyWorkdayCooldownNoticeStore", _Store)
    response = await automation.list_workday_cooldown_notices(
        current_user={"id": str(user_id)}, db=SimpleNamespace()
    )

    assert captured.user_id == user_id
    assert response == [automation._cooldown_notice_response(notice)]
    assert {"account_ref", "email", "credential", "lease_token"}.isdisjoint(response[0])


@pytest.mark.asyncio
async def test_decision_endpoint_hides_wrong_owner_as_not_found(monkeypatch) -> None:
    class _Store:
        def __init__(self, _db) -> None:
            pass

        async def decide(self, **_kwargs):
            raise WorkdayCooldownNoticeNotFoundError("private")

    monkeypatch.setattr(automation, "SQLAlchemyWorkdayCooldownNoticeStore", _Store)
    with pytest.raises(HTTPException) as raised:
        await automation.decide_workday_cooldown_notice(
            notice_id=uuid4(),
            body=automation.WorkdayCooldownDecisionRequest(decision="keep"),
            current_user={"id": str(uuid4())},
            db=SimpleNamespace(),
        )

    assert raised.value.status_code == 404
    assert raised.value.detail == "Cooldown notice not found."


@pytest.mark.asyncio
async def test_delete_endpoint_passes_explicit_confirmation_without_retry(
    monkeypatch,
) -> None:
    user_id = uuid4()
    notice = _view()
    captured = SimpleNamespace(kwargs=None)

    class _Store:
        def __init__(self, _db) -> None:
            pass

        async def decide(self, **kwargs):
            captured.kwargs = kwargs
            return WorkdayCooldownDecisionResult(notice=notice)

    monkeypatch.setattr(automation, "SQLAlchemyWorkdayCooldownNoticeStore", _Store)
    response = await automation.decide_workday_cooldown_notice(
        notice_id=notice.id,
        body=automation.WorkdayCooldownDecisionRequest(
            decision="delete", confirm_delete=True
        ),
        current_user={"id": str(user_id)},
        db=SimpleNamespace(),
    )

    assert captured.kwargs == {
        "user_id": user_id,
        "notice_id": notice.id,
        "decision": automation.WorkdayCooldownDecision.DELETE,
        "extend_until": None,
        "confirm_delete": True,
    }
    assert response["status"] == "pending"
    assert "retry" not in response


def test_decision_request_rejects_invalid_cross_field_choices() -> None:
    with pytest.raises(ValidationError):
        automation.WorkdayCooldownDecisionRequest(decision="extend")
    with pytest.raises(ValidationError):
        automation.WorkdayCooldownDecisionRequest(
            decision="keep", extend_until=NOW + timedelta(hours=1)
        )
    with pytest.raises(ValidationError):
        automation.WorkdayCooldownDecisionRequest(decision="delete")
