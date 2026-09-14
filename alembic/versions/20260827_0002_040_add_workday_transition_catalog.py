"""Add the shared Workday transition catalog.

Revision ID: 20260827_040
Revises: 20260827_039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260827_040"
down_revision = "20260827_039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workday_transition_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "visibility",
            sa.String(length=32),
            nullable=False,
            server_default="shared_catalog",
        ),
        sa.Column("portal_family", sa.String(length=50), nullable=False),
        sa.Column("tenant_scope", sa.String(length=255), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("from_state_signature", sa.String(length=128), nullable=False),
        sa.Column("action_intent", sa.String(length=80), nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "visibility = 'shared_catalog'",
            name="ck_workday_transition_family_shared_visibility",
        ),
        sa.UniqueConstraint(
            "portal_family",
            "tenant_scope",
            "task_type",
            "from_state_signature",
            "action_intent",
            name="uq_workday_transition_family_identity",
        ),
    )

    op.create_table(
        "workday_transition_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("safe_locator_strategy", postgresql.JSONB(), nullable=False),
        sa.Column("expected_to_state", sa.String(length=64), nullable=False),
        sa.Column("risk_class", sa.String(length=32), nullable=False),
        sa.Column("recipe_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("signature_version", sa.Integer(), nullable=False),
        sa.Column("executor_policy_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["workday_transition_families.id"],
            name="fk_workday_transition_version_family",
        ),
        sa.UniqueConstraint(
            "family_id",
            "id",
            name="uq_workday_transition_version_family_id",
        ),
        sa.UniqueConstraint(
            "family_id",
            "recipe_version",
            name="uq_workday_transition_version_recipe",
        ),
        sa.ForeignKeyConstraint(
            ["family_id", "parent_version_id"],
            [
                "workday_transition_versions.family_id",
                "workday_transition_versions.id",
            ],
            name="fk_workday_transition_version_parent_same_family",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(safe_locator_strategy) = 'object'",
            name="ck_workday_transition_version_locator_object",
        ),
        sa.CheckConstraint(
            "risk_class IN ('navigation_only', 'auth_structure')",
            name="ck_workday_transition_version_risk",
        ),
        sa.CheckConstraint(
            "status IN ('verified', 'quarantined', 'retired')",
            name="ck_workday_transition_version_status",
        ),
    )

    op.create_foreign_key(
        "fk_workday_transition_family_current_same_family",
        "workday_transition_families",
        "workday_transition_versions",
        ["id", "current_version_id"],
        ["family_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workday_transition_family_current_same_family",
        "workday_transition_families",
        type_="foreignkey",
    )
    op.drop_table("workday_transition_versions")
    op.drop_table("workday_transition_families")
