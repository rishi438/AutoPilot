import asyncio
import inspect
from dataclasses import fields
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.database import WorkdayTransitionFamily, WorkdayTransitionVersion
from services.portal_control_resolver import PortalControlIntent
from services.workday_transition_catalog import (
    SQLAlchemyWorkdayTransitionCatalog,
    UnsafeWorkdayLocatorStrategy,
    validate_safe_locator_strategy,
)
from services.workday_transition_contracts import (
    WorkdayTransitionKey,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)


class _Result:
    def __init__(self, value: Any = None, values: list[Any] | None = None):
        self._value = value
        self._values = values or []

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._values


class _Transaction:
    def __init__(self, events: list[str]):
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("begin")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._events.append("rollback" if exc_type else "commit")


class _Session:
    def __init__(self, *results: _Result):
        self.results = list(results)
        self.statements: list[Any] = []
        self.events: list[str] = []
        self.added: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        self.events.append("execute")
        return self.results.pop(0)

    def begin(self) -> _Transaction:
        return _Transaction(self.events)

    def add(self, value: Any) -> None:
        self.added.append(value)
        self.events.append("add")

    async def flush(self) -> None:
        for value in self.added:
            if value.id is None:
                value.id = uuid4()
        self.events.append("flush")


def _locator(**overrides: object) -> dict[str, object]:
    locator: dict[str, object] = {
        "schema_version": 1,
        "semantic_role": "button",
        "intent_key": "sign_in",
        "scope_key": "active_dialog",
        "require_unique": True,
    }
    locator.update(overrides)
    return locator


def _key(*, signature_version: int = 1, executor_policy_version: int = 1):
    return WorkdayTransitionKey(
        portal_family="workday",
        tenant_scope="workday:tenant:site",
        task_type="open_existing_sign_in",
        from_state_signature="safe-signature",
        action_intent=PortalControlIntent.SIGN_IN,
        signature_version=signature_version,
        executor_policy_version=executor_policy_version,
    )


def _family(*, current_version_id: UUID | None) -> WorkdayTransitionFamily:
    return WorkdayTransitionFamily(
        id=uuid4(),
        portal_family="workday",
        tenant_scope="workday:tenant:site",
        task_type="open_existing_sign_in",
        from_state_signature="safe-signature",
        action_intent=PortalControlIntent.SIGN_IN.value,
        current_version_id=current_version_id,
    )


def _version(
    *,
    family_id: UUID,
    version_id: UUID | None = None,
    parent_version_id: UUID | None = None,
    recipe_version: int,
    status: WorkdayTransitionStatus = WorkdayTransitionStatus.VERIFIED,
    signature_version: int = 1,
    executor_policy_version: int = 1,
) -> WorkdayTransitionVersion:
    return WorkdayTransitionVersion(
        id=version_id or uuid4(),
        family_id=family_id,
        parent_version_id=parent_version_id,
        safe_locator_strategy=_locator(),
        expected_to_state=WorkdayTransitionState.LOGIN_FORM.value,
        risk_class=WorkdayTransitionRisk.NAVIGATION_ONLY.value,
        recipe_version=recipe_version,
        status=status.value,
        signature_version=signature_version,
        executor_policy_version=executor_policy_version,
    )


@pytest.mark.asyncio
async def test_empty_family_bootstrap_is_atomic_and_returns_created_id() -> None:
    family_id = uuid4()
    session = _Session(_Result(family_id))

    result = await SQLAlchemyWorkdayTransitionCatalog(
        session  # type: ignore[arg-type]
    ).ensure_family(_key())

    statement = str(session.statements[0].compile(dialect=postgresql.dialect())).upper()
    assert result == family_id
    assert "ON CONFLICT" in statement
    assert session.events == ["begin", "execute", "commit"]


@pytest.mark.asyncio
async def test_concurrent_empty_family_bootstrap_returns_existing_id() -> None:
    family_id = uuid4()
    session = _Session(_Result(None), _Result(family_id))

    result = await SQLAlchemyWorkdayTransitionCatalog(
        session  # type: ignore[arg-type]
    ).ensure_family(_key())

    assert result == family_id
    assert len(session.statements) == 2


@pytest.mark.asyncio
async def test_current_precedes_verified_parent_history_and_limit_keeps_rows() -> None:
    oldest_id, previous_id, current_id = uuid4(), uuid4(), uuid4()
    family = _family(current_version_id=current_id)
    versions = [
        _version(
            family_id=family.id,
            version_id=oldest_id,
            recipe_version=1,
        ),
        _version(
            family_id=family.id,
            version_id=previous_id,
            parent_version_id=oldest_id,
            recipe_version=2,
        ),
        _version(
            family_id=family.id,
            version_id=current_id,
            parent_version_id=previous_id,
            recipe_version=3,
        ),
    ]
    session = _Session(_Result(family), _Result(values=versions))

    recipes = await SQLAlchemyWorkdayTransitionCatalog(
        session  # type: ignore[arg-type]
    ).get_current_and_history(_key(), limit=2)

    assert [recipe.version_id for recipe in recipes] == [current_id, previous_id]
    assert len(versions) == 3
    assert not any(statement.is_update for statement in session.statements)


@pytest.mark.asyncio
async def test_non_executable_and_incompatible_versions_are_never_returned() -> None:
    ids = [uuid4() for _ in range(5)]
    family = _family(current_version_id=ids[4])
    versions = [
        _version(family_id=family.id, version_id=ids[0], recipe_version=1),
        _version(
            family_id=family.id,
            version_id=ids[1],
            parent_version_id=ids[0],
            recipe_version=2,
            executor_policy_version=2,
        ),
        _version(
            family_id=family.id,
            version_id=ids[2],
            parent_version_id=ids[1],
            recipe_version=3,
            signature_version=2,
        ),
        _version(
            family_id=family.id,
            version_id=ids[3],
            parent_version_id=ids[2],
            recipe_version=4,
            status=WorkdayTransitionStatus.RETIRED,
        ),
        _version(
            family_id=family.id,
            version_id=ids[4],
            parent_version_id=ids[3],
            recipe_version=5,
            status=WorkdayTransitionStatus.QUARANTINED,
        ),
    ]
    session = _Session(_Result(family), _Result(values=versions))

    recipes = await SQLAlchemyWorkdayTransitionCatalog(
        session  # type: ignore[arg-type]
    ).get_current_and_history(_key(), limit=10)

    assert [recipe.version_id for recipe in recipes] == [ids[0]]


@pytest.mark.asyncio
async def test_append_locks_family_creates_child_and_sets_current_atomically() -> None:
    previous_id = uuid4()
    family = _family(current_version_id=previous_id)
    session = _Session(_Result(family), _Result(7))
    catalog = SQLAlchemyWorkdayTransitionCatalog(session)  # type: ignore[arg-type]

    recipe = await catalog.append_verified_and_set_current(
        family_id=family.id,
        safe_locator_strategy=_locator(),
        expected_to_state=WorkdayTransitionState.LOGIN_FORM,
        risk=WorkdayTransitionRisk.NAVIGATION_ONLY,
        signature_version=1,
        executor_policy_version=1,
    )

    version = session.added[0]
    family_select = str(
        session.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    assert "FOR UPDATE" in family_select
    assert recipe.parent_version_id == previous_id
    assert recipe.recipe_version == 8
    assert recipe.status is WorkdayTransitionStatus.VERIFIED
    assert family.current_version_id == recipe.version_id == version.id
    assert session.events == [
        "begin",
        "execute",
        "execute",
        "add",
        "flush",
        "flush",
        "commit",
    ]


@pytest.mark.asyncio
async def test_atomic_rollback_accepts_verified_target_and_rejects_stale_cas() -> None:
    family_id, version_id, expected_id = uuid4(), uuid4(), uuid4()
    winning_session = _Session(_Result(family_id))
    stale_session = _Session(_Result())
    winning = SQLAlchemyWorkdayTransitionCatalog(
        winning_session  # type: ignore[arg-type]
    )
    stale = SQLAlchemyWorkdayTransitionCatalog(stale_session)  # type: ignore[arg-type]

    results = await asyncio.gather(
        winning.rollback_current(
            family_id=family_id,
            version_id=version_id,
            expected_current_id=expected_id,
        ),
        stale.rollback_current(
            family_id=family_id,
            version_id=version_id,
            expected_current_id=expected_id,
        ),
    )

    statement = str(winning_session.statements[0].compile(dialect=postgresql.dialect()))
    assert results == [True, False]
    assert "current_version_id" in statement
    assert "EXISTS" in statement
    assert "status" in statement
    assert winning_session.events == ["begin", "execute", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("quarantine_version", "retire_version"))
async def test_quarantine_and_retire_are_status_only_atomic_updates(
    operation: str,
) -> None:
    session = _Session(_Result(uuid4()))
    catalog = SQLAlchemyWorkdayTransitionCatalog(session)  # type: ignore[arg-type]

    changed = await getattr(catalog, operation)(family_id=uuid4(), version_id=uuid4())

    statement = session.statements[0]
    assert changed is True
    assert {column.name for column in statement._values} == {"status"}
    assert session.events == ["begin", "execute", "commit"]


@pytest.mark.parametrize(
    "strategy",
    (
        {},
        _locator(label="Sign in"),
        _locator(raw_page_text="portal text"),
        _locator(css="#dynamic-id"),
        _locator(xpath="//button"),
        _locator(script="alert(1)"),
        _locator(url="https://example.test"),
        _locator(secret="value"),
        _locator(user_id="private"),
        _locator(application_id="private"),
        _locator(schema_version=2),
        _locator(semantic_role=[]),
        _locator(semantic_role="input"),
        _locator(intent_key=[]),
        _locator(intent_key="free form"),
        _locator(scope_key=[]),
        _locator(scope_key="arbitrary"),
        _locator(require_unique=False),
    ),
)
def test_unsafe_locator_shapes_and_prohibited_fields_are_rejected(
    strategy: dict[str, object],
) -> None:
    with pytest.raises(UnsafeWorkdayLocatorStrategy):
        validate_safe_locator_strategy(strategy)


def test_catalog_has_no_score_counter_or_ranking_surface() -> None:
    prohibited_fragments = ("score", "counter", "success_count", "completion_count")
    model_fields = {
        field.name
        for model in (WorkdayTransitionFamily, WorkdayTransitionVersion)
        for field in model.__table__.columns
    }
    protocol_source = inspect.getsource(SQLAlchemyWorkdayTransitionCatalog).lower()

    assert not any(fragment in model_fields for fragment in prohibited_fragments)
    assert not any(fragment in protocol_source for fragment in prohibited_fragments)
