"""Store the calling code derived from the selected profile country.

Revision ID: 20260827_039
Revises: 20260826_038
"""

import sqlalchemy as sa
from alembic import op

revision = "20260827_039"
down_revision = "20260826_038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_profiles",
        sa.Column("country_phone_code", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "country_phone_code")
