"""Store reviewed application-form answers before portal submission.

Revision ID: 20260820_031
Revises: 20260816_030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260820_031"
down_revision = "20260816_030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("application_draft_answers"):
        op.create_table(
            "application_draft_answers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
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
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.UniqueConstraint(
                "application_id", "question", name="uq_application_draft_question"
            ),
        )
    indexes = {
        item["name"] for item in inspector.get_indexes("application_draft_answers")
    }
    if "ix_draft_answer_application_updated" not in indexes:
        op.create_index(
            "ix_draft_answer_application_updated",
            "application_draft_answers",
            ["application_id", "updated_at"],
        )


def downgrade() -> None:
    op.drop_table("application_draft_answers")
