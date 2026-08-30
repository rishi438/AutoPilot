import importlib
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from models.database import WorkdayCooldownNotice


NOTICE_COLUMNS = {
    "id",
    "gate_id",
    "application_id",
    "gate_generation",
    "status",
    "decided_at",
    "created_at",
    "updated_at",
}


class _IsolatedMigrationDatabase:
    def __init__(self) -> None:
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.metadata = sa.MetaData()
        sa.Table(
            "workday_account_gates",
            self.metadata,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        )
        sa.Table(
            "job_applications",
            self.metadata,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        )
        self.metadata.create_all(self.engine)
        self.operations: list[tuple[str, str]] = []

    def create_table(self, name: str, *elements: object) -> None:
        table = sa.Table(name, self.metadata, *elements)
        table.create(self.engine)
        self.operations.append(("create_table", name))

    def create_index(
        self, name: str, table_name: str, columns: list[str], **_kwargs: object
    ) -> None:
        table = self.metadata.tables[table_name]
        sa.Index(name, *(table.c[column] for column in columns)).create(self.engine)
        self.operations.append(("create_index", name))

    def drop_index(self, name: str, *, table_name: str) -> None:
        table = self.metadata.tables[table_name]
        index = next(item for item in table.indexes if item.name == name)
        index.drop(self.engine)
        table.indexes.remove(index)
        self.operations.append(("drop_index", name))

    def drop_table(self, name: str) -> None:
        table = self.metadata.tables[name]
        table.drop(self.engine)
        self.metadata.remove(table)
        self.operations.append(("drop_table", name))


def _migration_module():
    alembic_package = importlib.import_module("alembic")
    if not hasattr(alembic_package, "op"):
        alembic_package.op = object()
    return importlib.import_module(
        "alembic.versions.20260828_0002_042_add_workday_cooldown_notices"
    )


def test_notice_model_is_minimal_private_and_deduplicated() -> None:
    assert set(WorkdayCooldownNotice.__table__.columns.keys()) == NOTICE_COLUMNS
    assert {
        "email",
        "credential",
        "portal_text",
        "answer",
        "cookie",
        "browser_data",
        "account_ref",
    }.isdisjoint(NOTICE_COLUMNS)
    unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in WorkdayCooldownNotice.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_workday_cooldown_notice_generation"] == (
        "gate_id",
        "gate_generation",
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in WorkdayCooldownNotice.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_workday_cooldown_notice_status"] == (
        "status IN ('pending', 'kept', 'extended', 'deleted')"
    )


def test_notice_migration_upgrades_and_downgrades_isolated_database(
    monkeypatch,
) -> None:
    migration = _migration_module()
    database = _IsolatedMigrationDatabase()
    monkeypatch.setattr(migration, "op", database)

    migration.upgrade()

    assert migration.revision == "20260828_042"
    assert migration.down_revision == "20260828_041"
    inspector = sa.inspect(database.engine)
    assert "workday_cooldown_notices" in inspector.get_table_names()
    assert {
        item["name"]
        for item in inspector.get_unique_constraints("workday_cooldown_notices")
    } == {"uq_workday_cooldown_notice_generation"}

    database.operations.clear()
    migration.downgrade()

    assert database.operations == [
        ("drop_index", "ix_workday_cooldown_notice_application"),
        ("drop_table", "workday_cooldown_notices"),
    ]
    assert (
        "workday_cooldown_notices" not in sa.inspect(database.engine).get_table_names()
    )


def test_dashboard_offers_three_safe_actions_without_retry_now() -> None:
    html = Path("ui/dashboard/index.html").read_text(encoding="utf-8")
    javascript = Path("ui/static/js/dashboard-home.js").read_text(encoding="utf-8")
    cooldown_source = javascript[
        javascript.index("function renderWorkdayCooldownNotices") : javascript.index(
            "/** Render only server-provided hold text"
        )
    ]

    assert "workdayCooldownSection" in html
    assert "Keep default wait" in cooldown_source
    assert "Extend wait" in cooldown_source
    assert "Delete this application" in cooldown_source
    assert "safe_next_attempt_at" in cooldown_source
    assert "Retry now" not in cooldown_source
    assert "retry-now" not in cooldown_source
