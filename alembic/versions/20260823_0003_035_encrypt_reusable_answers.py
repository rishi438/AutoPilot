"""Encrypt reusable form answers at rest.

Revision ID: 20260823_035
Revises: 20260823_034
"""

import sqlalchemy as sa
from alembic import op

revision = "20260823_035"
down_revision = "20260823_034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from utils.encryption import encrypt_api_key

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("job_form_answers")}
    if "answer_encrypted" not in columns:
        op.add_column(
            "job_form_answers",
            sa.Column("answer_encrypted", sa.Text(), nullable=True),
        )

    rows = bind.execute(
        sa.text(
            "SELECT id, answer FROM job_form_answers "
            "WHERE answer_encrypted IS NULL AND answer <> ''"
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE job_form_answers "
                "SET answer = '', answer_encrypted = :answer_encrypted "
                "WHERE id = :answer_id"
            ),
            {
                "answer_id": row["id"],
                "answer_encrypted": encrypt_api_key(row["answer"]),
            },
        )


def downgrade() -> None:
    from utils.encryption import decrypt_api_key

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("job_form_answers")}
    if "answer_encrypted" not in columns:
        return

    rows = bind.execute(
        sa.text(
            "SELECT id, answer_encrypted FROM job_form_answers "
            "WHERE answer_encrypted IS NOT NULL"
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE job_form_answers SET answer = :answer WHERE id = :answer_id"
            ),
            {
                "answer_id": row["id"],
                "answer": decrypt_api_key(row["answer_encrypted"]),
            },
        )
    op.drop_column("job_form_answers", "answer_encrypted")
