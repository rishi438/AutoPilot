from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from services.portal_control_resolver import PortalControlIntent
from services.workday_transition_contracts import (
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)
from services.workday_transition_engine import (
    TransitionPreconditionFailed,
    TransitionRepairRequired,
    TransitionReplayStatus,
    WorkdayReplayBrowserActions,
    WorkdayTransitionReplayEngine,
)

SCOPE = "workday:tenant:site"
SIGNATURE = "wds1:safe"


def _key(
    intent: PortalControlIntent = PortalControlIntent.SIGN_IN,
) -> WorkdayTransitionKey:
    return WorkdayTransitionKey(
        portal_family="workday",
        tenant_scope=SCOPE,
        task_type="open_existing_sign_in",
        from_state_signature=SIGNATURE,
        action_intent=intent,
        signature_version=1,
        executor_policy_version=1,
    )


def _candidate(
    *, role: str = "button", intent: PortalControlIntent = PortalControlIntent.SIGN_IN
) -> WorkdaySafeCandidateMetadata:
    return WorkdaySafeCandidateMetadata(
        candidate_id="wdc-0123456789abcdef",
        semantic_role=role,
        intent_key=intent.value,
        scope_key="page",
    )


def _observed(
    state: WorkdayTransitionState,
    *,
    candidates: tuple[WorkdaySafeCandidateMetadata, ...] = (),
    scope: str = SCOPE,
) -> WorkdayObservedState:
    return WorkdayObservedState(
        state=state,
        safe_signature=(
            SIGNATURE if state is WorkdayTransitionState.ACCOUNT_PAGE else "next"
        ),
        signature_version=1,
        portal_family="workday",
        tenant_scope=scope,
        candidate_metadata=candidates,
    )


def _recipe(
    *,
    version_id: UUID | None = None,
    family_id: UUID | None = None,
    role: str = "button",
    locator_intent: str = "sign_in",
    status: WorkdayTransitionStatus = WorkdayTransitionStatus.VERIFIED,
    signature_version: int = 1,
    executor_policy_version: int = 1,
) -> WorkdayTransitionRecipe:
    return WorkdayTransitionRecipe(
        version_id=version_id or uuid4(),
        family_id=family_id or uuid4(),
        parent_version_id=None,
        safe_locator_strategy={
            "schema_version": 1,
            "semantic_role": role,
            "intent_key": locator_intent,
            "scope_key": "page",
            "require_unique": True,
        },
        expected_to_state=WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        risk=WorkdayTransitionRisk.AUTH_STRUCTURE,
        status=status,
        recipe_version=1,
        signature_version=signature_version,
        executor_policy_version=executor_policy_version,
    )


@dataclass
class _Observer:
    observations: list[WorkdayObservedState]
    include_candidates: list[bool] = field(default_factory=list)

    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState:
        self.include_candidates.append(include_candidates)
        return self.observations.pop(0)


@dataclass
class _Browser:
    fail: bool = False
    actions: list[tuple[str, PortalControlIntent]] = field(default_factory=list)
    vault_calls: int = 0
    credential_fills: int = 0
    auth_submits: int = 0
    llm_calls: int = 0

    async def execute_candidate(
        self, *, candidate_id: str, action_intent: PortalControlIntent
    ) -> None:
        self.actions.append((candidate_id, action_intent))
        if self.fail:
            raise RuntimeError("action result unknown")


@dataclass
class _Catalog:
    recipes: tuple[WorkdayTransitionRecipe, ...]
    family_id: UUID = field(default_factory=uuid4)
    cas_result: bool = True
    lookups: list[int] = field(default_factory=list)
    rollbacks: list[tuple[UUID, UUID, UUID]] = field(default_factory=list)
    ensured: list[WorkdayTransitionKey] = field(default_factory=list)

    async def get_current_and_history(
        self, key: WorkdayTransitionKey, limit: int
    ) -> tuple[WorkdayTransitionRecipe, ...]:
        self.lookups.append(limit)
        return self.recipes[:limit]

    async def ensure_family(self, key: WorkdayTransitionKey) -> UUID:
        self.ensured.append(key)
        return self.family_id

    async def rollback_current(
        self, *, family_id: UUID, version_id: UUID, expected_current_id: UUID
    ) -> bool:
        self.rollbacks.append((family_id, version_id, expected_current_id))
        return self.cas_result


def _engine(
    recipes: tuple[WorkdayTransitionRecipe, ...],
    *,
    post_state: WorkdayTransitionState = WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
    history_limit: int = 3,
    browser: _Browser | None = None,
    catalog: _Catalog | None = None,
) -> tuple[WorkdayTransitionReplayEngine, _Browser, _Catalog, _Observer]:
    observer = _Observer(
        [
            _observed(WorkdayTransitionState.ACCOUNT_PAGE, candidates=(_candidate(),)),
            _observed(post_state),
        ]
    )
    browser = browser or _Browser()
    catalog = catalog or _Catalog(recipes)
    return (
        WorkdayTransitionReplayEngine(
            observer=observer,
            browser_actions=browser,
            catalog=catalog,  # type: ignore[arg-type]
            history_limit=history_limit,
        ),
        browser,
        catalog,
        observer,
    )


@pytest.mark.asyncio
async def test_working_current_executes_once_without_extra_lookup_or_llm() -> None:
    current = _recipe()
    engine, browser, catalog, observer = _engine((current, _recipe()))

    outcome = await engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    assert outcome.status is TransitionReplayStatus.CURRENT_SUCCEEDED
    assert browser.actions == [(_candidate().candidate_id, PortalControlIntent.SIGN_IN)]
    assert browser.llm_calls == 0
    assert catalog.lookups == [3]
    assert catalog.rollbacks == []
    assert observer.include_candidates == [True, False]


@pytest.mark.asyncio
async def test_current_mismatch_tries_verified_history_in_order_and_updates_current() -> (
    None
):
    family_id = uuid4()
    current = _recipe(family_id=family_id, role="link")
    previous = _recipe(family_id=family_id)
    older = _recipe(family_id=family_id)
    engine, browser, catalog, _ = _engine((current, previous, older))

    outcome = await engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    assert outcome.status is TransitionReplayStatus.PREVIOUS_SUCCEEDED_CURRENT_UPDATED
    assert outcome.version_id == previous.version_id
    assert len(browser.actions) == 1
    assert catalog.rollbacks == [(family_id, previous.version_id, current.version_id)]


@pytest.mark.asyncio
async def test_history_limit_is_enforced_without_mutating_stored_rows() -> None:
    recipes = tuple(_recipe(role="link") for _ in range(4))
    catalog = _Catalog(recipes)
    engine, browser, _, _ = _engine(recipes, history_limit=2, catalog=catalog)

    with pytest.raises(TransitionRepairRequired):
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    assert catalog.lookups == [2]
    assert catalog.recipes == recipes
    assert browser.actions == []


@pytest.mark.asyncio
async def test_all_pre_action_mismatches_require_repair() -> None:
    engine, browser, catalog, _ = _engine((_recipe(role="link"), _recipe(role="link")))

    with pytest.raises(TransitionRepairRequired):
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    assert browser.actions == []
    assert catalog.rollbacks == []


@pytest.mark.asyncio
async def test_empty_catalog_issues_first_version_bootstrap_ticket() -> None:
    engine, browser, catalog, _ = _engine(())

    with pytest.raises(TransitionRepairRequired) as raised:
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    ticket = raised.value.ticket
    assert ticket is not None
    assert ticket.family_id == catalog.family_id
    assert ticket.stored_version_count == 0
    assert (
        ticket.expected_to_state is WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
    )
    assert ticket.risk is WorkdayTransitionRisk.AUTH_STRUCTURE
    assert catalog.ensured == [_key()]
    assert browser.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ("executor", "wrong_state"))
async def test_possible_action_never_falls_back_to_a_second_version(
    failure_mode: str,
) -> None:
    current, previous = _recipe(), _recipe()
    browser = _Browser(fail=failure_mode == "executor")
    engine, browser, catalog, _ = _engine(
        (current, previous),
        post_state=(
            WorkdayTransitionState.LOGIN_FORM
            if failure_mode == "wrong_state"
            else WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY
        ),
        browser=browser,
    )

    outcome = await engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    assert outcome.status in {
        TransitionReplayStatus.ACTION_OUTCOME_UNVERIFIED,
        TransitionReplayStatus.UNEXPECTED_NEXT_STATE,
    }
    assert outcome.action_may_have_happened is True
    assert len(browser.actions) == 1
    assert catalog.rollbacks == []


@pytest.mark.asyncio
async def test_non_executable_and_incompatible_versions_never_execute() -> None:
    recipes = (
        _recipe(status=WorkdayTransitionStatus.QUARANTINED),
        _recipe(status=WorkdayTransitionStatus.RETIRED),
        _recipe(signature_version=2),
        _recipe(executor_policy_version=2),
        _recipe(locator_intent="create_account"),
    )
    engine, browser, _, _ = _engine(recipes, history_limit=5)

    with pytest.raises(TransitionRepairRequired):
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    assert browser.actions == []


@pytest.mark.asyncio
async def test_portal_precondition_failure_does_not_load_or_mutate_catalog() -> None:
    catalog = _Catalog((_recipe(),))
    observer = _Observer(
        [
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
                scope="workday:other:site",
            )
        ]
    )
    browser = _Browser()
    engine = WorkdayTransitionReplayEngine(
        observer=observer,
        browser_actions=browser,
        catalog=catalog,  # type: ignore[arg-type]
        history_limit=2,
    )

    with pytest.raises(TransitionPreconditionFailed):
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    assert catalog.lookups == catalog.rollbacks == []
    assert browser.actions == []


@pytest.mark.asyncio
async def test_post_action_portal_failure_never_changes_current_or_history() -> None:
    current, previous = _recipe(), _recipe()
    engine, browser, catalog, observer = _engine((current, previous))
    observer.observations[1] = _observed(
        WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        scope="workday:other:site",
    )

    outcome = await engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    assert outcome.status is TransitionReplayStatus.UNEXPECTED_NEXT_STATE
    assert len(browser.actions) == 1
    assert catalog.rollbacks == []
    assert catalog.recipes == (current, previous)


@pytest.mark.asyncio
async def test_m4_has_no_secret_fill_or_authentication_submit_surface() -> None:
    engine, browser, _, _ = _engine((_recipe(),))

    await engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    protocol_members = set(inspect.get_annotations(WorkdayReplayBrowserActions))
    protocol_source = inspect.getsource(WorkdayReplayBrowserActions).lower()
    assert browser.vault_calls == browser.credential_fills == browser.auth_submits == 0
    assert protocol_members == set()
    assert "credential" not in protocol_source
    assert "fill_" not in protocol_source
    assert "submit_" not in protocol_source


def test_engine_has_no_statistical_ranking_surface() -> None:
    source = inspect.getsource(WorkdayTransitionReplayEngine).lower()
    prohibited = ("score", "popularity", "success_count", "completion_count")
    assert not any(fragment in source for fragment in prohibited)
