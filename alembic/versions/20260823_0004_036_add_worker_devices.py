"""Add scoped and revocable local automation worker devices.

Revision ID: 20260823_036
Revises: 20260823_035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260823_036"
down_revision = "20260823_035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("automation_worker_devices"):
        return
    op.create_table(
        "automation_worker_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("scope", sa.String(80), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "scope = 'workday_account_gate'",
            name="ck_worker_device_workday_account_gate_scope",
        ),
    )
    op.create_index(
        "ix_automation_worker_devices_user_id",
        "automation_worker_devices",
        ["user_id"],
    )
    op.create_index(
        "ix_automation_worker_devices_expires_at",
        "automation_worker_devices",
        ["expires_at"],
    )
    op.create_index(
        "ix_worker_device_user_active",
        "automation_worker_devices",
        ["user_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("automation_worker_devices")
