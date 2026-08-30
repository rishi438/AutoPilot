"""Shared ownership-checked application soft-delete transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import JobApplication, WorkflowSession, WorkflowStatusEnum


class ApplicationSoftDeleteNotFoundError(LookupError):
    """The active application is absent or does not belong to the user."""


@dataclass(frozen=True, slots=True)
class ApplicationSoftDeleteResult:
    application: JobApplication
    session_id: str | None


async def soft_delete_owned_application_in_transaction(
    session: AsyncSession,
    *,
    application_id: UUID,
    user_id: UUID,
    now: datetime | None = None,
) -> ApplicationSoftDeleteResult:
    """Soft-delete only one owned application without committing the transaction."""
    deleted_at = (now or datetime.now(UTC)).astimezone(UTC)
    application = (
        await session.execute(
            select(JobApplication)
            .where(
                JobApplication.id == application_id,
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if application is None:
        raise ApplicationSoftDeleteNotFoundError("Application not found.")

    application.deleted_at = deleted_at
    session_id = application.session_id
    if session_id:
        workflow_session = (
            await session.execute(
                select(WorkflowSession)
                .where(
                    and_(
                        WorkflowSession.session_id == session_id,
                        WorkflowSession.user_id == user_id,
                    )
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if workflow_session and workflow_session.workflow_status in {
            WorkflowStatusEnum.INITIALIZED.value,
            WorkflowStatusEnum.IN_PROGRESS.value,
            WorkflowStatusEnum.AWAITING_CONFIRMATION.value,
        }:
            workflow_session.workflow_status = WorkflowStatusEnum.CANCELLED.value
            workflow_session.processing_end_time = deleted_at
    return ApplicationSoftDeleteResult(application=application, session_id=session_id)
