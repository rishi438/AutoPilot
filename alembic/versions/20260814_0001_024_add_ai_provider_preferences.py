"""Add local or cloud AI provider preferences.

Revision ID: 20260814_024
Revises: 20260812_023
Create Date: 2026-08-14
"""

import sqlalchemy as sa

from alembic import op

revision = "20260814_024"
down_revision = "20260812_023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_workflow_preferences",
        sa.Column(
            "ai_provider", sa.String(length=16), nullable=False, server_default="cloud"
        ),
    )
    op.add_column(
        "user_workflow_preferences",
        sa.Column("local_model", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_workflow_preferences", "local_model")
    op.drop_column("user_workflow_preferences", "ai_provider")
