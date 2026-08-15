"""Allow fractional years of experience.

Revision ID: 20260814_026
Revises: 20260814_025
Create Date: 2026-08-14
"""

import sqlalchemy as sa

from alembic import op

revision = "20260814_026"
down_revision = "20260814_025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_profiles",
        "years_experience",
        type_=sa.Float(),
        postgresql_using="years_experience::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "user_profiles",
        "years_experience",
        type_=sa.Integer(),
        postgresql_using="round(years_experience)::integer",
    )
