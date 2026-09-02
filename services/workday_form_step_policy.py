"""Stage-neutral form step execution and verified preparation policy."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import UUID

from services.workday_playwright_worker import (
    _INFORMATION_SECTION_HEADING,
    WorkdayFormAssignment,
    WorkdayFormField,
    WorkdayFormFillResult,
)
from services.workday_unit2_checkpoint import (
    bind_safe_structural_signature,
    canonical_workday_route_identity,
    compute_safe_structural_signature,
)

logger = logging.getLogger(__name__)

_CONSENT_TEXT_PATTERN = re.compile(
    r"\b(?:consent|terms|acknowledge|agree|privacy\s+policy)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Unit2PreparedForm:
    """Private prepared form metadata without field values or raw labels."""

    application_id: UUID
    attempt_id: UUID
    lease_id: UUID
    resume_safe_signature: str
    prepared_safe_signature: str
    required_count: int
    verified_count: int

    def __post_init__(self) -> None:
        if 0 in (self.application_id.int, self.attempt_id.int, self.lease_id.int):
            raise ValueError("Prepared-form execution IDs must be nonzero.")
        if not self.resume_safe_signature or not self.prepared_safe_signature:
            raise ValueError("Both resume and prepared safe signatures are required.")
        if self.verified_count < self.required_count:
            raise ValueError("All required fields must be verified.")


@dataclass(frozen=True, slots=True)
class WorkdayFormStepPolicyResult:
    """Result of executing one form step."""

    prepared_form: Unit2PreparedForm | None
    hold_code: str | None
    hold_question: str | None


def extract_field_question_text(field: WorkdayFormField) -> str | None:
    """Extract a bounded, clean prompt text from a form field."""
    for candidate in (
        field.label_text,
        field.aria_label,
        field.placeholder,
        field.name_attr,
        field.id_attr,
    ):
        if candidate and candidate.strip():
            return " ".join(candidate.split())[:2000]
    return None


def _assignment_matches_field(
    assignment: WorkdayFormAssignment, field: WorkdayFormField
) -> bool:
    """Compare one rescanned control with its approved assignment without logging values."""
    expected = assignment.value.strip()
    actual = str(field.current_value or "").strip()
    if not expected:
        return False

    if field.input_type in {"checkbox", "radio"}:
        truthy = {"1", "checked", "true", "yes"}
        falsy = {"0", "false", "no", "unchecked"}
        expected_normalized = expected.casefold()
        actual_normalized = actual.casefold()
        if expected_normalized in truthy:
            return actual_normalized in truthy
        if expected_normalized in falsy:
            return actual_normalized in falsy
        return False

    if field.tag == "select" or field.input_type in {
        "select",
        "select-one",
        "combobox",
    }:
        expected_normalized = expected.casefold()
        actual_normalized = actual.casefold()
        if actual_normalized == expected_normalized:
            return True
        for option in field.options:
            option_value = str(option.get("value", "")).strip().casefold()
            option_text = str(option.get("text", "")).strip().casefold()
            if expected_normalized in {option_value, option_text}:
                return actual_normalized == option_value
        return False

    return actual.casefold() == expected.casefold()


async def execute_workday_form_step(
    *,
    browser: Any,
    map_fields_fn: Callable[
        [str, list[WorkdayFormField]], Awaitable[list[WorkdayFormAssignment]]
    ],
    application_id: UUID,
    step_number: int = 1,
    attempt_id: UUID | None = None,
    lease_id: UUID | None = None,
    resume_safe_signature: str | None = None,
) -> WorkdayFormStepPolicyResult:
    """Scan, map approved answers, fill without overwriting, and verify one form step."""
    page_url, fields = await browser.scan_application_fields()
    if not fields:
        return WorkdayFormStepPolicyResult(None, "unsupported_step", None)

    # 1. Check for required file inputs -> upload_failure
    required_files = [f for f in fields if f.required and f.input_type == "file"]
    if required_files:
        return WorkdayFormStepPolicyResult(None, "upload_failure", None)

    # 2. Query approved answer mapping
    assignments = await map_fields_fn(page_url, fields)

    # 3. Accept only approved sources with non-empty values
    valid_assignments = [
        a
        for a in assignments
        if a.answer_source in {"profile", "approved_rule"}
        and a.value
        and a.value.strip()
    ]
    assigned_uids = {a.field_uid for a in valid_assignments}
    if len(assigned_uids) != len(valid_assignments):
        logger.warning("duplicate_approved_form_assignment_rejected")
        return WorkdayFormStepPolicyResult(None, "validation_failure", None)

    # 4. Check existing DOM values so we never overwrite user data
    non_empty_dom_uids = {
        f.field_uid
        for f in fields
        if f.current_value
        and str(f.current_value).strip()
        and str(f.current_value).lower() not in {"false", "0"}
    }

    # 5. Check unfamiliar consent before any browser write.
    for f in fields:
        if (
            f.required
            and f.input_type in {"checkbox", "radio"}
            and f.field_uid not in assigned_uids
        ):
            q_text = extract_field_question_text(f) or ""
            if _CONSENT_TEXT_PATTERN.search(q_text):
                return WorkdayFormStepPolicyResult(
                    None, "unfamiliar_consent", q_text or None
                )

    # 6. Every required value, including a prefilled value, needs server approval.
    missing_required = [
        f for f in fields if f.required and f.field_uid not in assigned_uids
    ]
    if missing_required:
        missing_field = missing_required[0]
        question = extract_field_question_text(missing_field)
        if question is None:
            return WorkdayFormStepPolicyResult(None, "unsupported_step", None)
        return WorkdayFormStepPolicyResult(None, "unknown_required_question", question)

    fields_by_uid = {field.field_uid: field for field in fields}
    for assignment in valid_assignments:
        initial_field = fields_by_uid.get(assignment.field_uid)
        if initial_field is None:
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
        if (
            assignment.field_uid in non_empty_dom_uids
            and not _assignment_matches_field(assignment, initial_field)
        ):
            return WorkdayFormStepPolicyResult(
                None,
                "validation_failure",
                extract_field_question_text(initial_field),
            )

    # 7. Preserve only prefilled fields that already match their approval.
    assignments_to_fill = [
        a for a in valid_assignments if a.field_uid not in non_empty_dom_uids
    ]

    # 8. Fill and verify application fields
    fill_result: WorkdayFormFillResult = (
        await browser.fill_and_verify_application_fields(assignments_to_fill)
    )

    required_uids = {f.field_uid for f in fields if f.required}

    if fill_result.failed_field_uids:
        failed_uid = fill_result.failed_field_uids[0]
        failed_field = next((f for f in fields if f.field_uid == failed_uid), None)
        failed_question = (
            extract_field_question_text(failed_field) if failed_field else None
        )
        logger.warning(
            "workday_form_field_validation_failed application_id=%s step=%s field_uid=%s",
            application_id,
            step_number,
            failed_uid,
        )
        return WorkdayFormStepPolicyResult(None, "validation_failure", failed_question)

    if required_uids.intersection(fill_result.unsupported_field_uids):
        return WorkdayFormStepPolicyResult(None, "unsupported_step", None)

    proof_inputs = (
        attempt_id is not None,
        lease_id is not None,
        bool(resume_safe_signature),
    )
    if any(proof_inputs) and not all(proof_inputs):
        logger.warning(
            "workday_form_step_incomplete_proof_context application_id=%s step=%s",
            application_id,
            step_number,
        )
        return WorkdayFormStepPolicyResult(None, "validation_failure", None)

    # The legacy Unit 1 runner consumes only the browser adapter's existing
    # fill/verify result. Unit 2 supplies all three proof inputs and continues
    # into the stronger rescan, value match, and structural-proof checks below.
    if not any(proof_inputs):
        logger.info(
            "workday_form_step_verified application_id=%s step=%s field_count=%s required_count=%s verified_count=%s",
            application_id,
            step_number,
            len(fields),
            len(required_uids),
            fill_result.verified_count,
        )
        return WorkdayFormStepPolicyResult(None, None, None)

    # 9. Re-scan after filling to compute prepared safe signature and count verified fields
    re_page_url, re_fields = await browser.scan_application_fields()
    re_fields_by_uid = {f.field_uid: f for f in re_fields}

    # Verify original required fields remain present after autofill
    if not required_uids.issubset(re_fields_by_uid.keys()):
        missing_uids = required_uids - set(re_fields_by_uid.keys())
        missing_field = next((f for f in fields if f.field_uid in missing_uids), None)
        missing_q = (
            extract_field_question_text(missing_field) if missing_field else None
        )
        return WorkdayFormStepPolicyResult(None, "validation_failure", missing_q)

    assignments_by_uid = {a.field_uid: a for a in valid_assignments}

    # Check that every approved assignment exactly matches its rescanned control.
    for a in valid_assignments:
        re_f = re_fields_by_uid.get(a.field_uid)
        if re_f is None:
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
        if not _assignment_matches_field(a, re_f):
            q = extract_field_question_text(re_f)
            return WorkdayFormStepPolicyResult(None, "validation_failure", q)

    # Check browser validity of controls - strictly fail closed on invalid controls or check failure
    page_obj = (
        getattr(browser, "raw_page", None)
        or getattr(browser, "page", None)
        or getattr(browser, "_page", None)
        or browser
    )
    if hasattr(page_obj, "query_selector_all"):
        try:
            invalid_elems = await page_obj.query_selector_all(
                "[aria-invalid='true'], :invalid, [data-automation-id*='error' i], [role='alert'], [data-automation-id='formFeedback']"
            )
            for elem in invalid_elems:
                is_vis = True
                if hasattr(elem, "is_visible"):
                    is_vis = await elem.is_visible()
                if is_vis:
                    logger.warning("browser_control_validation_failed")
                    return WorkdayFormStepPolicyResult(
                        None, "validation_failure", "Browser control validation failed"
                    )
        except Exception as exc:
            logger.warning("browser_control_validation_check_failed exc=%s", exc)
            return WorkdayFormStepPolicyResult(
                None, "validation_failure", "Browser control validation check failed"
            )
    elif hasattr(browser, "check_control_validity"):
        is_valid = await browser.check_control_validity()
        if not is_valid:
            return WorkdayFormStepPolicyResult(
                None, "validation_failure", "Browser control validation check failed"
            )
    elif hasattr(browser, "fields"):
        for f in getattr(browser, "fields", []):
            if getattr(f, "aria_invalid", False) or getattr(f, "has_error", False):
                return WorkdayFormStepPolicyResult(
                    None, "validation_failure", extract_field_question_text(f)
                )
    else:
        logger.warning("browser_control_validation_inspection_unavailable")
        return WorkdayFormStepPolicyResult(
            None,
            "validation_failure",
            "Browser control validation inspection unavailable",
        )

    # Exact-check ALL required fields: every required field MUST be approved via assignments_by_uid
    re_required = [f for f in re_fields if f.required]
    verified_required: list[WorkdayFormField] = []
    for re_f in re_required:
        q = extract_field_question_text(re_f)
        re_val = str(re_f.current_value or "").strip()
        if not re_val or re_val.lower() in {"false", "0"}:
            return WorkdayFormStepPolicyResult(None, "validation_failure", q)

        # Unassigned required fields are unapproved and must not be accepted
        if re_f.field_uid not in assignments_by_uid:
            logger.warning(
                "unassigned_required_field_rejected field_uid=%s", re_f.field_uid
            )
            return WorkdayFormStepPolicyResult(None, "validation_failure", q)

        assignment = assignments_by_uid[re_f.field_uid]
        if not _assignment_matches_field(assignment, re_f):
            return WorkdayFormStepPolicyResult(None, "validation_failure", q)
        verified_required.append(re_f)

    if len(verified_required) < len(re_required):
        unverified = [f for f in re_required if f not in verified_required]
        unverified_q = (
            extract_field_question_text(unverified[0]) if unverified else None
        )
        return WorkdayFormStepPolicyResult(None, "validation_failure", unverified_q)

    # Count actual factually verified controls
    verified_count = len(verified_required)

    # Obtain the actual visible heading/control counts from the same page snapshot.
    try:
        if not hasattr(page_obj, "query_selector_all"):
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
        observed_url_before = str(getattr(page_obj, "url", ""))
        if observed_url_before != re_page_url:
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
        headings = await page_obj.query_selector_all(
            "h1, h2, h3, [data-automation-id*='heading' i], "
            "[data-automation-id*='pageHeader' i]"
        )
        heading_name: str | None = None
        for heading in headings:
            if not await heading.is_visible():
                continue
            candidate = (await heading.inner_text()).strip()
            if _INFORMATION_SECTION_HEADING.match(candidate):
                heading_name = candidate
                break
        if heading_name is None:
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
        buttons = await page_obj.query_selector_all("button, [role='button']")
        button_count = 0
        for button in buttons:
            if await button.is_visible():
                button_count += 1
        if str(getattr(page_obj, "url", "")) != observed_url_before:
            return WorkdayFormStepPolicyResult(None, "validation_failure", None)
    except Exception as exc:
        logger.warning(
            "browser_structure_observation_failed exc=%s", type(exc).__name__
        )
        return WorkdayFormStepPolicyResult(None, "validation_failure", None)

    parsed = urlsplit(re_page_url)
    structural_sig = compute_safe_structural_signature(
        url_path=parsed.path,
        heading_name=heading_name,
        field_count=len(re_fields),
        button_count=button_count,
        route_identity_digest=canonical_workday_route_identity(re_page_url),
    )

    prepared_form = None
    if attempt_id and lease_id and resume_safe_signature:
        prepared_sig = bind_safe_structural_signature(
            structural_sig,
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
        )
        prepared_form = Unit2PreparedForm(
            application_id=application_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            resume_safe_signature=resume_safe_signature,
            prepared_safe_signature=prepared_sig,
            required_count=len(re_required),
            verified_count=verified_count,
        )

    logger.info(
        "workday_form_step_verified application_id=%s step=%s field_count=%s required_count=%s verified_count=%s",
        application_id,
        step_number,
        len(fields),
        len(re_required),
        verified_count,
    )

    return WorkdayFormStepPolicyResult(prepared_form, None, None)
