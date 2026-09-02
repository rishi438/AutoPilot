import importlib

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from models.database import (
    JobApplication,
    WorkdayAccountGate,
    WorkdayAuthAttempt,
    WorkdayTransitionFamily,
    WorkdayTransitionVersion,
)
from services.workday_transition_contracts import WorkdayGateState


GATE_COLUMNS = {
    "id",
    "account_ref",
    "user_id",
    "portal_scope",
    "state",
    "generation",
    "cooldown_until",
    "user_min_until",
    "next_eligible_at",
    "created_at",
    "updated_at",
}
ATTEMPT_COLUMNS = {
    "id",
    "gate_id",
    "application_id",
    "generation",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "secret_accessed",
    "auth_submit_count",
    "llm_repair_count",
    "status",
    "created_at",
    "updated_at",
}


class _MigrationRecorder:
    def __init__(self) -> None:
        self.tables: dict[str, tuple[object, ...]] = {}
        self.columns: dict[str, set[str]] = {"job_applications": set()}
        self.foreign_keys: dict[str, tuple[str, str]] = {}
        self.indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.operations: list[tuple[str, str]] = []

    def create_table(self, name: str, *elements: object) -> None:
        assert name not in self.tables
        self.tables[name] = elements
        self.operations.append(("create_table", name))

    def add_column(self, table_name: str, column: object) -> None:
        name = getattr(column, "name")
        self.columns.setdefault(table_name, set()).add(name)
        self.operations.append(("add_column", name))

    def create_foreign_key(
        self,
        name: str,
        source: str,
        referent: str,
        _local_columns: list[str],
        _remote_columns: list[str],
        **_kwargs: object,
    ) -> None:
        self.foreign_keys[name] = (source, referent)
        self.operations.append(("create_foreign_key", name))

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        **kwargs: object,
    ) -> None:
        self.indexes[name] = (table_name, tuple(columns), kwargs)
        self.operations.append(("create_index", name))

    def drop_index(self, name: str, *, table_name: str) -> None:
        assert self.indexes[name][0] == table_name
        del self.indexes[name]
        self.operations.append(("drop_index", name))

    def drop_table(self, name: str) -> None:
        del self.tables[name]
        self.operations.append(("drop_table", name))

    def drop_constraint(self, name: str, table_name: str, *, type_: str) -> None:
        assert type_ == "foreignkey"
        assert self.foreign_keys[name][0] == table_name
        del self.foreign_keys[name]
        self.operations.append(("drop_constraint", name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.columns[table_name].remove(column_name)
        self.operations.append(("drop_column", column_name))


def _migration_module():
    alembic_package = importlib.import_module("alembic")
    if not hasattr(alembic_package, "op"):
        alembic_package.op = object()
    return importlib.import_module(
        "alembic.versions.20260828_0001_041_add_workday_account_gate"
    )


def _record_upgrade(monkeypatch) -> tuple[object, _MigrationRecorder]:
    migration = _migration_module()
    recorder = _MigrationRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    return migration, recorder


def test_gate_identity_is_user_account_and_portal_scoped_without_email() -> None:
    assert set(WorkdayAccountGate.__table__.columns.keys()) == GATE_COLUMNS
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in WorkdayAccountGate.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert unique_constraints["uq_workday_account_gate_identity"] == (
        "user_id",
        "account_ref",
        "portal_scope",
    )
    assert {"email", "password", "credential", "secret"}.isdisjoint(GATE_COLUMNS)


def test_gate_and_attempt_fields_match_private_persistence_contract() -> None:
    assert set(WorkdayAuthAttempt.__table__.columns.keys()) == ATTEMPT_COLUMNS
    gate_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in WorkdayAccountGate.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    expected_states = ", ".join(f"'{state.value}'" for state in WorkdayGateState)

    assert gate_checks["ck_workday_account_gate_state"] == (
        f"state IN ({expected_states})"
    )
    assert WorkdayAccountGate.__table__.c.state.default.arg == "open"
    assert WorkdayAccountGate.__table__.c.generation.default.arg == 0
    for column_name in (
        "cooldown_until",
        "user_min_until",
        "next_eligible_at",
        "created_at",
        "updated_at",
    ):
        assert WorkdayAccountGate.__table__.c[column_name].type.timezone is True
    for column_name in (
        "lease_expires_at",
        "heartbeat_at",
        "created_at",
        "updated_at",
    ):
        assert WorkdayAuthAttempt.__table__.c[column_name].type.timezone is True


def test_only_one_active_attempt_is_allowed_per_gate() -> None:
    active_index = next(
        index
        for index in WorkdayAuthAttempt.__table__.indexes
        if index.name == "uq_workday_auth_attempt_one_active_per_gate"
    )
    ddl = str(CreateIndex(active_index).compile(dialect=postgresql.dialect()))

    assert active_index.unique is True
    assert tuple(column.name for column in active_index.columns) == ("gate_id",)
    assert "WHERE status = 'active'" in ddl


def test_attempt_defaults_and_bounded_monotonic_counters() -> None:
    columns = WorkdayAuthAttempt.__table__.c
    assert columns.secret_accessed.default.arg is False
    assert columns.auth_submit_count.default.arg == 0
    assert columns.llm_repair_count.default.arg == 0
    assert columns.status.default.arg == "active"

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in WorkdayAuthAttempt.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks["ck_workday_auth_attempt_submit_count"] == (
        "auth_submit_count BETWEEN 0 AND 1"
    )
    assert checks["ck_workday_auth_attempt_llm_repair_count"] == (
        "llm_repair_count BETWEEN 0 AND 1"
    )


def test_job_application_has_nullable_indexed_gate_foreign_key() -> None:
    column = JobApplication.__table__.c.workday_account_gate_id
    foreign_key = next(iter(column.foreign_keys))

    assert column.nullable is True
    assert column.index is True
    assert foreign_key.target_fullname == "workday_account_gates.id"
    assert foreign_key.ondelete == "SET NULL"


def test_shared_catalog_remains_free_of_private_gate_references() -> None:
    prohibited = {
        "user_id",
        "application_id",
        "account_ref",
        "gate_id",
        "workday_account_gate_id",
        "lease_token",
        "secret_accessed",
    }
    family_columns = set(WorkdayTransitionFamily.__table__.columns.keys())
    version_columns = set(WorkdayTransitionVersion.__table__.columns.keys())
    assert family_columns.isdisjoint(prohibited)
    assert version_columns.isdisjoint(prohibited)


def test_migration_upgrade_and_downgrade_are_reversible_in_isolation(
    monkeypatch,
) -> None:
    migration, recorder = _record_upgrade(monkeypatch)

    assert migration.revision == "20260828_041"
    assert migration.down_revision == "20260827_040"
    assert list(recorder.tables) == [
        "workday_account_gates",
        "workday_auth_attempts",
    ]
    assert recorder.columns["job_applications"] == {"workday_account_gate_id"}
    active_index = recorder.indexes["uq_workday_auth_attempt_one_active_per_gate"]
    assert active_index[0:2] == ("workday_auth_attempts", ("gate_id",))
    assert active_index[2]["unique"] is True
    assert str(active_index[2]["postgresql_where"]) == "status = 'active'"

    recorder.operations.clear()
    migration.downgrade()

    assert recorder.operations == [
        ("drop_index", "uq_workday_auth_attempt_one_active_per_gate"),
        ("drop_table", "workday_auth_attempts"),
        ("drop_index", "ix_job_applications_workday_account_gate_id"),
        ("drop_constraint", "fk_job_application_workday_account_gate"),
        ("drop_column", "workday_account_gate_id"),
        ("drop_table", "workday_account_gates"),
    ]
    assert recorder.tables == {}
    assert recorder.columns["job_applications"] == set()
    assert recorder.foreign_keys == {}
    assert recorder.indexes == {}


def test_migration_tables_define_named_foreign_keys_and_unique_lease_token(
    monkeypatch,
) -> None:
    _migration, recorder = _record_upgrade(monkeypatch)
    table_constraints = {
        constraint.name: constraint
        for elements in recorder.tables.values()
        for constraint in elements
        if isinstance(constraint, (ForeignKeyConstraint, UniqueConstraint))
    }

    assert {
        "fk_workday_account_gate_user",
        "fk_workday_auth_attempt_gate",
        "fk_workday_auth_attempt_application",
        "uq_workday_account_gate_identity",
        "uq_workday_auth_attempt_lease_token",
    } <= set(table_constraints)
    assert isinstance(
        table_constraints["uq_workday_auth_attempt_lease_token"], UniqueConstraint
    )
    assert all(
        not isinstance(index, Index)
        for elements in recorder.tables.values()
        for index in elements
    )
