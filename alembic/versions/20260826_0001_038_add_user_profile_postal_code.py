"""Add postal or PIN code to user profiles.

Revision ID: 20260826_038
Revises: 20260825_037
"""

import sqlalchemy as sa
from alembic import op

revision = "20260826_038"
down_revision = "20260825_037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profiles",
        sa.Column("postal_code", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "postal_code")
