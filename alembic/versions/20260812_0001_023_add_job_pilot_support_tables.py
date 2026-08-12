"""Add storage tables for future Job Pilot resume, form-answer, and portal-session features.

Revision ID: 20260812_023
Revises: 20260518_022
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260812_023"
down_revision = "20260518_022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resume_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_name", sa.String(length=100), nullable=False),
        sa.Column("job_title", sa.String(length=100), nullable=False),
        sa.Column("source_resume", sa.String(length=200), nullable=False),
        sa.Column("docx_path", sa.String(length=200), nullable=True),
        sa.Column("pdf_path", sa.String(length=200), nullable=True),
        sa.Column("ats_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("keywords_added", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_resume_versions_user_id", "resume_versions", ["user_id"])
    op.create_index("ix_resume_versions_job_title", "resume_versions", ["job_title"])
    op.create_index("ix_resume_versions_created_at", "resume_versions", ["created_at"])
    op.create_index("ix_resume_user_created", "resume_versions", ["user_id", "created_at"])
    op.create_index("ix_resume_user_company", "resume_versions", ["user_id", "company_name", "job_title"])

    op.create_table(
        "job_form_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question", sa.String(length=200), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_job_form_answers_user_id", "job_form_answers", ["user_id"])
    op.create_index("ix_job_form_answers_created_at", "job_form_answers", ["created_at"])
    op.create_index("ix_form_answer_user_question", "job_form_answers", ["user_id", "question"])
    op.create_index("ix_form_answer_user_created", "job_form_answers", ["user_id", "created_at"])

    op.create_table(
        "portal_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("portal_name", sa.String(length=50), nullable=False),
        sa.Column("storage_state_path", sa.String(length=500), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("portal_sessions")
    op.drop_table("job_form_answers")
    op.drop_table("resume_versions")
