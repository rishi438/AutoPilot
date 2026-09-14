from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.workday_form_step_policy import (
    Unit2PreparedForm,
    WorkdayFormStepPolicyResult,
    execute_workday_form_step,
    extract_field_question_text,
)
from services.workday_playwright_worker import (
    WorkdayFormAssignment,
    WorkdayFormField,
    WorkdayFormFillResult,
)


def test_unit2_prepared_form_validity() -> None:
    application_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    lease_id = uuid.uuid4()

    form = Unit2PreparedForm(
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        resume_safe_signature="resume_sig_123",
        prepared_safe_signature="prep_sig_456",
        required_count=3,
        verified_count=3,
    )
    assert form.required_count == 3
    assert form.verified_count == 3

    # Empty signatures raise ValueError
    with pytest.raises(ValueError):
        Unit2PreparedForm(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            resume_safe_signature="",
            prepared_safe_signature="prep_sig",
            required_count=1,
            verified_count=1,
        )

    # verified_count < required_count raises ValueError
    with pytest.raises(ValueError):
        Unit2PreparedForm(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            resume_safe_signature="resume_sig",
            prepared_safe_signature="prep_sig",
            required_count=5,
            verified_count=2,
        )


class _MockElement:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        return self._text


class _MockBrowser:
    def __init__(
        self,
        fields: list[WorkdayFormField],
        page_url: str = "https://wd1.myworkdaysite.com/job/R-1/apply",
        failed_uids: tuple[str, ...] = (),
        unsupported_uids: tuple[str, ...] = (),
    ) -> None:
        self.fields = list(fields)
        self.page_url = page_url
        self.url = page_url
        self.failed_uids = failed_uids
        self.unsupported_uids = unsupported_uids
        self.filled_assignments: list[WorkdayFormAssignment] = []

    async def query_selector_all(self, selector: str) -> list[_MockElement]:
        if selector.startswith("[aria-invalid='true']"):
            return []
        if selector.startswith("h1, h2, h3"):
            return [_MockElement("My Information")]
        if selector == "button, [role='button']":
            return [_MockElement("Save and Continue")]
        return []

    async def scan_application_fields(self) -> tuple[str, list[WorkdayFormField]]:
        return self.page_url, self.fields

    async def fill_and_verify_application_fields(
        self, assignments: list[WorkdayFormAssignment]
    ) -> WorkdayFormFillResult:
        self.filled_assignments = list(assignments)
        for a in assignments:
            for f in self.fields:
                if f.field_uid == a.field_uid:
                    # Update current_value in mock DOM
                    self.fields = [
                        WorkdayFormField(
                            field_uid=item.field_uid,
                            tag=item.tag,
                            input_type=item.input_type,
                            name_attr=item.name_attr,
                            id_attr=item.id_attr,
                            label_text=item.label_text,
                            placeholder=item.placeholder,
                            aria_label=item.aria_label,
                            required=item.required,
                            readonly=item.readonly,
                            disabled=item.disabled,
                            current_value=(
                                a.value
                                if item.field_uid == a.field_uid
                                else item.current_value
                            ),
                            max_length=item.max_length,
                            options=item.options,
                        )
                        for item in self.fields
                    ]
        return WorkdayFormFillResult(
            filled_count=len(assignments),
            verified_count=len(assignments),
            failed_field_uids=self.failed_uids,
            unsupported_field_uids=self.unsupported_uids,
        )


@pytest.mark.asyncio
async def test_form_step_required_file_triggers_upload_failure() -> None:
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="file",
            name_attr="resume",
            id_attr="resume_input",
            label_text="Upload Resume",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        )
    ]
    browser = _MockBrowser(fields)

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return []

    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=uuid.uuid4(),
    )
    assert res.hold_code == "upload_failure"
    assert res.prepared_form is None


@pytest.mark.asyncio
async def test_form_step_missing_required_answer_triggers_unknown_required_question() -> (
    None
):
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="text",
            name_attr="referral_source",
            id_attr="referral",
            label_text="How did you hear about us?",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        )
    ]
    browser = _MockBrowser(fields)

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return []

    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=uuid.uuid4(),
    )
    assert res.hold_code == "unknown_required_question"
    assert res.hold_question == "How did you hear about us?"
    assert res.prepared_form is None


@pytest.mark.asyncio
async def test_form_step_does_not_overwrite_existing_dom_values() -> None:
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="text",
            name_attr="first_name",
            id_attr="fname",
            label_text="First Name",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value="ExistingJohn",
            max_length=None,
            options=(),
        )
    ]
    browser = _MockBrowser(fields)

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return [
            WorkdayFormAssignment(
                field_uid="0",
                value="ExistingJohn",
                answer_source="profile",
            )
        ]

    attempt_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    application_id = uuid.uuid4()
    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        resume_safe_signature="resume_sig_001",
    )
    assert res.hold_code is None
    assert len(browser.filled_assignments) == 0  # Not filled because DOM had value
    assert res.prepared_form is not None
    assert res.prepared_form.application_id == application_id
    assert res.prepared_form.required_count == 1
    assert res.prepared_form.verified_count >= 1


@pytest.mark.asyncio
async def test_form_step_validation_failure() -> None:
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="text",
            name_attr="postal_code",
            id_attr="zip",
            label_text="Postal Code",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        )
    ]
    browser = _MockBrowser(fields, failed_uids=("0",))

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return [
            WorkdayFormAssignment(
                field_uid="0",
                value="99999",
                answer_source="profile",
            )
        ]

    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=uuid.uuid4(),
    )
    assert res.hold_code == "validation_failure"
    assert res.hold_question == "Postal Code"
    assert res.prepared_form is None


@pytest.mark.asyncio
async def test_form_step_unfamiliar_consent() -> None:
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="checkbox",
            name_attr="consent_check",
            id_attr="consent",
            label_text="I consent to the special tenant data processing terms and conditions",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        )
    ]
    browser = _MockBrowser(fields)

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return []

    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=uuid.uuid4(),
    )
    assert res.hold_code == "unfamiliar_consent"
    assert "consent" in (res.hold_question or "").lower()
    assert res.prepared_form is None


@pytest.mark.asyncio
async def test_form_step_successful_preparation_returns_unit2_prepared_form() -> None:
    fields = [
        WorkdayFormField(
            field_uid="0",
            tag="input",
            input_type="text",
            name_attr="first_name",
            id_attr="fname",
            label_text="First Name",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        ),
        WorkdayFormField(
            field_uid="1",
            tag="input",
            input_type="text",
            name_attr="last_name",
            id_attr="lname",
            label_text="Last Name",
            placeholder=None,
            aria_label=None,
            required=True,
            readonly=False,
            disabled=False,
            current_value=None,
            max_length=None,
            options=(),
        ),
    ]
    browser = _MockBrowser(fields)

    async def map_fn(
        url: str, f: list[WorkdayFormField]
    ) -> list[WorkdayFormAssignment]:
        return [
            WorkdayFormAssignment(field_uid="0", value="Jane", answer_source="profile"),
            WorkdayFormAssignment(field_uid="1", value="Doe", answer_source="profile"),
        ]

    attempt_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    application_id = uuid.uuid4()
    res = await execute_workday_form_step(
        browser=browser,
        map_fields_fn=map_fn,
        application_id=application_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        resume_safe_signature="resume_sig_ok",
    )
    assert res.hold_code is None
    assert res.prepared_form is not None
    assert res.prepared_form.application_id == application_id
    assert res.prepared_form.attempt_id == attempt_id
    assert res.prepared_form.lease_id == lease_id
    assert res.prepared_form.required_count == 2
    assert res.prepared_form.verified_count == 2
    assert bool(res.prepared_form.prepared_safe_signature) is True
