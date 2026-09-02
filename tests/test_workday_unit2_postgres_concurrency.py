"""Real PostgreSQL contention proof for Workday Unit 2.

Run only with UNIT2_POSTGRES_TEST_URL pointed at a disposable PostgreSQL
database. The test creates and drops the repository schema in that database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.automation import (
    WorkerUnit2FinalizeRequest,
    WorkerUnit2SaveClaimRequest,
    retry_review_hold,
    worker_claim_unit2_save,
    worker_finalize_unit2_application,
    worker_lease_next_unit2_application,
)
from models.database import (
    ApplicationAutomationBatch,
    ApplicationAutomationEvent,
    ApplicationHold,
    ApplicationStatus,
    AuthMethod,
    Base,
    JobApplication,
    User,
    WorkdayUnit2Attempt,
)


_DATABASE_URL = os.getenv("UNIT2_POSTGRES_TEST_URL")


async def _run_simultaneously(
    first: Callable[[], Awaitable[Any]],
    second: Callable[[], Awaitable[Any]],
) -> tuple[Any, Any]:
    barrier = asyncio.Barrier(2)

    async def _run(call: Callable[[], Awaitable[Any]]) -> Any:
        await barrier.wait()
        return await call()

    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(_run(first), _run(second)),
        timeout=15,
    )
    return first_result, second_result


async def _create_eligible_application(
    sessions: async_sessionmaker[AsyncSession], *, iteration: int
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    application_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with sessions() as session:
        session.add(
            User(
                id=user_id,
                email=f"unit2-pg-{iteration}-{user_id.hex}@example.test",
                full_name="Unit 2 PostgreSQL Test",
                auth_method=AuthMethod.GOOGLE.value,
                google_id=f"unit2-pg-{user_id.hex}",
            )
        )
        session.add(
            ApplicationAutomationBatch(
                id=batch_id,
                user_id=user_id,
                worker_kind="local_playwright",
                status="queued",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            JobApplication(
                id=application_id,
                user_id=user_id,
                automation_batch_id=batch_id,
                job_title=f"Unit 2 Engineer {iteration}",
                company_name="Disposable PostgreSQL",
                job_url=(
                    "https://wd1.myworkdaysite.com/job/" f"UNIT2-PG-{iteration}/apply"
                ),
                portal="workday",
                status=ApplicationStatus.APPLYING.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ApplicationAutomationEvent(
                id=uuid.uuid4(),
                application_id=application_id,
                batch_id=batch_id,
                event_type="workday_unit1_completed",
                detail="postgres_contention_fixture",
                created_at=now,
            )
        )
        await session.commit()
    return user_id, application_id


@pytest.mark.skipif(
    not _DATABASE_URL,
    reason="requires UNIT2_POSTGRES_TEST_URL for a disposable PostgreSQL database",
)
@pytest.mark.asyncio
async def test_postgresql_lease_claim_and_terminal_contention_five_times() -> None:
    """Prove real simultaneous lease, claim, and terminal contention five times."""
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL, pool_pre_ping=True)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    try:
        async with engine.begin() as connection:
            assert connection.dialect.name == "postgresql"
            await connection.run_sync(Base.metadata.create_all)

        for iteration in range(5):
            user_id, application_id = await _create_eligible_application(
                sessions,
                iteration=iteration,
            )
            worker_user = {"id": str(user_id)}

            async def _lease() -> Any:
                async with sessions() as session:
                    return await worker_lease_next_unit2_application(
                        application_id=application_id,
                        worker_user=worker_user,
                        db=session,
                    )

            lease_results = await _run_simultaneously(_lease, _lease)
            leases = [
                result.application for result in lease_results if result.application
            ]
            assert len(leases) == 1
            lease = leases[0]
            assert lease.mode == "normal"

            async with sessions() as session:
                attempt_count = await session.scalar(
                    select(func.count())
                    .select_from(WorkdayUnit2Attempt)
                    .where(WorkdayUnit2Attempt.application_id == application_id)
                )
                assert attempt_count == 1

            claim_body = WorkerUnit2SaveClaimRequest(
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
            )

            async def _claim() -> str:
                async with sessions() as session:
                    response = await worker_claim_unit2_save(
                        application_id=application_id,
                        body=claim_body,
                        worker_user=worker_user,
                        db=session,
                    )
                    return response.status

            claim_results = await _run_simultaneously(_claim, _claim)
            assert sorted(claim_results) == ["already_claimed", "claimed_now"]

            complete_body = WorkerUnit2FinalizeRequest(
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                outcome="complete",
                checkpoint_version="workday_unit2_v1",
            )
            review_body = WorkerUnit2FinalizeRequest(
                attempt_id=lease.attempt_id,
                lease_id=lease.lease_id,
                outcome="review_required",
                hold_code="unknown_page_state",
            )

            async def _finalize(body: WorkerUnit2FinalizeRequest) -> tuple[str, Any]:
                async with sessions() as session:
                    try:
                        response = await worker_finalize_unit2_application(
                            application_id=application_id,
                            body=body,
                            worker_user=worker_user,
                            db=session,
                        )
                        return "ok", response.status
                    except HTTPException as exc:
                        return "error", exc.status_code

            terminal_results = await _run_simultaneously(
                lambda: _finalize(complete_body),
                lambda: _finalize(review_body),
            )
            successes = [result for result in terminal_results if result[0] == "ok"]
            conflicts = [result for result in terminal_results if result[0] == "error"]
            assert len(successes) == 1
            assert conflicts == [("error", 409)]
            terminal_status = successes[0][1]
            assert terminal_status in {"completed", "review_required"}

            winning_body = (
                complete_body if terminal_status == "completed" else review_body
            )
            losing_body = (
                review_body if terminal_status == "completed" else complete_body
            )
            assert await _finalize(winning_body) == ("ok", terminal_status)
            assert await _finalize(losing_body) == ("error", 409)

            async with sessions() as session:
                attempt = await session.get(WorkdayUnit2Attempt, lease.attempt_id)
                application = await session.get(JobApplication, application_id)
                assert attempt is not None
                assert application is not None
                assert attempt.save_claim_count == 1
                assert attempt.status == terminal_status
                assert application.automation_lease_id is None

                save_receipts = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationAutomationEvent)
                    .where(
                        ApplicationAutomationEvent.application_id == application_id,
                        ApplicationAutomationEvent.event_type
                        == "workday_unit2_save_claimed",
                    )
                )
                completion_receipts = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationAutomationEvent)
                    .where(
                        ApplicationAutomationEvent.application_id == application_id,
                        ApplicationAutomationEvent.event_type
                        == "workday_unit2_completed",
                    )
                )
                review_receipts = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationAutomationEvent)
                    .where(
                        ApplicationAutomationEvent.application_id == application_id,
                        ApplicationAutomationEvent.event_type
                        == "workday_unit2_review_required",
                    )
                )
                open_holds = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationHold)
                    .where(
                        ApplicationHold.application_id == application_id,
                        ApplicationHold.status == "open",
                    )
                )

                assert save_receipts == 1
                if terminal_status == "completed":
                    assert (completion_receipts, review_receipts, open_holds) == (
                        1,
                        0,
                        0,
                    )
                else:
                    assert (completion_receipts, review_receipts, open_holds) == (
                        0,
                        1,
                        1,
                    )

            recovery_user_id, recovery_application_id = (
                await _create_eligible_application(
                    sessions,
                    iteration=100 + iteration,
                )
            )
            recovery_worker_user = {"id": str(recovery_user_id)}

            async def _recovery_lease() -> Any:
                async with sessions() as session:
                    return await worker_lease_next_unit2_application(
                        application_id=recovery_application_id,
                        worker_user=recovery_worker_user,
                        db=session,
                    )

            initial_recovery_results = await _run_simultaneously(
                _recovery_lease,
                _recovery_lease,
            )
            recovery_leases = [
                result.application
                for result in initial_recovery_results
                if result.application
            ]
            assert len(recovery_leases) == 1
            recovery_lease = recovery_leases[0]
            assert recovery_lease.mode == "normal"

            async with sessions() as session:
                claim_response = await worker_claim_unit2_save(
                    application_id=recovery_application_id,
                    body=WorkerUnit2SaveClaimRequest(
                        attempt_id=recovery_lease.attempt_id,
                        lease_id=recovery_lease.lease_id,
                    ),
                    worker_user=recovery_worker_user,
                    db=session,
                )
                assert claim_response.status == "claimed_now"

            expired_at = datetime.now(UTC) - timedelta(minutes=1)
            async with sessions() as session:
                recovery_application = await session.get(
                    JobApplication, recovery_application_id
                )
                recovery_attempt = await session.get(
                    WorkdayUnit2Attempt, recovery_lease.attempt_id
                )
                assert recovery_application is not None
                assert recovery_attempt is not None
                recovery_application.automation_lease_expires_at = expired_at
                recovery_attempt.lease_expires_at = expired_at
                await session.commit()

            stale_recovery_results = await _run_simultaneously(
                _recovery_lease,
                _recovery_lease,
            )
            assert all(result.application is None for result in stale_recovery_results)

            async with sessions() as session:
                recovery_application = await session.get(
                    JobApplication, recovery_application_id
                )
                recovery_attempt = await session.get(
                    WorkdayUnit2Attempt, recovery_lease.attempt_id
                )
                recovery_holds = list(
                    (
                        await session.execute(
                            select(ApplicationHold).where(
                                ApplicationHold.application_id
                                == recovery_application_id,
                                ApplicationHold.status == "open",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert recovery_application is not None
                assert recovery_attempt is not None
                assert recovery_application.status == ApplicationStatus.BLOCKED.value
                assert recovery_application.automation_lease_id is None
                assert recovery_attempt.status == "review_required"
                assert recovery_attempt.save_claim_count == 1
                assert len(recovery_holds) == 1
                recovery_hold_id = recovery_holds[0].id

            async with sessions() as session:
                retry_response = await retry_review_hold(
                    hold_id=recovery_hold_id,
                    current_user={"id": str(recovery_user_id)},
                    db=session,
                )
                assert (
                    retry_response["application_status"]
                    == ApplicationStatus.APPLYING.value
                )

            observe_results = await _run_simultaneously(
                _recovery_lease,
                _recovery_lease,
            )
            observe_leases = [
                result.application for result in observe_results if result.application
            ]
            assert len(observe_leases) == 1
            observe_lease = observe_leases[0]
            assert observe_lease.mode == "observe_only"
            assert observe_lease.attempt_id == recovery_lease.attempt_id

            async with sessions() as session:
                with pytest.raises(HTTPException) as claim_error:
                    await worker_claim_unit2_save(
                        application_id=recovery_application_id,
                        body=WorkerUnit2SaveClaimRequest(
                            attempt_id=observe_lease.attempt_id,
                            lease_id=observe_lease.lease_id,
                        ),
                        worker_user=recovery_worker_user,
                        db=session,
                    )
                assert claim_error.value.status_code == 409

            async with sessions() as session:
                complete_response = await worker_finalize_unit2_application(
                    application_id=recovery_application_id,
                    body=WorkerUnit2FinalizeRequest(
                        attempt_id=observe_lease.attempt_id,
                        lease_id=observe_lease.lease_id,
                        outcome="complete",
                        checkpoint_version="workday_unit2_v1",
                    ),
                    worker_user=recovery_worker_user,
                    db=session,
                )
                assert complete_response.status == "completed"

            async with sessions() as session:
                recovery_attempt = await session.get(
                    WorkdayUnit2Attempt, recovery_lease.attempt_id
                )
                assert recovery_attempt is not None
                assert recovery_attempt.mode == "observe_only"
                assert recovery_attempt.status == "completed"
                assert recovery_attempt.save_claim_count == 1
                recovery_attempt_count = await session.scalar(
                    select(func.count())
                    .select_from(WorkdayUnit2Attempt)
                    .where(
                        WorkdayUnit2Attempt.application_id == recovery_application_id
                    )
                )
                recovery_save_receipts = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationAutomationEvent)
                    .where(
                        ApplicationAutomationEvent.application_id
                        == recovery_application_id,
                        ApplicationAutomationEvent.event_type
                        == "workday_unit2_save_claimed",
                    )
                )
                recovery_completion_receipts = await session.scalar(
                    select(func.count())
                    .select_from(ApplicationAutomationEvent)
                    .where(
                        ApplicationAutomationEvent.application_id
                        == recovery_application_id,
                        ApplicationAutomationEvent.event_type
                        == "workday_unit2_completed",
                    )
                )
                assert recovery_attempt_count == 1
                assert recovery_save_receipts == 1
                assert recovery_completion_receipts == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
