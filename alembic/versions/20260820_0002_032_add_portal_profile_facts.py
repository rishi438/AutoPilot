"""Add user-confirmed portal profile facts.

Revision ID: 20260820_032
Revises: 20260820_031
"""

import sqlalchemy as sa
from alembic import op

revision = "20260820_032"
down_revision = "20260820_031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("user_profiles")}
    if "nationality" not in columns:
        op.add_column("user_profiles", sa.Column("nationality", sa.String(100)))
    if "citizenship" not in columns:
        op.add_column("user_profiles", sa.Column("citizenship", sa.String(100)))


def downgrade() -> None:
    op.drop_column("user_profiles", "citizenship")
    op.drop_column("user_profiles", "nationality")
