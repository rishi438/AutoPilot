"""Add worker-neutral application automation records.

Revision ID: 20260816_028
Revises: 20260815_027
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260816_028"
down_revision = "20260815_027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    def add_column_if_missing(table: str, column: sa.Column) -> None:
        if column.name not in {item["name"] for item in inspector.get_columns(table)}:
            op.add_column(table, column)

    def create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
        if name not in {item["name"] for item in inspector.get_indexes(table)}:
            op.create_index(name, table, columns)

    if not inspector.has_table("application_automation_batches"):
        op.create_table(
            "application_automation_batches",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("worker_kind", sa.String(30), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
    create_index_if_missing(
        "ix_automation_batch_user_status",
        "application_automation_batches",
        ["user_id", "status"],
    )

    add_column_if_missing(
        "job_applications", sa.Column("portal", sa.String(50), nullable=True)
    )
    add_column_if_missing(
        "job_applications", sa.Column("external_job_id", sa.String(255), nullable=True)
    )
    add_column_if_missing(
        "job_applications", sa.Column("external_ats_url", sa.Text(), nullable=True)
    )
    add_column_if_missing(
        "job_applications",
        sa.Column(
            "automation_batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_automation_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    add_column_if_missing(
        "job_applications",
        sa.Column("automation_lease_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    add_column_if_missing(
        "job_applications",
        sa.Column(
            "automation_lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    create_index_if_missing(
        "ix_job_applications_portal", "job_applications", ["portal"]
    )
    create_index_if_missing(
        "ix_job_applications_automation_lease_id",
        "job_applications",
        ["automation_lease_id"],
    )
    create_index_if_missing(
        "ix_job_applications_automation_lease_expires_at",
        "job_applications",
        ["automation_lease_expires_at"],
    )
    create_index_if_missing(
        "ix_job_applications_automation_batch_id",
        "job_applications",
        ["automation_batch_id"],
    )

    add_column_if_missing(
        "job_form_answers",
        sa.Column("normalized_question", sa.String(240), nullable=True),
    )
    add_column_if_missing(
        "job_form_answers", sa.Column("field_type", sa.String(50), nullable=True)
    )
    add_column_if_missing(
        "job_form_answers",
        sa.Column(
            "sensitivity", sa.String(30), nullable=False, server_default="standard"
        ),
    )
    add_column_if_missing(
        "job_form_answers",
        sa.Column(
            "approved_for_reuse",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    add_column_if_missing(
        "job_form_answers", sa.Column("source_portal", sa.String(50), nullable=True)
    )
    add_column_if_missing(
        "job_form_answers",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    add_column_if_missing(
        "job_form_answers",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    create_index_if_missing(
        "ix_form_answer_user_normalized",
        "job_form_answers",
        ["user_id", "normalized_question"],
    )

    if not inspector.has_table("application_holds"):
        op.create_table(
            "application_holds",
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
            sa.Column("portal", sa.String(50)),
            sa.Column("question", sa.Text()),
            sa.Column("normalized_question", sa.String(240)),
            sa.Column("hold_code", sa.String(80), nullable=False),
            sa.Column("remediation", sa.Text(), nullable=False),
            sa.Column("error_detail", sa.Text()),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(30), nullable=False, server_default="open"),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
    create_index_if_missing(
        "ix_application_hold_user_status", "application_holds", ["user_id", "status"]
    )

    if not inspector.has_table("application_automation_events"):
        op.create_table(
            "application_automation_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "application_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("job_applications.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "batch_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("application_automation_batches.id", ondelete="SET NULL"),
            ),
            sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("detail", sa.Text()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )


def downgrade() -> None:
    op.drop_table("application_automation_events")
    op.drop_table("application_holds")
    op.drop_index("ix_form_answer_user_normalized", table_name="job_form_answers")
    for name in (
        "updated_at",
        "last_used_at",
        "source_portal",
        "approved_for_reuse",
        "sensitivity",
        "field_type",
        "normalized_question",
    ):
        op.drop_column("job_form_answers", name)
    op.drop_index(
        "ix_job_applications_automation_batch_id", table_name="job_applications"
    )
    op.drop_index("ix_job_applications_portal", table_name="job_applications")
    op.drop_index(
        "ix_job_applications_automation_lease_expires_at", table_name="job_applications"
    )
    op.drop_index(
        "ix_job_applications_automation_lease_id", table_name="job_applications"
    )
    for name in (
        "automation_lease_expires_at",
        "automation_lease_id",
        "automation_batch_id",
        "external_ats_url",
        "external_job_id",
        "portal",
    ):
        op.drop_column("job_applications", name)
    op.drop_table("application_automation_batches")
