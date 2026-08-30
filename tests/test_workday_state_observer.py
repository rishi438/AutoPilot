from __future__ import annotations

from dataclasses import asdict, dataclass
import json

import pytest

from services.workday_state_observer import (
    MAX_OBSERVED_CONTROLS,
    WorkdayPageStructure,
    WorkdaySemanticControl,
    WorkdayStateObserver,
    WorkdayStateSecurityError,
)
from services.workday_transition_contracts import (
    WorkdayFailureClass,
    WorkdayTransitionState,
)

SCOPE = "workday:wf:wellsfargojobs"
BASE_URL = "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs"


def _control(
    role: str,
    name: str = "",
    *,
    input_type: str = "",
    busy: bool = False,
) -> WorkdaySemanticControl:
    return WorkdaySemanticControl(
        role=role,
        semantic_name=name,
        input_type=input_type,
        busy=busy,
    )


@dataclass
class _ReadOnlyAdapter:
    structure: WorkdayPageStructure
    captures: int = 0
    mutations: int = 0
    llm_calls: int = 0
    vault_calls: int = 0

    async def capture_structure(self, *, limit: int) -> WorkdayPageStructure:
        assert limit == MAX_OBSERVED_CONTROLS
        self.captures += 1
        return self.structure


def _observer(
    url: str, controls: tuple[WorkdaySemanticControl, ...]
) -> WorkdayStateObserver:
    return WorkdayStateObserver(
        _ReadOnlyAdapter(WorkdayPageStructure(url=url, controls=controls)),
        expected_tenant_scope=SCOPE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs",
        "https://example.com/recruiting/wf/WellsFargoJobs",
        "https://wd1.myworkdaysite.com/recruiting/other/WellsFargoJobs",
        "https://wd1.myworkdaysite.com/recruiting/wf/OtherSite",
    ],
)
async def test_wrong_origin_or_tenant_fails_closed(url: str) -> None:
    with pytest.raises(WorkdayStateSecurityError) as exc_info:
        await _observer(url, (_control("button", "Apply"),)).observe()

    assert exc_info.value.failure_class is WorkdayFailureClass.WRONG_ORIGIN_OR_TENANT
    assert url not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ((_control("button", "Apply"),), WorkdayTransitionState.JOB_PAGE),
        (
            (_control("button", "Apply Manually"),),
            WorkdayTransitionState.APPLY_CHOICES,
        ),
        (
            (_control("button", "Sign In"), _control("link", "Create Account")),
            WorkdayTransitionState.ACCOUNT_PAGE,
        ),
        (
            (_control("form"), _control("textbox", input_type="email")),
            WorkdayTransitionState.LOGIN_FORM,
        ),
        (
            (
                _control("textbox", input_type="email"),
                _control("textbox", input_type="password"),
                _control("button", "Sign In"),
            ),
            WorkdayTransitionState.AUTH_FORM_STRUCTURALLY_READY,
        ),
        (
            (_control("progressbar", busy=True),),
            WorkdayTransitionState.AUTH_OUTCOME_PENDING,
        ),
        (
            (_control("combobox"), _control("button", "Next")),
            WorkdayTransitionState.AUTHENTICATED_APPLICATION_READY,
        ),
    ],
)
async def test_each_unit_one_fixture_maps_to_exact_state(
    controls: tuple[WorkdaySemanticControl, ...], expected: WorkdayTransitionState
) -> None:
    observed = await _observer(BASE_URL, controls).observe()
    assert observed.state is expected


@pytest.mark.asyncio
async def test_pending_auth_diagnostic_uses_only_safe_reason_codes(caplog) -> None:
    private_name = "private page value must not be logged"

    with caplog.at_level("INFO", logger="services.workday_state_observer"):
        observed = await _observer(
            BASE_URL,
            (_control("status", private_name, busy=True),),
        ).observe()

    assert observed.state is WorkdayTransitionState.AUTH_OUTCOME_PENDING
    assert "safe_reasons=auth_pending" in caplog.text
    assert private_name not in caplog.text


@pytest.mark.asyncio
async def test_equal_safe_structure_has_same_signature() -> None:
    controls = (
        _control("textbox", input_type="email"),
        _control("textbox", input_type="password"),
        _control("button", "Sign In"),
    )
    first = await _observer(BASE_URL, controls).observe()
    second = await _observer(BASE_URL, tuple(reversed(controls))).observe()
    assert first.safe_signature == second.safe_signature


@pytest.mark.asyncio
async def test_dynamic_ids_query_strings_and_unrelated_text_do_not_change_signature() -> (
    None
):
    first_adapter = _ReadOnlyAdapter(
        WorkdayPageStructure(
            f"{BASE_URL}/job/R-1?source=private",
            (
                _control("button", "Apply"),
                _control("button", "unrelated private text"),
            ),
        )
    )
    first_adapter.dynamic_element_ids = ("volatile-123",)
    second_adapter = _ReadOnlyAdapter(
        WorkdayPageStructure(
            f"{BASE_URL}/job/R-2?tracking=different",
            (
                _control("button", "Apply"),
                _control("button", "other unrelated text"),
            ),
        )
    )
    second_adapter.dynamic_element_ids = ("volatile-999",)
    first = await WorkdayStateObserver(
        first_adapter, expected_tenant_scope=SCOPE
    ).observe()
    second = await WorkdayStateObserver(
        second_adapter, expected_tenant_scope=SCOPE
    ).observe()
    assert first.safe_signature == second.safe_signature


@pytest.mark.asyncio
async def test_private_or_raw_data_never_appears_in_serialized_observation() -> None:
    prohibited = (
        "person@example.test",
        "secret-password",
        "raw portal instructions",
        "session-cookie",
        "browser-snapshot",
    )
    controls = tuple(_control("button", value) for value in prohibited) + (
        _control("button", "Apply"),
    )
    observed = await _observer(f"{BASE_URL}?email={prohibited[0]}", controls).observe(
        include_candidates=True
    )
    serialized = json.dumps(asdict(observed), sort_keys=True)
    assert all(value not in serialized for value in prohibited)
    assert "http" not in serialized
    assert "dynamic-" not in serialized


@pytest.mark.asyncio
async def test_candidate_ids_are_bounded_unique_and_allowlisted() -> None:
    controls = tuple(_control("button", "Apply") for _ in range(60)) + (
        _control("button", "Submit"),
        _control("textbox", "person@example.test", input_type="email"),
    )
    observed = await _observer(BASE_URL, controls).observe(include_candidates=True)
    candidates = observed.candidate_metadata
    ids = [candidate.candidate_id for candidate in candidates]
    assert len(candidates) == 40
    assert len(ids) == len(set(ids))
    assert all(len(candidate_id) <= 20 for candidate_id in ids)
    assert {candidate.intent_key for candidate in candidates} == {
        "start_job_application"
    }
    assert {candidate.semantic_role for candidate in candidates} == {"button"}


@pytest.mark.asyncio
async def test_observation_is_fresh_and_performs_no_mutation_llm_or_vault_call() -> (
    None
):
    adapter = _ReadOnlyAdapter(
        WorkdayPageStructure(BASE_URL, (_control("button", "Apply"),))
    )
    observer = WorkdayStateObserver(adapter, expected_tenant_scope=SCOPE)

    first = await observer.observe()
    adapter.structure = WorkdayPageStructure(
        BASE_URL, (_control("button", "Apply Manually"),)
    )
    second = await observer.observe()

    assert first.state is WorkdayTransitionState.JOB_PAGE
    assert second.state is WorkdayTransitionState.APPLY_CHOICES
    assert adapter.captures == 2
    assert adapter.mutations == adapter.llm_calls == adapter.vault_calls == 0
