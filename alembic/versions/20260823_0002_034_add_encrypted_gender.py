"""Add encrypted user-entered gender for protected portal autofill.

Revision ID: 20260823_034
Revises: 20260823_033
"""

import sqlalchemy as sa
from alembic import op

revision = "20260823_034"
down_revision = "20260823_033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("user_profiles")}
    if "gender_encrypted" not in columns:
        op.add_column("user_profiles", sa.Column("gender_encrypted", sa.Text()))


def downgrade() -> None:
    op.drop_column("user_profiles", "gender_encrypted")
