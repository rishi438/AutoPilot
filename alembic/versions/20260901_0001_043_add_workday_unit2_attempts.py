"""Add workday_unit2_attempts table.

Revision ID: 20260901_043
Revises: 20260828_042
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260901_043"
down_revision = "20260828_042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workday_unit2_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="leased",
        ),
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default="normal",
        ),
        sa.Column(
            "save_claim_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "terminal_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["job_applications.id"],
            name="fk_workday_unit2_attempt_application_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "lease_id",
            name="uq_workday_unit2_attempt_lease_id",
        ),
        sa.CheckConstraint(
            "save_claim_count BETWEEN 0 AND 1",
            name="ck_workday_unit2_attempt_save_claim_count",
        ),
        sa.CheckConstraint(
            "status IN ('leased', 'save_claimed', 'review_required', 'released', 'completed')",
            name="ck_workday_unit2_attempt_status",
        ),
        sa.CheckConstraint(
            "mode IN ('normal', 'observe_only')",
            name="ck_workday_unit2_attempt_mode",
        ),
    )
    op.create_index(
        "uq_workday_unit2_attempt_one_active_per_application",
        "workday_unit2_attempts",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('leased', 'save_claimed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workday_unit2_attempt_one_active_per_application",
        table_name="workday_unit2_attempts",
        postgresql_where=sa.text("status IN ('leased', 'save_claimed')"),
    )
    op.drop_table("workday_unit2_attempts")
