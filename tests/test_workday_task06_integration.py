from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from api.automation import (
    ReleaseWorkdayCooldownLeaseRequest,
    worker_release_cooldown_lease,
)

from services.workday_account_gate_store import (
    WorkdayGateMutation,
    create_workday_account_gate_store,
)
from services.workday_playwright_worker import (
    PlaywrightWorkdayBrowser,
    WorkdayAccountSignals,
    extract_trusted_workday_unlock_time,
)
from services.workday_unit1_runtime import _Persistence
from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureOutcome,
    route_workday_failure,
)
from services.workday_transition_contracts import WorkdayFailureClass


USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")
APPLICATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000102")
APPLICATION_LEASE_ID = uuid.UUID("00000000-0000-0000-0000-000000000103")
GATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000104")
GATE_GENERATION = 3
GATE_LEASE_TOKEN = "opaque-gate-token"


@pytest.mark.parametrize("hours", [1, 12, 168])
def test_production_gate_factory_uses_configured_cooldown(monkeypatch, hours):
    monkeypatch.setattr(
        "services.workday_account_gate_store.get_settings",
        lambda: SimpleNamespace(workday_account_lock_cooldown_hours=hours),
    )

    store = create_workday_account_gate_store(object())

    assert store._default_lock_cooldown == timedelta(hours=hours)


def test_trusted_unlock_extractor_rejects_future_outside_bounded_window():
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    signals = WorkdayAccountSignals(
        trusted_account_message_text=(
            "Your account is temporarily locked until 2026-09-05T10:00:00Z."
        )
    )

    assert extract_trusted_workday_unlock_time(signals, observed_at=observed_at) is None


@pytest.mark.asyncio
async def test_post_submit_observation_keeps_only_trusted_unlock_deadline(monkeypatch):
    browser = PlaywrightWorkdayBrowser(object())

    async def capture_signals():
        return WorkdayAccountSignals(
            trusted_account_message_text=(
                "Your account is temporarily locked. Try again in 30 minutes."
            )
        )

    monkeypatch.setattr(browser, "_capture_account_signals", capture_signals)

    observation = await browser.observe_post_submit()

    assert observation.trusted_portal_until is not None
    assert observation.trusted_portal_until > datetime.now(UTC)
    assert not hasattr(observation, "trusted_account_message_text")


@dataclass
class _Api:
    retries: int = 0
    cooldown_releases: list[str] = field(default_factory=list)

    async def record_retry(self, lease, *, safe_reason):
        del lease, safe_reason
        self.retries += 1

    async def release_cooldown_or_defer(self, lease, *, reason):
        del lease
        self.cooldown_releases.append(reason)


@dataclass
class _Gate:
    backoffs: int = 0

    async def start_bounded_backoff(self, lease, *, backoff):
        del lease, backoff
        self.backoffs += 1
        return WorkdayGateMutation(applied=True)


@pytest.mark.asyncio
async def test_local_defer_uses_cooldown_release_and_transient_keeps_backoff():
    api = _Api()
    gate = _Gate()
    persistence = _Persistence(api=api, gate_store=gate)
    defer = route_workday_failure(
        WorkdayFailureFacts(frozenset({WorkdayFailureClass.COOLDOWN_ACTIVE}))
    )

    lease = SimpleNamespace(
        gate_id="gate",
        application_id="application",
        gate_generation=1,
        gate_lease_token="token",
    )
    await persistence.apply_route(lease=lease, route=defer)

    assert defer.outcome is WorkdayFailureOutcome.DEFER
    assert api.cooldown_releases == ["defer"]
    assert api.retries == 0


@dataclass
class _DatabaseResult:
    value: object

    def scalar_one_or_none(self):
        return self.value


class _CooldownReleaseDatabase:
    def __init__(self, application, gate, attempt):
        self._results = iter((application, gate, attempt))
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        del statement
        return _DatabaseResult(next(self._results))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _cooldown_release_fixture(*, reason: str, attempt_lease_expires_at: datetime):
    now = datetime.now(UTC)
    application = SimpleNamespace(
        id=APPLICATION_ID,
        user_id=USER_ID,
        deleted_at=None,
        status="preparing",
        retry_count=2,
        requeue_count=3,
        automation_batch_id=uuid.uuid4(),
        automation_lease_id=APPLICATION_LEASE_ID,
        automation_lease_expires_at=now + timedelta(minutes=5),
        workday_account_gate_id=GATE_ID,
    )
    post_submit = reason == "start_or_refresh_cooldown"
    gate = SimpleNamespace(
        id=GATE_ID,
        user_id=USER_ID,
        state="cooling_down",
        generation=GATE_GENERATION + int(post_submit),
        cooldown_until=now + timedelta(hours=1),
        user_min_until=None,
        next_eligible_at=now + timedelta(hours=2),
    )
    attempt = SimpleNamespace(
        gate_id=GATE_ID,
        application_id=APPLICATION_ID,
        generation=GATE_GENERATION,
        lease_token=GATE_LEASE_TOKEN,
        status="completed" if post_submit else "active",
        auth_submit_count=1 if post_submit else 0,
        lease_expires_at=attempt_lease_expires_at,
    )
    return application, gate, attempt


def _cooldown_release_request(reason: str) -> ReleaseWorkdayCooldownLeaseRequest:
    return ReleaseWorkdayCooldownLeaseRequest(
        lease_id=APPLICATION_LEASE_ID,
        gate_id=GATE_ID,
        gate_generation=GATE_GENERATION,
        gate_lease_token=GATE_LEASE_TOKEN,
        reason=reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["defer", "start_or_refresh_cooldown"])
async def test_cooldown_release_is_deferred_by_private_gate_without_counter_change(
    reason,
):
    application, gate, attempt = _cooldown_release_fixture(
        reason=reason,
        attempt_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    database = _CooldownReleaseDatabase(application, gate, attempt)

    result = await worker_release_cooldown_lease(
        application_id=APPLICATION_ID,
        body=_cooldown_release_request(reason),
        worker_user={"id": str(USER_ID)},
        db=database,
    )

    assert result == {
        "application_id": str(APPLICATION_ID),
        "status": "retrying",
        "next_eligible_at": gate.next_eligible_at,
    }
    assert gate.state == "cooling_down"
    assert gate.next_eligible_at > datetime.now(UTC)
    assert application.automation_lease_id is None
    assert application.automation_lease_expires_at is None
    assert (application.retry_count, application.requeue_count) == (2, 3)
    assert database.added[0].event_type == "application_cooldown_deferred"
    assert database.added[0].detail == reason
    assert database.commits == 1
    assert database.rollbacks == 0


@pytest.mark.asyncio
async def test_local_defer_rejects_an_expired_gate_attempt_without_mutation():
    application, gate, attempt = _cooldown_release_fixture(
        reason="defer",
        attempt_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    database = _CooldownReleaseDatabase(application, gate, attempt)

    with pytest.raises(HTTPException) as exc_info:
        await worker_release_cooldown_lease(
            application_id=APPLICATION_ID,
            body=_cooldown_release_request("defer"),
            worker_user={"id": str(USER_ID)},
            db=database,
        )

    assert exc_info.value.status_code == 409
    assert application.status == "preparing"
    assert application.automation_lease_id == APPLICATION_LEASE_ID
    assert (application.retry_count, application.requeue_count) == (2, 3)
    assert database.added == []
    assert database.commits == 0
    assert database.rollbacks == 1
