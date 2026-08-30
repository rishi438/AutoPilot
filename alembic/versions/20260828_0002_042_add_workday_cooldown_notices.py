"""Add private Workday cooldown notices.

Revision ID: 20260828_042
Revises: 20260828_041
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260828_042"
down_revision = "20260828_041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workday_cooldown_notices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("gate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gate_generation", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="pending"
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            ["gate_id"],
            ["workday_account_gates.id"],
            name="fk_workday_cooldown_notice_gate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["job_applications.id"],
            name="fk_workday_cooldown_notice_application",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "gate_id",
            "gate_generation",
            name="uq_workday_cooldown_notice_generation",
        ),
        sa.CheckConstraint(
            "gate_generation >= 0",
            name="ck_workday_cooldown_notice_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'kept', 'extended', 'deleted')",
            name="ck_workday_cooldown_notice_status",
        ),
    )
    op.create_index(
        "ix_workday_cooldown_notice_application",
        "workday_cooldown_notices",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workday_cooldown_notice_application",
        table_name="workday_cooldown_notices",
    )
    op.drop_table("workday_cooldown_notices")
