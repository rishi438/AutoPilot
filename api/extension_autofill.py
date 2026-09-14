"""
Chrome extension: map visible job-application form fields to the user's profile via LLM.

The client scans accessible frames and open shadow roots, then previews suggestions before applying values in-tab.
"""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.extension_autofill_rules import (
    build_deterministic_raw_assignments,
    filter_skipped_for_assigned_uids,
    merge_assignment_dicts,
)
from config.settings import get_settings
from models.database import JobApplication, JobFormAnswer, User
from models.database import UserProfile as UserProfileModel
from services.application_automation import (
    classify_sensitivity,
    is_prohibited_answer_material,
    reusable_answer_value,
    resolve_approved_answer,
)
from utils.auth import get_current_user_with_complete_profile
from utils.cache import (
    cache_tool_result,
    check_rate_limit_with_headers,
    generate_hash,
    get_cached_tool_result,
)
from utils.database import get_database
from utils.encryption import decrypt_api_key
from utils.error_responses import (
    ErrorCode,
    external_service_error,
    internal_error,
    no_api_key_error,
    not_found_error,
    rate_limit_error,
)
from utils.llm_client import (
    GeminiError,
    get_gemini_client,
    user_facing_message_from_llm_exception,
)
from utils.llm_parsing import parse_json_from_llm_response
from utils.llm_preferences import get_user_llm_request_options
from utils.security import sanitize_text

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_user_resume_asset_model():
    from models.database import UserResumeAsset

    return UserResumeAsset


def _portal_hostname(page_url: str) -> str | None:
    """Return a normalized HTTPS hostname for answer-library scoping."""
    try:
        parsed = urlparse(page_url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return parsed.hostname.lower().removeprefix("www.")


# =============================================================================
# CONSTANTS
# =============================================================================

_MAX_FIELDS: int = 80
_MAX_LABEL_CHARS: int = 600
_MAX_OPTION_TEXT: int = 200
_MAX_OPTIONS_PER_SELECT: int = 40
_MAX_PAGE_URL_LEN: int = 2048
_MAX_EXTRAS_KEYS: int = 16
_MAX_EXTRA_KEY_LEN: int = 64
_MAX_EXTRA_VALUE_LEN: int = 500
_MAX_ASSIGNMENT_VALUE: int = 8000

_RATE_LIMIT: int = 15
_RATE_LIMIT_DEBUG: int = 200
_RATE_WINDOW_S: int = 3600


def _autofill_rate_limit() -> int:
    """Production cap is 15/hour; local DEBUG allows more iteration while testing."""
    if get_settings().debug:
        return _RATE_LIMIT_DEBUG
    return _RATE_LIMIT


_SYSTEM_PROMPT: str = """You map job application form fields to a user's profile data.

Rules:
- Output ONLY a JSON object with keys "assignments" and "skipped". No markdown fences.
- "assignments" is an array of {"field_uid": string, "value": string}. Use ONLY field_uid values from the input list.
- "skipped" is an array of {"field_uid": string, "reason": string} for fields you refuse to fill or cannot map.
- Use ONLY facts present in the provided profile JSON and extras JSON. Do not invent employers, degrees, or credentials.
- Fields marked required:true are priority — fill them when profile data supports an answer; do not skip merely to be cautious.
- Name fields (full/legal/applicant name): copy PROFILE_JSON full_name EXACTLY as stored — include all given names and surnames; never shorten to first+last only.
- First name / last name split fields: derive from full_name (first token = first name; last name = all remaining tokens joined, e.g. Elior Nataf Lackritz → first Elior, last Nataf Lackritz; middle-only field = tokens between first and last when asked).
- Email fields: use PROFILE_JSON email exactly.
- Phone fields: use profile.phone when present.
- When fields ask for school, university, degree, major, field of study, or graduation dates, map from profile.education when present (each entry may include institution, degree, field_of_study, start_date, end_date, is_current).
- When multiple Degree/Discipline fields appear (duplicate_label_index 0, 1, …), map profile.education[0] to index 0, profile.education[1] to index 1, etc. Fill every row when profile data exists.
- Degree dropdown mapping: copy the profile degree text; the server normalizes common aliases to dropdown labels. Examples — Associate: AA/AS/AAS → Associate's Degree; Bachelor: BA/BS/BSc/BBA/BEng/BFA/LLB/Bachelor of Laws → Bachelor's Degree (or specific Bachelor of … when listed); Master: MA/MS/MSc/MBA/MEng → Master of Arts/Science/Business/etc. or Master's Degree; explicit JD/Juris Doctor in profile → Juris Doctor (J.D.); Doctorate: PhD/MD/EdD → matching Doctor of … option. Never invent a degree not in profile.education.
- Years of industry experience: when options are numeric ranges (e.g. 5-7, 8-10, 11+), pick the bucket closest to profile.years_experience — if the exact count is below a range's lower bound, use the nearest range above (e.g. 4 years → 5-7 when that is the closest bucket).
- Profile may include city, state, country, country_phone_code, postal_code, willing_to_relocate, work_arrangements, desired_company_sizes, phone, linkedin_url, github_url, portfolio_url, work_authorization (no_work_authorization | has_work_authorization | us_lawful_permanent_resident | us_citizen; omit if null), requires_visa_sponsorship (boolean), and resume_file.has_file — use only when present in PROFILE_JSON.
- Country dropdown/combobox fields: map profile.country to the full country name (e.g. US → United States).
- Country phone-code fields: derive the dial code from profile.country; never put the phone number into a country-code control.
- Postal/ZIP/PIN code fields: use profile.postal_code exactly when present.
- Location (City) / current city / where you are located: use profile.city and profile.state when present (e.g. Hoboken, NJ) — not country dropdowns or relocation screening questions.
- Website/portfolio fields: use portfolio_url when present; if empty, use github_url even when the form also has a separate GitHub Username field.
- GitHub Username fields (not Website): use the username from github_url (e.g. eliornl from https://github.com/eliornl).
- Legal acknowledgement attestations (acknowledge/confirm/agree to statements): select Yes or the affirmative agreement option — do not skip as EEO.
- Application consent checkboxes ("By checking this box, I agree to allow … store/process/retain my data", privacy consent): assign value "checked" — default to checked; skip optional marketing/newsletter opt-ins and EEO/diversity boxes.
- "Based in the United States" / US location questions: Yes when country is US/United States OR work_authorization is us_citizen or us_lawful_permanent_resident; No when country is clearly outside the US and no US work authorization.
- Visa / employment sponsorship questions ("require visa sponsorship", H-1B, etc.): answer "Yes" ONLY if profile.requires_visa_sponsorship is true. Answer "No" when false or when work_authorization is us_citizen, us_lawful_permanent_resident, or has_work_authorization — having authorization means you do NOT need sponsorship; never answer Yes because the user is authorized.
- In-person / on-site / NYC / tri-state commute questions (e.g. commute to NYC office 2x/week): use profile.city and profile.state to judge whether the candidate can reasonably commute (e.g. Hoboken NJ and Jersey City → Yes; Austin TX → No unless willing_to_relocate). Plain Yes/No → Yes when commute is reasonable. Long option lists → pick currently local/metropolitan when close, relocation when willing_to_relocate, else cannot work in-office. Match full option text when listed.
- "Not local to central offices" / willing to relocate (Greenhouse long dropdowns): same geographic reasoning from city/state; pick currently local/metropolitan when close, relocation when willing_to_relocate, else not-willing.
- Startup readiness questions: Yes when desired_company_sizes includes startup; otherwise Yes unless the user only selected enterprise/large company sizes.
- Open-ended questions (why this company/role, why join, motivation, cover-letter-style prompts): ONE or TWO short sentences only (about 25–50 words total). Lead with role fit; one concrete point from profile.summary or work_experience — no lists, no fluff, no repetition. You may name the employer from the page URL when obvious. Do not invent facts.
- input_type "file" for resume/CV: skip (the client attaches the stored resume separately). Do not assign file fields.
- If a field asks for legally sensitive attestations, diversity/EEO self-ID, or anything you should not infer, skip it.
- For salary expectation questions (in $, yearly): use profile.desired_salary_range min/max when present (e.g. 150000-200000); if absent, skip — do not invent a number.
- Start date / notice period / "how quickly are you looking to start" questions: answer with exactly "I can start a new role in 2 weeks." — not 2-4 weeks or a longer paragraph.
- Keep values concise. Match the expected format when obvious (e.g. email for email fields).
- Skip only when profile truly lacks the needed fact or the field is EEO/diversity; do not skip required screening questions when profile has the answer.
"""

# =============================================================================
# MODELS
# =============================================================================


class AutofillSelectOption(BaseModel):
    """One <option> for a select control."""

    value: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=_MAX_OPTION_TEXT)


class AutofillFieldIn(BaseModel):
    """Serialized form control from an accessible frame or open shadow root."""

    field_uid: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^\d+$",
        description="Stable id from the extension serializer (digits only)",
    )
    tag: str = Field(..., max_length=24)
    input_type: str | None = Field(None, max_length=32)
    name_attr: str | None = Field(None, max_length=240)
    id_attr: str | None = Field(None, max_length=240)
    label_text: str = Field(default="", max_length=_MAX_LABEL_CHARS)
    placeholder: str | None = Field(None, max_length=500)
    aria_label: str | None = Field(None, max_length=500)
    required: bool = False
    readonly: bool = False
    disabled: bool = False
    current_value: str | None = Field(None, max_length=500)
    max_length: int | None = Field(None, ge=0, le=1_000_000)
    options: list[AutofillSelectOption] | None = Field(
        None, max_length=_MAX_OPTIONS_PER_SELECT
    )
    duplicate_label_index: int = Field(
        default=0,
        ge=0,
        le=20,
        description="0-based index when the same label appears on multiple fields (e.g. education rows)",
    )


class AutofillMapRequest(BaseModel):
    """Request body for POST /extension/autofill/map."""

    fields: list[AutofillFieldIn] = Field(..., min_length=1)
    page_url: str = Field(..., min_length=1, max_length=_MAX_PAGE_URL_LEN)
    application_id: uuid.UUID | None = Field(
        default=None,
        description="Optional existing saved application; ownership is verified server-side.",
    )
    extras: dict[str, str] | None = Field(
        default=None,
        description="Optional key/value hints stored in the extension (phone, URLs, etc.)",
    )

    @field_validator("page_url")
    @classmethod
    def _page_url_scheme(cls, v: str) -> str:
        t = v.strip()
        if not t.startswith(("http://", "https://")):
            raise ValueError("page_url must start with http:// or https://")
        return t

    @model_validator(mode="after")
    def _aggregate_field_rules(self) -> AutofillMapRequest:
        if len(self.fields) > _MAX_FIELDS:
            raise ValueError(f"At most {_MAX_FIELDS} fields allowed")
        uids = [f.field_uid for f in self.fields]
        if len(uids) != len(set(uids)):
            raise ValueError("Each field_uid must be unique")
        if self.extras is not None:
            if len(self.extras) > _MAX_EXTRAS_KEYS:
                raise ValueError(f"At most {_MAX_EXTRAS_KEYS} extras keys allowed")
            for k, val in self.extras.items():
                if len(k) > _MAX_EXTRA_KEY_LEN:
                    raise ValueError("extras key too long")
                if val is not None and len(val) > _MAX_EXTRA_VALUE_LEN:
                    raise ValueError("extras value too long")
        return self


class AutofillAssignmentOut(BaseModel):
    """One suggested value for a field."""

    field_uid: str
    value: str
    label_text: str = Field(default="", description="Echo from request for preview UI")
    duplicate_label_index: int = Field(
        default=0,
        ge=0,
        le=20,
        description="Which repeated label occurrence (0=first Degree row, 1=second, etc.)",
    )
    answer_source: str = Field(
        default="ai",
        description="profile, approved_rule, ai, or manual; never a browser secret.",
    )
    review_reasons: list[str] = Field(
        default_factory=list,
        description="Reasons the user must inspect this proposed value before it is applied.",
    )


class AutofillMapResponse(BaseModel):
    """LLM mapping result returned to the extension."""

    assignments: list[AutofillAssignmentOut] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="UX hints about inaccessible protected frames or closed shadow roots",
    )
    application_id: uuid.UUID | None = None


# =============================================================================
# HELPERS
# =============================================================================


def _get_user_uuid(current_user: dict[str, Any]) -> uuid.UUID:
    uid = current_user.get("id") or current_user.get("_id")
    if isinstance(uid, str):
        return uuid.UUID(uid)
    return uid


async def _get_user_api_key(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    try:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user and user.gemini_api_key_encrypted:
            return decrypt_api_key(user.gemini_api_key_encrypted)
    except Exception:
        logger.warning(
            "Unable to load stored provider configuration for autofill", exc_info=True
        )
    return None


def _server_has_llm() -> bool:
    """Read settings at call time so tests and env reloads see current config."""
    cfg = get_settings()
    return (
        bool(getattr(cfg, "gemini_api_key", None))
        or bool(getattr(cfg, "use_vertex_ai", False))
        or (
            bool(getattr(cfg, "local_llm_url", None))
            and bool(getattr(cfg, "local_llm_model", None))
        )
    )


async def _load_profile_bundle(
    db: AsyncSession, user_id: uuid.UUID, user_row: User
) -> tuple[dict[str, Any], str | None]:
    """
    Build a JSON-serializable snapshot for the LLM (user + profile).

    Returns:
        Tuple of (snapshot dict, profile updated_at iso or None for cache keying)
    """
    result = await db.execute(
        select(UserProfileModel).where(UserProfileModel.user_id == user_id)
    )
    prof = result.scalar_one_or_none()
    snap: dict[str, Any] = {
        "email": user_row.email,
        "full_name": user_row.full_name,
    }
    prof_sig = ""
    if prof:
        d = prof.to_dict()
        summary = d.get("summary") or ""
        if isinstance(summary, str) and len(summary) > 2500:
            summary = summary[:2500] + "…"
        d["summary"] = summary
        we = d.get("work_experience") or []
        if isinstance(we, list) and len(we) > 12:
            d["work_experience"] = we[:12]
        snap["profile"] = d
        if prof.sensitive_portal_autofill_enabled:
            # Kept outside `profile` and removed before the LLM prompt. These
            # values are mapped deterministically only after explicit opt-in.
            snap["_sensitive_portal"] = {
                "date_of_birth": (
                    decrypt_api_key(prof.date_of_birth_encrypted)
                    if prof.date_of_birth_encrypted
                    else None
                ),
                "pan": (
                    decrypt_api_key(prof.pan_encrypted) if prof.pan_encrypted else None
                ),
                "gender": (
                    decrypt_api_key(prof.gender_encrypted)
                    if prof.gender_encrypted
                    else None
                ),
            }
        if prof.updated_at:
            prof_sig = prof.updated_at.isoformat()
    else:
        snap["profile"] = {}

    UserResumeAsset = _get_user_resume_asset_model()
    ra_res = await db.execute(
        select(UserResumeAsset).where(UserResumeAsset.user_id == user_id)
    )
    ra = ra_res.scalar_one_or_none()
    if ra:
        snap["resume_file"] = {
            "has_file": True,
            "original_filename": ra.original_filename,
            "mime_type": ra.mime_type,
            "byte_size": ra.byte_size,
        }
    else:
        snap["resume_file"] = {"has_file": False}

    return snap, prof_sig


async def map_form_fields_from_approved_sources(
    request: AutofillMapRequest,
    *,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> AutofillMapResponse:
    """Map fields using profile facts and explicitly approved reusable answers only."""
    user_result = await db.execute(select(User).where(User.id == user_id))
    user_row = user_result.scalar_one_or_none()
    if not user_row:
        raise not_found_error(resource_type="User")
    if request.application_id is not None:
        application = await db.get(JobApplication, request.application_id)
        if (
            application is None
            or application.user_id != user_id
            or application.deleted_at is not None
        ):
            raise not_found_error(resource_type="Application")

    approved_answers = list(
        (
            await db.execute(
                select(JobFormAnswer).where(
                    JobFormAnswer.user_id == user_id,
                    JobFormAnswer.approved_for_reuse.is_(True),
                )
            )
        ).scalars()
    )
    portal_hostname = _portal_hostname(request.page_url)
    approved_answers = [
        answer
        for answer in approved_answers
        if answer.source_portal is None or answer.source_portal == portal_hostname
    ]
    profile_bundle, _ = await _load_profile_bundle(db, user_id, user_row)
    assignment_fields = [
        field for field in request.fields if _field_needs_assignment(field)
    ]
    fields_by_uid = {field.field_uid: field for field in assignment_fields}
    assignments, skipped = _finalize_autofill_response(
        [], [], fields_by_uid, profile_bundle, assignment_fields, approved_answers
    )
    return AutofillMapResponse(
        assignments=assignments,
        skipped=skipped,
        warnings=_missing_required_warnings(assignment_fields, assignments),
        application_id=request.application_id,
    )


def _sanitize_field_dict(f: AutofillFieldIn) -> dict[str, Any]:
    opts = None
    if f.options:
        opts = [
            {
                "value": sanitize_text(o.value)[:500],
                "text": sanitize_text(o.text)[:_MAX_OPTION_TEXT],
            }
            for o in f.options[:_MAX_OPTIONS_PER_SELECT]
        ]
    return {
        "field_uid": sanitize_text(f.field_uid)[:64],
        "tag": sanitize_text(f.tag)[:24],
        "input_type": sanitize_text(f.input_type)[:32] if f.input_type else None,
        "name_attr": sanitize_text(f.name_attr)[:240] if f.name_attr else None,
        "id_attr": sanitize_text(f.id_attr)[:240] if f.id_attr else None,
        "label_text": sanitize_text(f.label_text)[:_MAX_LABEL_CHARS],
        "placeholder": sanitize_text(f.placeholder)[:500] if f.placeholder else None,
        "aria_label": sanitize_text(f.aria_label)[:500] if f.aria_label else None,
        "required": f.required,
        "readonly": f.readonly,
        "disabled": f.disabled,
        "max_length": f.max_length,
        "options": opts,
        "duplicate_label_index": f.duplicate_label_index,
    }


def _field_needs_assignment(field: AutofillFieldIn) -> bool:
    """Only empty, enabled controls should be proposed to the user."""
    if field.disabled or field.readonly or (field.current_value or "").strip():
        return False
    # A full name is not proof that an optional middle-name control should be
    # populated; only an explicit middle-name profile field could authorize it.
    return re.search(r"\bmiddle(?:\s+|-)?name\b", field.label_text or "", re.I) is None


def _sanitize_form_autofill_value(val: str) -> str:
    """Plain text for DOM input/textarea values — decode entities, strip controls, keep newlines."""
    if not val:
        return ""
    text = html.unescape(str(val))
    # Resolve double-encoded entities (e.g. &amp;#x27; from display sanitizers).
    text = html.unescape(text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if len(text) > _MAX_ASSIGNMENT_VALUE:
        text = text[:_MAX_ASSIGNMENT_VALUE]
    return text


def _sanitize_extras(extras: dict[str, str] | None) -> dict[str, str]:
    if not extras:
        return {}
    out: dict[str, str] = {}
    for k, v in list(extras.items())[:_MAX_EXTRAS_KEYS]:
        kk = sanitize_text(str(k))[:_MAX_EXTRA_KEY_LEN]
        if not kk:
            continue
        out[kk] = sanitize_text(str(v))[:_MAX_EXTRA_VALUE_LEN] if v is not None else ""
    return out


def _build_user_prompt(
    fields_compact: list[dict[str, Any]],
    profile: dict[str, Any],
    extras: dict[str, str],
    page_url: str,
) -> str:
    profile = dict(profile)
    profile.pop("_sensitive_portal", None)
    return (
        "Page URL (context only): "
        + sanitize_text(page_url)[:_MAX_PAGE_URL_LEN]
        + "\n\nFIELDS_JSON:\n"
        + json.dumps(fields_compact, ensure_ascii=False)
        + "\n\nPROFILE_JSON:\n"
        + json.dumps(profile, ensure_ascii=False, default=str)
        + "\n\nEXTRAS_JSON:\n"
        + json.dumps(extras, ensure_ascii=False)
        + '\n\nRespond with JSON: {"assignments":[{"field_uid":"…","value":"…"}],'
        + '"skipped":[{"field_uid":"…","reason":"…"}]}'
    )


def _validate_assignments(
    raw_assignments: list[dict[str, Any]],
    fields_by_uid: dict[str, AutofillFieldIn],
) -> list[AutofillAssignmentOut]:
    out: list[AutofillAssignmentOut] = []
    for item in raw_assignments:
        if not isinstance(item, dict):
            continue
        uid = item.get("field_uid")
        val = item.get("value")
        if not isinstance(uid, str) or uid not in fields_by_uid:
            continue
        if not isinstance(val, str):
            val = str(val) if val is not None else ""
        val = _sanitize_form_autofill_value(val)
        meta = fields_by_uid[uid]
        # The extension attaches the stored resume separately. A file input
        # must never appear as an empty AI answer in the review panel.
        if (meta.input_type or "").lower() == "file":
            continue
        if not _field_needs_assignment(meta):
            continue
        if (
            meta.max_length is not None
            and meta.max_length > 0
            and len(val) > meta.max_length
        ):
            val = val[: int(meta.max_length)]
        source = item.get("answer_source")
        if source not in {"profile", "approved_rule", "ai", "manual"}:
            source = "ai"
        review_reasons = item.get("review_reasons")
        if not isinstance(review_reasons, list):
            review_reasons = []
        normalized_reasons = [
            sanitize_text(str(reason))[:80]
            for reason in review_reasons
            if isinstance(reason, str) and sanitize_text(reason)
        ][:8]
        if source == "ai" and "ai_generated" not in normalized_reasons:
            normalized_reasons.append("ai_generated")
        if meta.required and "required_field" not in normalized_reasons:
            normalized_reasons.append("required_field")
        if (
            classify_sensitivity(meta.label_text) == "sensitive"
            and "sensitive" not in normalized_reasons
        ):
            normalized_reasons.append("sensitive")
        out.append(
            AutofillAssignmentOut(
                field_uid=uid,
                value=val,
                label_text=meta.label_text[:_MAX_LABEL_CHARS],
                duplicate_label_index=meta.duplicate_label_index,
                answer_source=source,
                review_reasons=normalized_reasons,
            )
        )
    return out


def _missing_required_warnings(
    request_fields: Sequence[AutofillFieldIn],
    assignments: Sequence[AutofillAssignmentOut],
) -> list[str]:
    """Warn when required fields (except resume file) received no assignment."""
    assigned = {a.field_uid for a in assignments}
    missing_labels: list[str] = []
    for field in request_fields:
        if not field.required or field.field_uid in assigned:
            continue
        if (field.input_type or "").lower() == "file":
            continue
        label = re.sub(r"\s+", " ", (field.label_text or "").strip())[:100]
        if label:
            missing_labels.append(label)
    if not missing_labels:
        return []
    preview = "; ".join(missing_labels[:3])
    if len(missing_labels) > 3:
        preview += f" (+{len(missing_labels) - 3} more)"
    return [
        f"{len(missing_labels)} required field(s) could not be auto-filled — please review: {preview}"
    ]


def _finalize_autofill_response(
    llm_raw_assignments: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    fields_by_uid: dict[str, AutofillFieldIn],
    profile_bundle: dict[str, Any],
    request_fields: list[AutofillFieldIn],
    approved_answers: Sequence[JobFormAnswer] = (),
) -> tuple[list[AutofillAssignmentOut], list[dict[str, str]]]:
    """
    Merge deterministic profile rules over LLM assignments and drop stale skips.

    Args:
        llm_raw_assignments: Raw assignment dicts from cache or LLM.
        skipped: Skipped field entries from cache or LLM.
        fields_by_uid: field_uid → field metadata.
        profile_bundle: User + profile snapshot.
        request_fields: All fields from the client request.

    Returns:
        Tuple of (validated assignments, filtered skipped list).
    """
    det_raw = build_deterministic_raw_assignments(request_fields, profile_bundle)
    for assignment in det_raw:
        assignment["answer_source"] = "profile"

    approved_raw: list[dict[str, Any]] = []
    for field in request_fields:
        approved = resolve_approved_answer(field.label_text, approved_answers)
        if approved is not None:
            approved_value = reusable_answer_value(approved)
            if approved_value is None:
                continue
            reasons = ["approved_reusable_answer"]
            if classify_sensitivity(field.label_text) == "sensitive":
                reasons.insert(0, "sensitive")
            approved_raw.append(
                {
                    "field_uid": field.field_uid,
                    "value": approved_value,
                    "label_text": field.label_text,
                    "duplicate_label_index": field.duplicate_label_index,
                    "answer_source": "approved_rule",
                    "review_reasons": reasons,
                }
            )

    # A reusable answer is only eligible after an explicit user approval and an
    # exact normalized-question match. Sensitive answers remain blocked unless
    # they have that approved entry; all assignments still go to the review UI.
    non_sensitive_llm = [
        assignment
        for assignment in llm_raw_assignments
        if isinstance(assignment, dict)
        and (field := fields_by_uid.get(str(assignment.get("field_uid", ""))))
        and classify_sensitivity(field.label_text) != "sensitive"
    ]
    non_sensitive_profile = [
        assignment
        for assignment in det_raw
        if (field := fields_by_uid.get(str(assignment.get("field_uid", ""))))
        and classify_sensitivity(field.label_text) != "sensitive"
    ]
    merged_raw = merge_assignment_dicts(non_sensitive_llm, non_sensitive_profile)
    merged_raw = merge_assignment_dicts(merged_raw, approved_raw)
    assignments = _validate_assignments(
        [x for x in merged_raw if isinstance(x, dict)],
        fields_by_uid,
    )
    sensitive = profile_bundle.get("_sensitive_portal") or {}
    for field in request_fields:
        label = (field.label_text or "").lower()
        value = None
        if "date of birth" in label or "dob" in label:
            value = sensitive.get("date_of_birth")
        elif "pan" in label and ("card" in label or "number" in label):
            value = sensitive.get("pan")
        elif re.search(r"\bgender\b", label):
            value = sensitive.get("gender")
        if value:
            assignments = [
                assignment
                for assignment in assignments
                if assignment.field_uid != field.field_uid
            ]
            assignments.append(
                AutofillAssignmentOut(
                    field_uid=field.field_uid,
                    value=value,
                    label_text=field.label_text[:_MAX_LABEL_CHARS],
                    duplicate_label_index=field.duplicate_label_index,
                    answer_source="profile",
                    review_reasons=["sensitive", "user_opted_in"],
                )
            )
    assigned_uids = {assignment.field_uid for assignment in assignments}
    for field in request_fields:
        if field.field_uid in assigned_uids:
            continue
        if (field.input_type or "").lower() == "file":
            continue
        if is_prohibited_answer_material(field.label_text):
            continue
        reasons = ["needs_user_input"]
        if classify_sensitivity(field.label_text) == "sensitive":
            reasons.append("sensitive")
        if field.required:
            reasons.append("required_field")
        assignments.append(
            AutofillAssignmentOut(
                field_uid=field.field_uid,
                value="",
                label_text=field.label_text[:_MAX_LABEL_CHARS],
                duplicate_label_index=field.duplicate_label_index,
                answer_source="manual",
                review_reasons=reasons,
            )
        )
    skipped_safe = filter_skipped_for_assigned_uids(
        skipped,
        [a.field_uid for a in assignments],
    )
    return assignments, skipped_safe


# =============================================================================
# ENDPOINT
# =============================================================================


@router.post("/autofill/map", response_model=AutofillMapResponse)
async def map_form_fields_to_profile(
    request: AutofillMapRequest,
    response: Response,
    current_user: dict[str, Any] = Depends(get_current_user_with_complete_profile),
    db: AsyncSession = Depends(get_database),
) -> AutofillMapResponse:
    """
    Map serialized form field descriptors to profile-backed values using Gemini.

    The extension must show a preview and obtain user confirmation before writing DOM values.
    """
    user_id = _get_user_uuid(current_user)

    rate_cap = _autofill_rate_limit()
    rate = await check_rate_limit_with_headers(
        identifier=f"{user_id}:extension_autofill_map",
        limit=rate_cap,
        window_seconds=_RATE_WINDOW_S,
    )
    if not rate.allowed:
        raise rate_limit_error(
            f"Rate limit exceeded. Maximum {rate_cap} autofill requests per hour. "
            f"Resets in {rate.reset_seconds} seconds.",
            retry_after=rate.reset_seconds,
        )
    for hk, hv in rate.get_headers().items():
        response.headers[hk] = hv

    user_api_key = await _get_user_api_key(db, user_id)
    if not user_api_key and not _server_has_llm():
        raise no_api_key_error()

    user_result = await db.execute(select(User).where(User.id == user_id))
    user_row = user_result.scalar_one_or_none()
    if not user_row:
        raise not_found_error(resource_type="User")

    if request.application_id is not None:
        application = await db.get(JobApplication, request.application_id)
        if (
            application is None
            or application.user_id != user_id
            or application.deleted_at is not None
        ):
            raise not_found_error(resource_type="Application")

    approved_answers = list(
        (
            await db.execute(
                select(JobFormAnswer).where(
                    JobFormAnswer.user_id == user_id,
                    JobFormAnswer.approved_for_reuse.is_(True),
                )
            )
        ).scalars()
    )
    portal_hostname = _portal_hostname(request.page_url)
    approved_answers = [
        answer
        for answer in approved_answers
        if answer.source_portal is None or answer.source_portal == portal_hostname
    ]

    profile_bundle, prof_sig = await _load_profile_bundle(db, user_id, user_row)
    extras_clean = _sanitize_extras(request.extras)

    assignment_fields = [f for f in request.fields if _field_needs_assignment(f)]
    fields_by_uid = {f.field_uid: f for f in assignment_fields}
    fields_compact = [_sanitize_field_dict(f) for f in assignment_fields]
    page_url_clean = sanitize_text(request.page_url.strip())[:_MAX_PAGE_URL_LEN]

    cache_payload: dict[str, Any] = {
        "tool": "extension_autofill",
        "user_id": str(user_id),
        "page_url": page_url_clean,
        "fields": fields_compact,
        "profile_sig": prof_sig or "",
        "extras_sig": generate_hash(json.dumps(extras_clean, sort_keys=True)),
    }

    cached = await get_cached_tool_result("extension_autofill", cache_payload)
    warnings = [
        "Accessible frames and open shadow roots are scanned; protected frames and closed shadow roots are excluded.",
        "Review every value before applying; the model can mis-map similar labels.",
    ]

    if cached and isinstance(cached, dict) and "assignments" in cached:
        raw_assign = [
            x for x in (cached.get("assignments") or []) if isinstance(x, dict)
        ]
        raw_skip = cached.get("skipped") or []
        skipped_safe: list[dict[str, str]] = []
        for s in raw_skip:
            if isinstance(s, dict) and isinstance(s.get("field_uid"), str):
                uid = s["field_uid"]
                if uid not in fields_by_uid:
                    continue
                skipped_safe.append(
                    {
                        "field_uid": sanitize_text(uid)[:64],
                        "reason": sanitize_text(str(s.get("reason", "")))[:500],
                    }
                )
        assignments, skipped_safe = _finalize_autofill_response(
            raw_assign,
            skipped_safe,
            fields_by_uid,
            profile_bundle,
            assignment_fields,
            approved_answers,
        )
        warnings.extend(_missing_required_warnings(assignment_fields, assignments))
        return AutofillMapResponse(
            assignments=assignments,
            skipped=skipped_safe,
            warnings=warnings,
            application_id=request.application_id,
        )

    user_prompt = _build_user_prompt(
        fields_compact, profile_bundle, extras_clean, page_url_clean
    )

    try:
        client = await get_gemini_client()
        llm_options = await get_user_llm_request_options(db, user_id)
        # Tool-level Redis cache (get_cached_tool_result) is sufficient; avoid a second
        # LLM-response cache layer that can drift from this endpoint's validation rules.
        gen = await client.generate(
            prompt=user_prompt,
            system=_SYSTEM_PROMPT,
            temperature=0.15,
            max_tokens=8192,
            use_cache=False,
            **llm_options,
            user_api_key=user_api_key,
            user_id=str(user_id),
        )
    except GeminiError as e:
        logger.error("Autofill LLM error: %s", e, exc_info=True)
        raise external_service_error(
            user_facing_message_from_llm_exception(e),
            error_code=ErrorCode.LLM_SERVICE_ERROR,
        )
    except Exception as e:
        logger.error("Autofill unexpected error: %s", e, exc_info=True)
        raise internal_error("Failed to generate autofill suggestions")

    raw_text = gen.get("response") or ""
    parsed = parse_json_from_llm_response(raw_text)
    if not isinstance(parsed, dict) or "assignments" not in parsed:
        logger.warning("Autofill parse failed; raw snippet: %s", raw_text[:400])
        raise external_service_error(
            "Could not parse AI response. Try again with fewer fields visible.",
            error_code=ErrorCode.LLM_SERVICE_ERROR,
        )

    # Do not use sanitize_llm_output here — it HTML-escapes strings (e.g. ' → &#x27;), which
    # must remain plain text for form input/textarea values written by the extension.
    raw_assignments = (
        parsed.get("assignments") if isinstance(parsed.get("assignments"), list) else []
    )
    skipped = parsed.get("skipped") if isinstance(parsed.get("skipped"), list) else []

    skipped_safe: list[dict[str, str]] = []
    for s in skipped:
        if isinstance(s, dict) and isinstance(s.get("field_uid"), str):
            sk_uid = s["field_uid"]
            if sk_uid not in fields_by_uid:
                continue
            skipped_safe.append(
                {
                    "field_uid": sanitize_text(sk_uid)[:64],
                    "reason": sanitize_text(str(s.get("reason", "")))[:500],
                }
            )

    llm_raw = [x for x in raw_assignments if isinstance(x, dict)]
    assignments, skipped_safe = _finalize_autofill_response(
        llm_raw,
        skipped_safe,
        fields_by_uid,
        profile_bundle,
        assignment_fields,
        approved_answers,
    )

    warnings.extend(_missing_required_warnings(assignment_fields, assignments))

    cache_body = {
        # Sensitive plaintext is reconstructed from encrypted storage for each
        # request and must never be copied into the shared autofill cache.
        "assignments": [
            a.model_dump() for a in assignments if "sensitive" not in a.review_reasons
        ],
        "skipped": skipped_safe,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    await cache_tool_result("extension_autofill", cache_payload, cache_body)

    return AutofillMapResponse(
        assignments=assignments,
        skipped=skipped_safe,
        warnings=warnings,
        application_id=request.application_id,
    )
