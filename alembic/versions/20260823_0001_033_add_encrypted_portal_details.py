"""Add encrypted optional portal details.

Revision ID: 20260823_033
Revises: 20260820_032
"""

import sqlalchemy as sa
from alembic import op

revision = "20260823_033"
down_revision = "20260820_032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("user_profiles")}
    if "date_of_birth_encrypted" not in columns:
        op.add_column("user_profiles", sa.Column("date_of_birth_encrypted", sa.Text()))
    if "pan_encrypted" not in columns:
        op.add_column("user_profiles", sa.Column("pan_encrypted", sa.Text()))
    if "sensitive_portal_autofill_enabled" not in columns:
        op.add_column(
            "user_profiles",
            sa.Column(
                "sensitive_portal_autofill_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    op.drop_column("user_profiles", "sensitive_portal_autofill_enabled")
    op.drop_column("user_profiles", "pan_encrypted")
    op.drop_column("user_profiles", "date_of_birth_encrypted")
