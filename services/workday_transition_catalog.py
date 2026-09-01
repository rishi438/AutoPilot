"""Event-driven CURRENT and history storage for safe Workday transitions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import WorkdayTransitionFamily, WorkdayTransitionVersion
from services.workday_transition_contracts import (
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)

_LOCATOR_FIELDS: Final = frozenset(
    {
        "schema_version",
        "semantic_role",
        "intent_key",
        "scope_key",
        "require_unique",
    }
)
_SEMANTIC_ROLES: Final = frozenset({"button", "link"})
_INTENT_KEYS: Final = frozenset(
    {
        "apply",
        "apply_manually",
        "open_registration",
        "sign_in",
        "create_account",
        "next_application_step",
    }
)
_SCOPE_KEYS: Final = frozenset({"page", "active_dialog", "active_account_form"})


class WorkdayTransitionCatalogError(RuntimeError):
    """Base error for rejected catalog operations."""


class WorkdayTransitionFamilyNotFound(WorkdayTransitionCatalogError):
    """Raised when an append targets no shared transition family."""


class UnsafeWorkdayLocatorStrategy(WorkdayTransitionCatalogError, ValueError):
    """Raised when a locator strategy is not a bounded structural recipe."""


class WorkdayTransitionCatalog(Protocol):
    """Storage contract consumed by transition replay and repair services."""

    async def get_current_and_history(
        self, key: WorkdayTransitionKey, limit: int
    ) -> Sequence[WorkdayTransitionRecipe]: ...

    async def ensure_family(self, key: WorkdayTransitionKey) -> UUID: ...

    async def append_verified_and_set_current(
        self,
        *,
        family_id: UUID,
        safe_locator_strategy: Mapping[str, object],
        expected_to_state: WorkdayTransitionState,
        risk: WorkdayTransitionRisk,
        signature_version: int,
        executor_policy_version: int,
    ) -> WorkdayTransitionRecipe: ...

    async def rollback_current(
        self, *, family_id: UUID, version_id: UUID, expected_current_id: UUID
    ) -> bool: ...

    async def quarantine_version(
        self, *, family_id: UUID, version_id: UUID
    ) -> bool: ...

    async def retire_version(self, *, family_id: UUID, version_id: UUID) -> bool: ...


def validate_safe_locator_strategy(
    strategy: Mapping[str, object],
) -> dict[str, str | int | bool]:
    """Return a canonical locator recipe or reject every non-allowlisted shape."""
    if not isinstance(strategy, Mapping) or set(strategy) != _LOCATOR_FIELDS:
        raise UnsafeWorkdayLocatorStrategy(
            "Locator strategy must contain exactly the allowlisted structural fields."
        )
    if type(strategy["schema_version"]) is not int or strategy["schema_version"] != 1:
        raise UnsafeWorkdayLocatorStrategy("Locator schema version is unsupported.")
    if (
        type(strategy["semantic_role"]) is not str
        or strategy["semantic_role"] not in _SEMANTIC_ROLES
    ):
        raise UnsafeWorkdayLocatorStrategy("Locator semantic role is unsupported.")
    if (
        type(strategy["intent_key"]) is not str
        or strategy["intent_key"] not in _INTENT_KEYS
    ):
        raise UnsafeWorkdayLocatorStrategy("Locator intent key is unsupported.")
    if (
        type(strategy["scope_key"]) is not str
        or strategy["scope_key"] not in _SCOPE_KEYS
    ):
        raise UnsafeWorkdayLocatorStrategy("Locator scope key is unsupported.")
    if strategy["require_unique"] is not True:
        raise UnsafeWorkdayLocatorStrategy("Locator must require a unique match.")
    return {
        "schema_version": 1,
        "semantic_role": str(strategy["semantic_role"]),
        "intent_key": str(strategy["intent_key"]),
        "scope_key": str(strategy["scope_key"]),
        "require_unique": True,
    }


class SQLAlchemyWorkdayTransitionCatalog:
    """Async SQLAlchemy catalog with atomic append and CURRENT compare-and-set."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def ensure_family(self, key: WorkdayTransitionKey) -> UUID:
        """Atomically create or return one empty structural transition family."""
        async with self._session.begin():
            statement = (
                postgresql_insert(WorkdayTransitionFamily)
                .values(
                    visibility="shared_catalog",
                    portal_family=key.portal_family,
                    tenant_scope=key.tenant_scope,
                    task_type=key.task_type,
                    from_state_signature=key.from_state_signature,
                    action_intent=key.action_intent.value,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        "portal_family",
                        "tenant_scope",
                        "task_type",
                        "from_state_signature",
                        "action_intent",
                    )
                )
                .returning(WorkdayTransitionFamily.id)
            )
            family_id = (await self._session.execute(statement)).scalar_one_or_none()
            if family_id is not None:
                return family_id
            existing = await self._session.execute(
                select(WorkdayTransitionFamily.id).where(
                    WorkdayTransitionFamily.visibility == "shared_catalog",
                    WorkdayTransitionFamily.portal_family == key.portal_family,
                    WorkdayTransitionFamily.tenant_scope == key.tenant_scope,
                    WorkdayTransitionFamily.task_type == key.task_type,
                    WorkdayTransitionFamily.from_state_signature
                    == key.from_state_signature,
                    WorkdayTransitionFamily.action_intent == key.action_intent.value,
                )
            )
            existing_id = existing.scalar_one_or_none()
            if existing_id is None:
                raise WorkdayTransitionFamilyNotFound(
                    "Shared transition family could not be created or found."
                )
            return existing_id

    async def get_current_and_history(
        self, key: WorkdayTransitionKey, limit: int
    ) -> tuple[WorkdayTransitionRecipe, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100.")

        family_result = await self._session.execute(
            select(WorkdayTransitionFamily).where(
                WorkdayTransitionFamily.visibility == "shared_catalog",
                WorkdayTransitionFamily.portal_family == key.portal_family,
                WorkdayTransitionFamily.tenant_scope == key.tenant_scope,
                WorkdayTransitionFamily.task_type == key.task_type,
                WorkdayTransitionFamily.from_state_signature
                == key.from_state_signature,
                WorkdayTransitionFamily.action_intent == key.action_intent.value,
            )
        )
        family = family_result.scalar_one_or_none()
        if family is None or family.current_version_id is None:
            return ()

        versions_result = await self._session.execute(
            select(WorkdayTransitionVersion).where(
                WorkdayTransitionVersion.family_id == family.id
            )
        )
        versions = {version.id: version for version in versions_result.scalars().all()}
        recipes: list[WorkdayTransitionRecipe] = []
        visited: set[UUID] = set()
        version_id = family.current_version_id
        while version_id is not None and version_id not in visited:
            visited.add(version_id)
            version = versions.get(version_id)
            if version is None:
                break
            if (
                version.status == WorkdayTransitionStatus.VERIFIED.value
                and version.signature_version == key.signature_version
                and version.executor_policy_version == key.executor_policy_version
            ):
                recipes.append(_to_recipe(version))
                if len(recipes) == limit:
                    break
            version_id = version.parent_version_id
        return tuple(recipes)

    async def append_verified_and_set_current(
        self,
        *,
        family_id: UUID,
        safe_locator_strategy: Mapping[str, object],
        expected_to_state: WorkdayTransitionState,
        risk: WorkdayTransitionRisk,
        signature_version: int,
        executor_policy_version: int,
    ) -> WorkdayTransitionRecipe:
        locator = validate_safe_locator_strategy(safe_locator_strategy)
        _require_positive_version(signature_version, "Signature version")
        _require_positive_version(executor_policy_version, "Executor policy version")

        async with self._session.begin():
            family_result = await self._session.execute(
                select(WorkdayTransitionFamily)
                .where(WorkdayTransitionFamily.id == family_id)
                .with_for_update()
            )
            family = family_result.scalar_one_or_none()
            if family is None:
                raise WorkdayTransitionFamilyNotFound(
                    "Shared transition family was not found."
                )
            recipe_version_result = await self._session.execute(
                select(func.max(WorkdayTransitionVersion.recipe_version)).where(
                    WorkdayTransitionVersion.family_id == family_id
                )
            )
            recipe_version = (recipe_version_result.scalar_one_or_none() or 0) + 1
            version = WorkdayTransitionVersion(
                family_id=family_id,
                parent_version_id=family.current_version_id,
                safe_locator_strategy=locator,
                expected_to_state=expected_to_state.value,
                risk_class=risk.value,
                recipe_version=recipe_version,
                status=WorkdayTransitionStatus.VERIFIED.value,
                signature_version=signature_version,
                executor_policy_version=executor_policy_version,
            )
            self._session.add(version)
            await self._session.flush()
            family.current_version_id = version.id
            await self._session.flush()
        return _to_recipe(version)

    async def rollback_current(
        self, *, family_id: UUID, version_id: UUID, expected_current_id: UUID
    ) -> bool:
        verified_target = (
            select(WorkdayTransitionVersion.id)
            .where(
                WorkdayTransitionVersion.id == version_id,
                WorkdayTransitionVersion.family_id == family_id,
                WorkdayTransitionVersion.status
                == WorkdayTransitionStatus.VERIFIED.value,
            )
            .exists()
        )
        async with self._session.begin():
            result = await self._session.execute(
                update(WorkdayTransitionFamily)
                .where(
                    WorkdayTransitionFamily.id == family_id,
                    WorkdayTransitionFamily.current_version_id == expected_current_id,
                    verified_target,
                )
                .values(current_version_id=version_id)
                .returning(WorkdayTransitionFamily.id)
            )
        return result.scalar_one_or_none() is not None

    async def quarantine_version(self, *, family_id: UUID, version_id: UUID) -> bool:
        return await self._set_non_executable_status(
            family_id=family_id,
            version_id=version_id,
            status=WorkdayTransitionStatus.QUARANTINED,
        )

    async def retire_version(self, *, family_id: UUID, version_id: UUID) -> bool:
        return await self._set_non_executable_status(
            family_id=family_id,
            version_id=version_id,
            status=WorkdayTransitionStatus.RETIRED,
        )

    async def _set_non_executable_status(
        self,
        *,
        family_id: UUID,
        version_id: UUID,
        status: WorkdayTransitionStatus,
    ) -> bool:
        async with self._session.begin():
            result = await self._session.execute(
                update(WorkdayTransitionVersion)
                .where(
                    WorkdayTransitionVersion.id == version_id,
                    WorkdayTransitionVersion.family_id == family_id,
                    WorkdayTransitionVersion.status
                    == WorkdayTransitionStatus.VERIFIED.value,
                )
                .values(status=status.value)
                .returning(WorkdayTransitionVersion.id)
            )
        return result.scalar_one_or_none() is not None


def _require_positive_version(value: int, label: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer.")


def _to_recipe(version: WorkdayTransitionVersion) -> WorkdayTransitionRecipe:
    return WorkdayTransitionRecipe(
        version_id=version.id,
        family_id=version.family_id,
        parent_version_id=version.parent_version_id,
        safe_locator_strategy=validate_safe_locator_strategy(
            version.safe_locator_strategy
        ),
        expected_to_state=WorkdayTransitionState(version.expected_to_state),
        risk=WorkdayTransitionRisk(version.risk_class),
        status=WorkdayTransitionStatus(version.status),
        recipe_version=version.recipe_version,
        signature_version=version.signature_version,
        executor_policy_version=version.executor_policy_version,
    )
