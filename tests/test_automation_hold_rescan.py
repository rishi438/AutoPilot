from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from api.automation import (
    ResolveHoldRequest,
    rescan_unknown_required_question,
    resolve_hold_with_answer,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RescanDatabase:
    def __init__(self, *, hold, application):
        self.hold = hold
        self.application = application
        self.added = []
        self.committed = False

    async def execute(self, statement):
        del statement
        return _ScalarResult(self.hold)

    async def get(self, model, item_id):
        del model
        assert item_id == self.application.id
        return self.application

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True


def _state(
    *, question=None, normalized_question=None, hold_code="unknown_required_question"
):
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    hold = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        user_id=user_id,
        application_id=application_id,
        hold_code=hold_code,
        question=question,
        normalized_question=normalized_question,
        retry_count=0,
        status="open",
        resolved_at=None,
    )
    application = SimpleNamespace(
        id=application_id,
        user_id=user_id,
        status="blocked",
        automation_lease_id=None,
        automation_lease_expires_at=None,
        automation_batch_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        external_ats_url=None,
        job_url=(
            "https://wd1.myworkdaysite.com/recruiting/wf/"
            "WellsFargoJobs/job/Engineer_R-1"
        ),
    )
    return user_id, hold, application


@pytest.mark.asyncio
async def test_rescan_requeues_owned_legacy_null_question_hold() -> None:
    user_id, hold, application = _state()
    database = _RescanDatabase(hold=hold, application=application)

    response = await rescan_unknown_required_question(
        hold_id=hold.id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response == {
        "hold_id": str(hold.id),
        "application_id": str(application.id),
        "application_status": "retrying",
        "retry_count": 1,
    }
    assert hold.status == "resolved"
    assert hold.resolved_at is not None
    assert application.status == "retrying"
    assert database.added[0].event_type == "retry_enqueued_for_form_rescan"
    assert database.committed is True


@pytest.mark.asyncio
async def test_rescan_requeues_question_hold_after_mapper_improvement() -> None:
    user_id, hold, application = _state(
        question="City*",
        normalized_question="city",
    )
    database = _RescanDatabase(hold=hold, application=application)

    response = await rescan_unknown_required_question(
        hold_id=hold.id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response["application_status"] == "retrying"
    assert hold.status == "resolved"
    assert application.status == "retrying"
    assert database.committed is True


@pytest.mark.asyncio
async def test_rescan_requeues_validation_failure_for_diagnostic_retry() -> None:
    user_id, hold, application = _state(hold_code="validation_failure")
    database = _RescanDatabase(hold=hold, application=application)

    response = await rescan_unknown_required_question(
        hold_id=hold.id,
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response["application_status"] == "retrying"
    assert response["retry_count"] == 1
    assert hold.status == "resolved"
    assert database.added[0].event_type == "retry_enqueued_for_form_rescan"
    assert database.committed is True


@pytest.mark.asyncio
async def test_rescan_rejects_non_question_hold() -> None:
    user_id, hold, application = _state(hold_code="captcha")
    database = _RescanDatabase(hold=hold, application=application)

    with pytest.raises(HTTPException) as exc_info:
        await rescan_unknown_required_question(
            hold_id=hold.id,
            current_user={"id": str(user_id)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == (
        "Only an unknown-required-question or validation-failure hold can be rescanned."
    )
    assert database.committed is False


@pytest.mark.asyncio
async def test_hold_answer_is_scoped_to_exact_application_hostname() -> None:
    user_id, hold, application = _state(
        question="Address Line 1*",
        normalized_question="address line 1",
    )
    database = _RescanDatabase(hold=hold, application=application)

    response = await resolve_hold_with_answer(
        hold_id=hold.id,
        body=ResolveHoldRequest(
            answer="user-confirmed-value",
            field_type="text",
            approved_for_reuse=True,
        ),
        current_user={"id": str(user_id)},
        db=database,
    )

    assert response["application_status"] == "retrying"
    assert database.added[0].source_portal == "wd1.myworkdaysite.com"
    assert database.added[0].approved_for_reuse is True
    assert database.committed is True
