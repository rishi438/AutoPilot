"""Add user job-search preferences and discovered public board vacancies.

Revision ID: 20260815_027
Revises: 20260814_026
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260815_027"
down_revision = "20260814_026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_job_search_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("primary_skills", postgresql.JSONB(), nullable=False),
        sa.Column("secondary_skills", postgresql.JSONB(), nullable=False),
        sa.Column("roles", postgresql.JSONB(), nullable=False),
        sa.Column("company_tiers", postgresql.JSONB(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("min_match_score", sa.Float(), nullable=False, server_default="0.65"),
        sa.Column(
            "require_review_before_apply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_user_job_search_preferences_user_id",
        "user_job_search_preferences",
        ["user_id"],
    )
    op.create_table(
        "job_discoveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("company_name", sa.String(500), nullable=False),
        sa.Column("company_tier", sa.String(30)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("location", sa.String(500)),
        sa.Column("job_url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("match_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("match_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="discovered"),
        sa.Column("failure_code", sa.String(80)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column(
            "discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "user_id", "source", "external_id", name="uq_user_job_discovery_source_id"
        ),
    )
    op.create_index("ix_job_discoveries_user_id", "job_discoveries", ["user_id"])
    op.create_index("ix_job_discoveries_status", "job_discoveries", ["status"])
    op.create_index(
        "ix_job_discoveries_discovered_at", "job_discoveries", ["discovered_at"]
    )
    op.create_index(
        "ix_discovery_user_status_score",
        "job_discoveries",
        ["user_id", "status", "match_score"],
    )


def downgrade() -> None:
    op.drop_table("job_discoveries")
    op.drop_table("user_job_search_preferences")
