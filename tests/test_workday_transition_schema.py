import importlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles

from models.database import WorkdayTransitionFamily, WorkdayTransitionVersion
from services.workday_transition_contracts import (
    WorkdayTransitionRisk,
    WorkdayTransitionStatus,
)


FAMILY_COLUMNS = {
    "id",
    "visibility",
    "portal_family",
    "tenant_scope",
    "task_type",
    "from_state_signature",
    "action_intent",
    "current_version_id",
    "created_at",
    "updated_at",
}
VERSION_COLUMNS = {
    "id",
    "family_id",
    "parent_version_id",
    "safe_locator_strategy",
    "expected_to_state",
    "risk_class",
    "recipe_version",
    "status",
    "signature_version",
    "executor_policy_version",
    "created_at",
}


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_isolated_schema(_type, _compiler, **_kwargs) -> str:
    return "JSON"


class _MigrationRecorder:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[object, ...]] = {}
        self.foreign_keys: dict[
            str, tuple[str, str, tuple[str, ...], tuple[str, ...]]
        ] = {}
        self.operations: list[tuple[str, str]] = []

    def create_table(self, name: str, *elements: object) -> None:
        assert name not in self.tables
        self.tables[name] = elements
        self.operations.append(("create_table", name))

    def create_foreign_key(
        self,
        name: str,
        source: str,
        referent: str,
        local_columns: list[str],
        remote_columns: list[str],
    ) -> None:
        assert source in self.tables and referent in self.tables
        self.foreign_keys[name] = (
            source,
            referent,
            tuple(local_columns),
            tuple(remote_columns),
        )
        self.operations.append(("create_foreign_key", name))

    def drop_constraint(self, name: str, table_name: str, *, type_: str) -> None:
        assert type_ == "foreignkey"
        assert self.foreign_keys[name][0] == table_name
        del self.foreign_keys[name]
        self.operations.append(("drop_constraint", name))

    def drop_table(self, name: str) -> None:
        inbound = [
            fk_name
            for fk_name, (
                source,
                referent,
                _local,
                _remote,
            ) in self.foreign_keys.items()
            if referent == name and source != name
        ]
        assert not inbound, f"inbound foreign keys remain: {inbound}"
        self.foreign_keys = {
            fk_name: details
            for fk_name, details in self.foreign_keys.items()
            if details[0] != name
        }
        del self.tables[name]
        self.operations.append(("drop_table", name))

    def build_sqlite_schema(self) -> sa.Engine:
        metadata = sa.MetaData()
        for table_name, elements in self.tables.items():
            sa.Table(table_name, metadata, *elements)
        for name, (
            source,
            referent,
            local_columns,
            remote_columns,
        ) in self.foreign_keys.items():
            metadata.tables[source].append_constraint(
                ForeignKeyConstraint(
                    local_columns,
                    [f"{referent}.{column}" for column in remote_columns],
                    name=name,
                )
            )

        engine = sa.create_engine("sqlite:///:memory:")

        @sa.event.listens_for(engine, "connect")
        def _enable_constraints(dbapi_connection, _connection_record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.create_function(
                "jsonb_typeof",
                1,
                lambda value: (
                    "object"
                    if isinstance(value, str) and value.lstrip().startswith("{")
                    else "other"
                ),
            )

        metadata.create_all(engine)
        return engine


def _migration_module():
    alembic_package = importlib.import_module("alembic")
    if not hasattr(alembic_package, "op"):
        alembic_package.op = object()
    return importlib.import_module(
        "alembic.versions.20260827_0002_040_add_workday_transition_catalog"
    )


def _record_upgrade(monkeypatch) -> tuple[object, _MigrationRecorder]:
    migration = _migration_module()
    recorder = _MigrationRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    return migration, recorder


def test_shared_catalog_models_expose_exact_intended_columns() -> None:
    assert set(WorkdayTransitionFamily.__table__.columns.keys()) == FAMILY_COLUMNS
    assert set(WorkdayTransitionVersion.__table__.columns.keys()) == VERSION_COLUMNS


def test_shared_catalog_models_expose_no_private_or_ranking_columns() -> None:
    prohibited = {
        "user_id",
        "application_id",
        "credential_id",
        "email",
        "answer",
        "token",
        "cookie",
        "resume_data",
        "raw_page_text",
        "success_count",
        "failure_count",
        "rank_score",
    }
    assert FAMILY_COLUMNS.isdisjoint(prohibited)
    assert VERSION_COLUMNS.isdisjoint(prohibited)


def test_family_and_version_uniqueness_constraints_exist() -> None:
    family_unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in WorkdayTransitionFamily.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    version_unique = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in WorkdayTransitionVersion.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert family_unique["uq_workday_transition_family_identity"] == (
        "portal_family",
        "tenant_scope",
        "task_type",
        "from_state_signature",
        "action_intent",
    )
    assert version_unique["uq_workday_transition_version_recipe"] == (
        "family_id",
        "recipe_version",
    )


def test_status_and_risk_constraints_match_contract_values() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in WorkdayTransitionVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    risk_values = ", ".join(f"'{member.value}'" for member in WorkdayTransitionRisk)
    status_values = ", ".join(f"'{member.value}'" for member in WorkdayTransitionStatus)
    assert checks["ck_workday_transition_version_risk"] == (
        f"risk_class IN ({risk_values})"
    )
    assert checks["ck_workday_transition_version_status"] == (
        f"status IN ({status_values})"
    )


def test_catalog_timestamps_are_timezone_aware() -> None:
    for column_name in ("created_at", "updated_at"):
        assert WorkdayTransitionFamily.__table__.c[column_name].type.timezone is True
    assert WorkdayTransitionVersion.__table__.c.created_at.type.timezone is True


def test_migration_upgrade_creates_tables_and_named_lineage_fks(monkeypatch) -> None:
    migration, recorder = _record_upgrade(monkeypatch)

    assert migration.revision == "20260827_040"
    assert migration.down_revision == "20260827_039"
    assert list(recorder.tables) == [
        "workday_transition_families",
        "workday_transition_versions",
    ]
    table_fk_names = {
        element.name
        for elements in recorder.tables.values()
        for element in elements
        if isinstance(element, ForeignKeyConstraint)
    }
    assert table_fk_names == {
        "fk_workday_transition_version_family",
        "fk_workday_transition_version_parent_same_family",
    }
    assert set(recorder.foreign_keys) == {
        "fk_workday_transition_family_current_same_family"
    }


def test_migration_downgrade_is_reversible_in_isolated_schema(monkeypatch) -> None:
    migration, recorder = _record_upgrade(monkeypatch)
    recorder.operations.clear()

    migration.downgrade()

    assert recorder.operations == [
        (
            "drop_constraint",
            "fk_workday_transition_family_current_same_family",
        ),
        ("drop_table", "workday_transition_versions"),
        ("drop_table", "workday_transition_families"),
    ]
    assert recorder.tables == {}
    assert recorder.foreign_keys == {}


def test_compatibility_versions_exist_only_on_immutable_version_rows() -> None:
    assert "signature_version" not in FAMILY_COLUMNS
    assert "executor_policy_version" not in FAMILY_COLUMNS
    assert {"signature_version", "executor_policy_version"} <= VERSION_COLUMNS
    assert "updated_at" not in VERSION_COLUMNS


def _seed_two_families(engine: sa.Engine) -> tuple[str, str, str]:
    family_a = str(uuid.uuid4())
    family_b = str(uuid.uuid4())
    version_b = str(uuid.uuid4())
    families = sa.table(
        "workday_transition_families",
        sa.column("id"),
        sa.column("visibility"),
        sa.column("portal_family"),
        sa.column("tenant_scope"),
        sa.column("task_type"),
        sa.column("from_state_signature"),
        sa.column("action_intent"),
    )
    versions = sa.table(
        "workday_transition_versions",
        sa.column("id"),
        sa.column("family_id"),
        sa.column("parent_version_id"),
        sa.column("safe_locator_strategy"),
        sa.column("expected_to_state"),
        sa.column("risk_class"),
        sa.column("recipe_version"),
        sa.column("status"),
        sa.column("signature_version"),
        sa.column("executor_policy_version"),
    )
    identity = {
        "visibility": "shared_catalog",
        "portal_family": "workday",
        "task_type": "open_existing_sign_in",
        "from_state_signature": "safe-signature",
        "action_intent": "sign_in_to_existing_account",
    }
    with engine.begin() as connection:
        connection.execute(
            families.insert(),
            [
                {"id": family_a, "tenant_scope": "workday:a:site", **identity},
                {"id": family_b, "tenant_scope": "workday:b:site", **identity},
            ],
        )
        connection.execute(
            versions.insert(),
            {
                "id": version_b,
                "family_id": family_b,
                "parent_version_id": None,
                "safe_locator_strategy": '{"schema_version":1}',
                "expected_to_state": "login_form",
                "risk_class": "navigation_only",
                "recipe_version": 1,
                "status": "verified",
                "signature_version": 1,
                "executor_policy_version": 1,
            },
        )
    return family_a, family_b, version_b


def test_cross_family_current_and_parent_references_are_rejected(monkeypatch) -> None:
    _migration, recorder = _record_upgrade(monkeypatch)
    engine = recorder.build_sqlite_schema()
    family_a, _family_b, version_b = _seed_two_families(engine)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE workday_transition_families "
                "SET current_version_id = ? WHERE id = ?",
                (version_b, family_a),
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workday_transition_versions "
                "(id, family_id, parent_version_id, safe_locator_strategy, "
                "expected_to_state, risk_class, recipe_version, status, "
                "signature_version, executor_policy_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    family_a,
                    version_b,
                    '{"schema_version":1}',
                    "login_form",
                    "navigation_only",
                    1,
                    "verified",
                    1,
                    1,
                ),
            )


@pytest.mark.parametrize(
    ("column_name", "invalid_value"),
    (
        ("risk_class", "unsafe"),
        ("status", "unknown"),
        ("safe_locator_strategy", "[]"),
    ),
)
def test_database_rejects_unsupported_recipe_values(
    monkeypatch, column_name: str, invalid_value: str
) -> None:
    _migration, recorder = _record_upgrade(monkeypatch)
    engine = recorder.build_sqlite_schema()
    family_a, _family_b, _version_b = _seed_two_families(engine)
    values = {
        "id": str(uuid.uuid4()),
        "family_id": family_a,
        "parent_version_id": None,
        "safe_locator_strategy": '{"schema_version":1}',
        "expected_to_state": "login_form",
        "risk_class": "navigation_only",
        "recipe_version": 1,
        "status": "verified",
        "signature_version": 1,
        "executor_policy_version": 1,
        column_name: invalid_value,
    }

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO workday_transition_versions "
                "(id, family_id, parent_version_id, safe_locator_strategy, "
                "expected_to_state, risk_class, recipe_version, status, "
                "signature_version, executor_policy_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values["id"],
                    values["family_id"],
                    values["parent_version_id"],
                    values["safe_locator_strategy"],
                    values["expected_to_state"],
                    values["risk_class"],
                    values["recipe_version"],
                    values["status"],
                    values["signature_version"],
                    values["executor_policy_version"],
                ),
            )
