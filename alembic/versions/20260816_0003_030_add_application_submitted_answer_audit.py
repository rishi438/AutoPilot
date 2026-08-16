"""Add exact submitted-answer audit records.

Revision ID: 20260816_030
Revises: 20260816_029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260816_030"
down_revision = "20260816_029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("application_submitted_answers"):
        op.create_table(
            "application_submitted_answers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "application_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("job_applications.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("answer", sa.Text(), nullable=False),
            sa.Column("answer_source", sa.String(30), nullable=False),
            sa.Column(
                "review_reasons",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
    indexes = {
        item["name"] for item in inspector.get_indexes("application_submitted_answers")
    }
    if "ix_submitted_answer_application_created" not in indexes:
        op.create_index(
            "ix_submitted_answer_application_created",
            "application_submitted_answers",
            ["application_id", "submitted_at"],
        )


def downgrade() -> None:
    op.drop_table("application_submitted_answers")
