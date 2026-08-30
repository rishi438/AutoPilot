from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass, field, replace
from uuid import UUID, uuid4

import pytest

from services.portal_control_resolver import (
    LocalLLMPortalControlResolver,
    PortalControlIntent,
    PortalControlSelection,
)
from services.workday_account_gate_store import WorkdayGateLease, WorkdayGateMutation
from services.workday_failure_router import (
    WorkdayFailureFacts,
    WorkdayFailureOutcome,
    route_workday_failure,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayObservedState,
    WorkdaySafeCandidateMetadata,
    WorkdayTransitionKey,
    WorkdayTransitionRecipe,
    WorkdayTransitionRisk,
    WorkdayTransitionState,
    WorkdayTransitionStatus,
)
from services.workday_transition_engine import (
    TransitionRepairRequired,
    TransitionRepairTicket,
    WorkdayTransitionReplayEngine,
)
from services.workday_transition_repair import (
    WorkdayRepairAttemptFacts,
    WorkdayTransitionRepairCoordinator,
    WorkdayTransitionRepairRequest,
    WorkdayTransitionRepairStatus,
)

SCOPE = "workday:tenant:site"
SIGNATURE = "wds1:safe"
CANDIDATE_ID = "wdc-0123456789abcdef"


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
    *,
    candidate_id: str = CANDIDATE_ID,
    role: str = "button",
    intent: PortalControlIntent = PortalControlIntent.SIGN_IN,
    scope: str = "page",
) -> WorkdaySafeCandidateMetadata:
    return WorkdaySafeCandidateMetadata(
        candidate_id=candidate_id,
        semantic_role=role,
        intent_key=intent.value,
        scope_key=scope,
    )


def _observed(
    state: WorkdayTransitionState,
    *,
    candidates: tuple[WorkdaySafeCandidateMetadata, ...] = (),
    tenant_scope: str = SCOPE,
) -> WorkdayObservedState:
    return WorkdayObservedState(
        state=state,
        safe_signature=(
            SIGNATURE if state is WorkdayTransitionState.ACCOUNT_PAGE else "next"
        ),
        signature_version=1,
        portal_family="workday",
        tenant_scope=tenant_scope,
        candidate_metadata=candidates,
    )


def _recipe(
    *,
    family_id: UUID,
    role: str = "link",
) -> WorkdayTransitionRecipe:
    return WorkdayTransitionRecipe(
        version_id=uuid4(),
        family_id=family_id,
        parent_version_id=None,
        safe_locator_strategy={
            "schema_version": 1,
            "semantic_role": role,
            "intent_key": "sign_in",
            "scope_key": "page",
            "require_unique": True,
        },
        expected_to_state=WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        risk=WorkdayTransitionRisk.AUTH_STRUCTURE,
        status=WorkdayTransitionStatus.VERIFIED,
        recipe_version=1,
        signature_version=1,
        executor_policy_version=1,
    )


def _ticket(*, family_id: UUID, key: WorkdayTransitionKey | None = None):
    return TransitionRepairTicket(
        key=key or _key(),
        expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE,
        family_id=family_id,
        expected_to_state=WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        risk=WorkdayTransitionRisk.AUTH_STRUCTURE,
        stored_version_count=1,
    )


def _lease() -> WorkdayGateLease:
    return WorkdayGateLease(
        gate_id=uuid4(),
        application_id=uuid4(),
        generation=1,
        lease_token="opaque-lease",
    )


def _route():
    return route_workday_failure(
        WorkdayFailureFacts(frozenset({WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH}))
    )


def _facts() -> WorkdayRepairAttemptFacts:
    return WorkdayRepairAttemptFacts(
        approved_https_origin=True,
        canonical_tenant_verified=True,
        stable_from_state=True,
    )


@dataclass
class _Observer:
    observations: list[WorkdayObservedState]

    async def observe(
        self, *, include_candidates: bool = False
    ) -> WorkdayObservedState:
        return self.observations.pop(0)


@dataclass
class _Browser:
    actions: list[tuple[str, PortalControlIntent]] = field(default_factory=list)
    hydration_waits: list[int] = field(default_factory=list)
    fail: bool = False

    async def execute_candidate(
        self, *, candidate_id: str, action_intent: PortalControlIntent
    ) -> None:
        self.actions.append((candidate_id, action_intent))
        if self.fail:
            raise RuntimeError("uncertain action outcome")

    async def wait_for_hydration(self, milliseconds: int) -> None:
        self.hydration_waits.append(milliseconds)


@dataclass
class _Resolver:
    selection: PortalControlSelection | None = field(
        default_factory=lambda: PortalControlSelection(CANDIDATE_ID, 0.9)
    )
    calls: list[dict[str, object]] = field(default_factory=list)
    select_calls: int = 0

    async def select(self, intent, candidates):
        self.select_calls += 1
        return None

    async def select_repair(self, **kwargs: object) -> PortalControlSelection | None:
        self.calls.append(kwargs)
        return self.selection


@dataclass
class _Gate:
    claim_allowed: bool = True
    claims: int = 0
    holds: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def claim_llm_repair(self, lease: WorkdayGateLease) -> WorkdayGateMutation:
        async with self.lock:
            self.claims += 1
            if not self.claim_allowed:
                return WorkdayGateMutation(applied=False, stale=True)
            self.claim_allowed = False
            return WorkdayGateMutation(applied=True)

    async def mark_review_required(
        self, lease: WorkdayGateLease
    ) -> WorkdayGateMutation:
        self.holds += 1
        return WorkdayGateMutation(applied=True)


@dataclass
class _Catalog:
    recipes: tuple[WorkdayTransitionRecipe, ...]
    appends: list[dict[str, object]] = field(default_factory=list)

    async def get_current_and_history(
        self, key: WorkdayTransitionKey, limit: int
    ) -> tuple[WorkdayTransitionRecipe, ...]:
        return self.recipes[:limit]

    async def append_verified_and_set_current(
        self, **kwargs: object
    ) -> WorkdayTransitionRecipe:
        self.appends.append(kwargs)
        if self.recipes:
            return self.recipes[0]
        return WorkdayTransitionRecipe(
            version_id=uuid4(),
            family_id=kwargs["family_id"],
            parent_version_id=None,
            safe_locator_strategy=kwargs["safe_locator_strategy"],
            expected_to_state=kwargs["expected_to_state"],
            risk=kwargs["risk"],
            status=WorkdayTransitionStatus.VERIFIED,
            recipe_version=1,
            signature_version=kwargs["signature_version"],
            executor_policy_version=kwargs["executor_policy_version"],
        )

    async def rollback_current(self, **kwargs: object) -> bool:
        return False

    async def quarantine_version(self, **kwargs: object) -> bool:
        return False

    async def retire_version(self, **kwargs: object) -> bool:
        return False


def _setup(
    *,
    selection: PortalControlSelection | None = None,
    first_observed: WorkdayObservedState | None = None,
    next_observed: WorkdayObservedState | None = None,
    next_observations: tuple[WorkdayObservedState, ...] | None = None,
    gate: _Gate | None = None,
    browser: _Browser | None = None,
):
    family_id = uuid4()
    catalog = _Catalog((_recipe(family_id=family_id),))
    followup_observations = (
        next_observations
        if next_observations is not None
        else (
            next_observed
            or _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
        )
    )
    observer = _Observer(
        [
            first_observed
            or _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
            ),
            *followup_observations,
        ]
    )
    resolver = _Resolver(
        PortalControlSelection(CANDIDATE_ID, 0.9) if selection is None else selection
    )
    gate = gate or _Gate()
    browser = browser or _Browser()
    coordinator = WorkdayTransitionRepairCoordinator(
        observer=observer,
        browser_actions=browser,
        catalog=catalog,
        gate_store=gate,
        resolver=resolver,
        history_limit=3,
    )
    request = WorkdayTransitionRepairRequest(
        route=_route(),
        ticket=_ticket(family_id=family_id),
        gate_lease=_lease(),
        facts=_facts(),
    )
    return coordinator, request, resolver, gate, browser, catalog


@pytest.mark.asyncio
async def test_replay_issues_value_free_ticket_only_after_all_versions_mismatch() -> (
    None
):
    family_id = uuid4()
    browser = _Browser()
    engine = WorkdayTransitionReplayEngine(
        observer=_Observer(
            [
                _observed(
                    WorkdayTransitionState.ACCOUNT_PAGE,
                    candidates=(_candidate(),),
                )
            ]
        ),
        browser_actions=browser,
        catalog=_Catalog((_recipe(family_id=family_id),)),  # type: ignore[arg-type]
        history_limit=3,
    )

    with pytest.raises(TransitionRepairRequired) as raised:
        await engine.replay(
            key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    assert raised.value.ticket == _ticket(family_id=family_id)
    assert browser.actions == []


@pytest.mark.asyncio
async def test_all_replay_mismatches_allow_one_repair_call_not_legacy_selection() -> (
    None
):
    family_id = uuid4()
    observer = _Observer(
        [
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
            ),
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
            ),
            _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
        ]
    )
    browser = _Browser()
    catalog = _Catalog((_recipe(family_id=family_id),))
    replay = WorkdayTransitionReplayEngine(
        observer=observer,
        browser_actions=browser,
        catalog=catalog,
        history_limit=3,
    )
    key = _key()

    with pytest.raises(TransitionRepairRequired) as raised:
        await replay.replay(
            key=key, expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
        )

    resolver = _Resolver()
    gate = _Gate()
    coordinator = WorkdayTransitionRepairCoordinator(
        observer=observer,
        browser_actions=browser,
        catalog=catalog,
        gate_store=gate,
        resolver=resolver,
        history_limit=3,
    )
    outcome = await coordinator.repair(
        WorkdayTransitionRepairRequest(
            route=_route(),
            ticket=raised.value.ticket,
            gate_lease=_lease(),
            facts=_facts(),
        )
    )

    assert outcome.status is WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED
    assert resolver.select_calls == 0
    assert len(resolver.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    (
        {"route": replace(_route(), outcome=WorkdayFailureOutcome.SAFE_HOLD)},
        {"ticket": replace(_ticket(family_id=uuid4()), all_versions_mismatched=False)},
        {"ticket": replace(_ticket(family_id=uuid4()), action_may_have_happened=True)},
        {"facts": replace(_facts(), approved_https_origin=False)},
        {"facts": replace(_facts(), canonical_tenant_verified=False)},
        {"facts": replace(_facts(), stable_from_state=False)},
        {"facts": replace(_facts(), secret_accessed=True)},
        {"facts": replace(_facts(), auth_submit_count=1)},
        {"facts": replace(_facts(), irreversible_action_taken=True)},
        {"facts": replace(_facts(), llm_repair_count=1)},
    ),
)
async def test_each_false_eligibility_condition_prevents_llm_call(
    change: dict[str, object],
) -> None:
    coordinator, request, resolver, gate, browser, catalog = _setup()
    request = replace(request, **change)

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.INELIGIBLE
    assert resolver.calls == []
    assert gate.claims == gate.holds == 0
    assert browser.actions == catalog.appends == []


@pytest.mark.asyncio
async def test_concurrent_repairs_call_llm_at_most_once_per_attempt() -> None:
    gate = _Gate()
    first = _setup(gate=gate)
    second = _setup(gate=gate)
    second_request = replace(second[1], gate_lease=first[1].gate_lease)

    outcomes = await asyncio.gather(
        first[0].repair(first[1]), second[0].repair(second_request)
    )

    assert sum(len(resolver.calls) for resolver in (first[2], second[2])) == 1
    assert {outcome.status for outcome in outcomes} == {
        WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED,
        WorkdayTransitionRepairStatus.CLAIM_REJECTED,
    }


@pytest.mark.asyncio
async def test_model_input_contains_only_bounded_structural_fields() -> None:
    coordinator, request, resolver, _, _, _ = _setup()

    await coordinator.repair(request)

    call = resolver.calls[0]
    assert set(call) == {"intent", "expected_next_state", "candidates"}
    candidate_payload = asdict(call["candidates"][0])  # type: ignore[index]
    assert set(candidate_payload) == {
        "candidate_id",
        "semantic_role",
        "semantic_name",
        "scope_key",
    }
    serialized = json.dumps(
        {
            "intent": call["intent"].value,  # type: ignore[union-attr]
            "expected_next_state": call["expected_next_state"],
            "candidates": [candidate_payload],
        }
    ).lower()
    assert not any(
        fragment in serialized
        for fragment in (
            "user_id",
            "application_id",
            "url",
            "html",
            "cookie",
            "token",
            "secret",
            "selector",
            "script",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "first_observed", "key"),
    (
        (PortalControlSelection("wdc-ffffffffffffffff", 0.9), None, None),
        (PortalControlSelection(CANDIDATE_ID, 0.49), None, None),
        (
            PortalControlSelection(CANDIDATE_ID, 0.9),
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
                tenant_scope="workday:other:site",
            ),
            None,
        ),
        (
            PortalControlSelection(CANDIDATE_ID, 0.9),
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(intent=PortalControlIntent.OPEN_REGISTRATION),),
            ),
            _key(PortalControlIntent.OPEN_REGISTRATION),
        ),
    ),
)
async def test_invalid_selection_unsafe_action_or_changed_origin_fails_closed(
    selection: PortalControlSelection,
    first_observed: WorkdayObservedState | None,
    key: WorkdayTransitionKey | None,
) -> None:
    coordinator, request, resolver, gate, browser, catalog = _setup(
        selection=selection, first_observed=first_observed
    )
    if key is not None:
        request = replace(request, ticket=replace(request.ticket, key=key))

    outcome = await coordinator.repair(request)

    assert outcome.status in {
        WorkdayTransitionRepairStatus.INELIGIBLE,
        WorkdayTransitionRepairStatus.SAFE_HOLD,
    }
    assert browser.actions == catalog.appends == []
    if outcome.status is WorkdayTransitionRepairStatus.SAFE_HOLD:
        assert gate.holds == 1


@pytest.mark.asyncio
async def test_accepted_output_executes_once_verifies_and_learns_current() -> None:
    coordinator, request, resolver, gate, browser, catalog = _setup()

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED
    assert browser.actions == [(CANDIDATE_ID, PortalControlIntent.SIGN_IN)]
    assert gate.claims == 1
    assert resolver.select_calls == 0
    assert len(resolver.calls) == 1
    assert gate.holds == 0
    assert len(catalog.appends) == 1
    learned = catalog.appends[0]
    assert learned == {
        "family_id": request.ticket.family_id,
        "safe_locator_strategy": {
            "schema_version": 1,
            "semantic_role": "button",
            "intent_key": "sign_in",
            "scope_key": "page",
            "require_unique": True,
        },
        "expected_to_state": WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        "risk": WorkdayTransitionRisk.AUTH_STRUCTURE,
        "signature_version": 1,
        "executor_policy_version": 1,
    }


@pytest.mark.asyncio
async def test_empty_catalog_bootstrap_executes_verifies_and_learns_first_current() -> (
    None
):
    family_id = uuid4()
    catalog = _Catalog(())
    observer = _Observer(
        [
            _observed(
                WorkdayTransitionState.ACCOUNT_PAGE,
                candidates=(_candidate(),),
            ),
            _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
        ]
    )
    resolver = _Resolver()
    gate = _Gate()
    browser = _Browser()
    coordinator = WorkdayTransitionRepairCoordinator(
        observer=observer,
        browser_actions=browser,
        catalog=catalog,
        gate_store=gate,
        resolver=resolver,
        history_limit=3,
    )
    request = WorkdayTransitionRepairRequest(
        route=_route(),
        ticket=replace(_ticket(family_id=family_id), stored_version_count=0),
        gate_lease=_lease(),
        facts=_facts(),
    )

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED
    assert gate.claims == 1
    assert browser.actions == [(CANDIDATE_ID, PortalControlIntent.SIGN_IN)]
    assert catalog.appends[0]["family_id"] == family_id


@pytest.mark.asyncio
async def test_login_form_hydrates_before_verification_and_learning() -> None:
    coordinator, request, _, gate, browser, catalog = _setup(
        next_observations=(
            _observed(WorkdayTransitionState.LOGIN_FORM),
            _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
        )
    )

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.VERIFIED_AND_LEARNED
    assert browser.hydration_waits == [250]
    assert gate.holds == 0
    assert len(catalog.appends) == 1


@pytest.mark.asyncio
async def test_persistent_login_form_holds_and_does_not_learn() -> None:
    coordinator, request, _, gate, browser, catalog = _setup(
        next_observations=tuple(
            _observed(WorkdayTransitionState.LOGIN_FORM) for _ in range(4)
        )
    )

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.SAFE_HOLD
    assert outcome.hold_applied is True
    assert len(browser.actions) == 1
    assert browser.hydration_waits == [250, 250, 250]
    assert gate.holds == 1
    assert catalog.appends == []


@pytest.mark.asyncio
async def test_login_form_hydration_stops_and_holds_on_pending_auth_condition() -> None:
    coordinator, request, _, gate, browser, catalog = _setup(
        next_observations=(
            _observed(WorkdayTransitionState.LOGIN_FORM),
            _observed(WorkdayTransitionState.AUTH_OUTCOME_PENDING),
        )
    )

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.SAFE_HOLD
    assert outcome.observed_state is WorkdayTransitionState.AUTH_OUTCOME_PENDING
    assert browser.hydration_waits == [250]
    assert gate.holds == 1
    assert catalog.appends == []


@pytest.mark.asyncio
async def test_learned_recipe_is_reusable_without_learning_user_data() -> None:
    coordinator, request, _, _, _, catalog = _setup()
    await coordinator.repair(request)
    locator = catalog.appends[0]["safe_locator_strategy"]
    learned = replace(
        catalog.recipes[0],
        safe_locator_strategy=locator,
        version_id=uuid4(),
    )
    other_browser = _Browser()
    other_engine = WorkdayTransitionReplayEngine(
        observer=_Observer(
            [
                _observed(
                    WorkdayTransitionState.ACCOUNT_PAGE,
                    candidates=(_candidate(),),
                ),
                _observed(WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY),
            ]
        ),
        browser_actions=other_browser,
        catalog=_Catalog((learned,)),  # type: ignore[arg-type]
        history_limit=1,
    )

    await other_engine.replay(
        key=_key(), expected_from_state=WorkdayTransitionState.ACCOUNT_PAGE
    )

    assert other_browser.actions == [(CANDIDATE_ID, PortalControlIntent.SIGN_IN)]
    assert not any(name in locator for name in ("user_id", "application_id", "gate_id"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_class",
    tuple(
        failure_class
        for failure_class in WorkdayFailureClass
        if failure_class is not WorkdayFailureClass.STRUCTURAL_DRIFT_PRE_AUTH
    ),
)
async def test_account_and_portal_failures_never_invoke_or_learn(
    failure_class: WorkdayFailureClass,
) -> None:
    coordinator, request, resolver, _, browser, catalog = _setup()
    submit_count = 1 if failure_class.value.startswith("post_submit_") else 0
    request = replace(
        request,
        route=route_workday_failure(
            WorkdayFailureFacts(frozenset({failure_class}), submit_count)
        ),
        facts=replace(request.facts, auth_submit_count=submit_count),
    )

    outcome = await coordinator.repair(request)

    assert outcome.status is WorkdayTransitionRepairStatus.INELIGIBLE
    assert resolver.calls == browser.actions == catalog.appends == []


class _PromptClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs: object) -> dict[str, object]:
        self.prompts.append(prompt)
        return {"candidate_id": CANDIDATE_ID, "confidence": 0.9}


@pytest.mark.asyncio
async def test_configured_resolver_repair_prompt_has_exact_safe_shape() -> None:
    coordinator, request, _, _, _, _ = _setup()
    client = _PromptClient()
    coordinator._resolver = LocalLLMPortalControlResolver(  # type: ignore[attr-defined]
        client, model="configured-local-model"
    )

    await coordinator.repair(request)

    prompt = json.loads(client.prompts[0])
    assert set(prompt) == {"intent", "expected_next_state", "candidates"}
    assert set(prompt["candidates"][0]) == {
        "candidate_id",
        "semantic_role",
        "semantic_name",
        "scope_key",
    }


def test_repair_has_no_statistical_ranking_surface() -> None:
    source = inspect.getsource(WorkdayTransitionRepairCoordinator).lower()
    prohibited = ("popularity", "success_count", "completion_count", "ranking")
    assert not any(fragment in source for fragment in prohibited)
