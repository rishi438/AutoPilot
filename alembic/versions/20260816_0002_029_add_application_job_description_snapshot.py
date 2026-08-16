"""Store a safe job-description snapshot for queued applications.

Revision ID: 20260816_029
Revises: 20260816_028
"""

import sqlalchemy as sa
from alembic import op

revision = "20260816_029"
down_revision = "20260816_028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("job_applications")}
    if "job_description" not in existing:
        op.add_column(
            "job_applications", sa.Column("job_description", sa.Text(), nullable=True)
        )
    if "job_description_hash" not in existing:
        op.add_column(
            "job_applications",
            sa.Column("job_description_hash", sa.String(64), nullable=True),
        )
    if "job_description_captured_at" not in existing:
        op.add_column(
            "job_applications",
            sa.Column(
                "job_description_captured_at", sa.DateTime(timezone=True), nullable=True
            ),
        )


def downgrade() -> None:
    op.drop_column("job_applications", "job_description_captured_at")
    op.drop_column("job_applications", "job_description_hash")
    op.drop_column("job_applications", "job_description")
