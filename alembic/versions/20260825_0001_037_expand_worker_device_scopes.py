"""Allow account-gate and application-scoped Workday worker devices.

Revision ID: 20260825_037
Revises: 20260823_036
"""

from alembic import op

revision = "20260825_037"
down_revision = "20260823_036"
branch_labels = None
depends_on = None

_TABLE = "automation_worker_devices"
_OLD_CONSTRAINT = "ck_worker_device_workday_account_gate_scope"
_NEW_CONSTRAINT = "ck_worker_device_scope"


def upgrade() -> None:
    op.drop_constraint(_OLD_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _NEW_CONSTRAINT,
        _TABLE,
        "scope IN ('workday_account_gate', 'workday_application')",
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _OLD_CONSTRAINT,
        _TABLE,
        "scope = 'workday_account_gate'",
    )
