"""Add private Workday account-gate persistence.

Revision ID: 20260828_041
Revises: 20260827_040
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260828_041"
down_revision = "20260827_040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workday_account_gates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_ref", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("portal_scope", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            nullable=False,
            server_default="open",
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_min_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
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
            ["user_id"],
            ["users.id"],
            name="fk_workday_account_gate_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "account_ref",
            "portal_scope",
            name="uq_workday_account_gate_identity",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'cooling_down', 'probe_in_progress', "
            "'auth_outcome_pending', 'review_required')",
            name="ck_workday_account_gate_state",
        ),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_workday_account_gate_generation_nonnegative",
        ),
    )

    op.add_column(
        "job_applications",
        sa.Column(
            "workday_account_gate_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_job_application_workday_account_gate",
        "job_applications",
        "workday_account_gates",
        ["workday_account_gate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_job_applications_workday_account_gate_id",
        "job_applications",
        ["workday_account_gate_id"],
    )

    op.create_table(
        "workday_auth_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("gate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "secret_accessed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "auth_submit_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("llm_repair_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
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
            ["gate_id"],
            ["workday_account_gates.id"],
            name="fk_workday_auth_attempt_gate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["job_applications.id"],
            name="fk_workday_auth_attempt_application",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("lease_token", name="uq_workday_auth_attempt_lease_token"),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_workday_auth_attempt_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "auth_submit_count BETWEEN 0 AND 1",
            name="ck_workday_auth_attempt_submit_count",
        ),
        sa.CheckConstraint(
            "llm_repair_count BETWEEN 0 AND 1",
            name="ck_workday_auth_attempt_llm_repair_count",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'abandoned', 'review_required')",
            name="ck_workday_auth_attempt_status",
        ),
    )
    op.create_index(
        "uq_workday_auth_attempt_one_active_per_gate",
        "workday_auth_attempts",
        ["gate_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workday_auth_attempt_one_active_per_gate",
        table_name="workday_auth_attempts",
    )
    op.drop_table("workday_auth_attempts")
    op.drop_index(
        "ix_job_applications_workday_account_gate_id",
        table_name="job_applications",
    )
    op.drop_constraint(
        "fk_job_application_workday_account_gate",
        "job_applications",
        type_="foreignkey",
    )
    op.drop_column("job_applications", "workday_account_gate_id")
    op.drop_table("workday_account_gates")
