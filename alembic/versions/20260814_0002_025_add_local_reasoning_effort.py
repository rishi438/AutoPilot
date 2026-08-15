"""Add GPT-OSS local reasoning preference.

Revision ID: 20260814_025
Revises: 20260814_024
Create Date: 2026-08-14
"""

import sqlalchemy as sa

from alembic import op

revision = "20260814_025"
down_revision = "20260814_024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_workflow_preferences",
        sa.Column(
            "local_reasoning_effort",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_workflow_preferences", "local_reasoning_effort")
