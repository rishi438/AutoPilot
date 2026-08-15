"""
Agent for advanced job posting analysis and extraction from URLs and text.
Uses AI-powered content analysis to generate structured job data for application workflows.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from config.constants import _INVALID_JOB_TITLES
from utils.cache import (
    _get_job_cache_key,
    acquire_compute_lock,
    cache_job_analysis,
    get_cached_job_analysis,
    release_compute_lock,
)
from utils.llm_parsing import parse_json_from_llm_response
from utils.llm_prompting import build_llm_system_prompt
from utils.text_processing import clean_text
from workflows.state_schema import InputMethod, JobAnalysisResult, WorkflowState

logger: logging.Logger = logging.getLogger(__name__)


def _normalize_string_list(val: Any, *, split_lines: bool = False) -> list[str]:
    """Coerce LLM output to List[str]; models often emit a prose string instead of an array."""

    def _flatten_dict(obj: dict[str, Any]) -> str | None:
        for key in (
            "qualification",
            "requirement",
            "duty",
            "responsibility",
            "text",
            "description",
            "item",
            "skill",
            "name",
        ):
            raw = obj.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return None

    if val is None:
        return []
    if isinstance(val, list):
        out: list[str] = []
        for item in val:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            elif isinstance(item, dict):
                t = _flatten_dict(item)
                if t:
                    out.append(t)
        return out
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if split_lines and ("\n" in s or "\r" in s):
            lines = [ln.strip() for ln in re.split(r"\r?\n+", s) if ln.strip()]
            if len(lines) > 1:
                return lines
        return [s]
    return []


def _has_usable_job_title(value: Any) -> bool:
    """A completed application must have an actual posting title."""
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).casefold()
    return bool(normalized and normalized not in _INVALID_JOB_TITLES)


# =============================================================================
# CONSTANTS
# =============================================================================

# Content processing constraints
MIN_JOB_TEXT_LENGTH: int = 50  # Minimum characters for valid job posting
MAX_CONTENT_LENGTH_FOR_AI: int = (
    50000  # Align with extension cap / cache key normalization
)

# AI processing parameters
AI_TEMPERATURE: float = 0.1  # Low temperature for consistent extraction
AI_MAX_TOKENS: int = 4096

# =============================================================================
# PROMPTS
# =============================================================================

AI_SYSTEM_CONTEXT: str = build_llm_system_prompt(
    "Profession-neutral job-posting extractor",
    "Extract the requested structured fields from the supplied job posting.",
    extra_rules=(
        "Extract only explicitly stated facts; do not infer hidden requirements, company details, or market data.",
        "Distinguish required from preferred qualifications using the posting's own wording.",
        "Preserve exact job titles, skill names, dates, and compensation units when present.",
        "Use null or an empty list whenever the posting does not support a field.",
    ),
)

JOB_ANALYSIS_PROMPT: str = """Analyze this job posting and extract ALL structured information.

=== JOB POSTING CONTENT ===
{content}

=== EXTRACTION INSTRUCTIONS ===

Extract information into this EXACT JSON structure. Output ONLY valid JSON, no explanations.

{{
    "company_name": "<company/organization name or null>",
    "job_title": "<exact job title as posted>",
    "job_city": "<city or null if remote/not specified>",
    "job_state": "<state/province or null>",
    "job_country": "<country or null>",
    "employment_type": "<full-time | part-time | contract | temporary | internship | null>",
    "work_arrangement": "<onsite | remote | hybrid | null>",
    "salary_range": {{
        "min": <number or null>,
        "max": <number or null>,
        "currency": "<USD | EUR | GBP | etc. or null>",
        "period": "<yearly | monthly | hourly | null>"
    }},
    "is_student_position": <true if internship/entry-level/new-grad, false otherwise, null if unclear>,
    "company_size": "<Startup (1-50) | Small (51-200) | Medium (201-1000) | Large (1000+) | Enterprise (10000+) | null>",
    "posted_date": "<YYYY-MM-DD if the posting date is explicitly stated; must be between 2026-01-01 and today (inclusive); null if absent, unclear, or would violate those constraints>",
    "application_deadline": "<date string or null>",
    "benefits": ["<benefit 1>", "<benefit 2>"],

    "required_skills": [
        "<technical skill 1 - be specific, e.g., 'Python 3' not just 'Python'>",
        "<technical skill 2>",
        "<framework/tool>"
    ],
    "soft_skills": [
        "<soft skill 1, e.g., 'Communication'>",
        "<soft skill 2, e.g., 'Problem-solving'>"
    ],
    "required_qualifications": [
        "<MUST-HAVE qualification 1>",
        "<MUST-HAVE qualification 2>"
    ],
    "preferred_qualifications": [
        "<NICE-TO-HAVE qualification 1>",
        "<NICE-TO-HAVE qualification 2>"
    ],
    "education_requirements": {{
        "degree": "<High School | Associate | Bachelor's | Master's | PhD | null — if the posting says 'degree required' without specifying level, write Bachelor's>",
        "field": "<field of study, e.g. 'Computer Science', 'Engineering', or null if not specified>",
        "required": <true | false — false only if the posting explicitly says degree is NOT required>
    }},
    "years_experience_required": <number or null>,
    "language_requirements": [
        {{"language": "<language>", "proficiency": "<Native | Fluent | Intermediate | Basic>"}}
    ],

    "industry": "<Technology | Healthcare | Finance | Education | Retail | Manufacturing | etc.>",
    "role_classification": "<Engineering | Sales | Marketing | Operations | Management | Design | Data | Support | etc.>",
    "keywords": ["<important keyword 1>", "<keyword 2>", "<keyword 3>"],
    "ats_keywords": [
        "<ATS-optimized keyword that recruiters search for>",
        "<another ATS keyword>",
        "<skill variation, e.g., both 'JavaScript' and 'JS'>"
    ],

    "visa_sponsorship": <true | false | null if not mentioned>,
    "security_clearance": <true | false | null if not mentioned>,
    "max_travel_preference": <0 | 25 | 50 | 75 | 100 | null>,
    "contact_information": "<recruiter name/email or null>",

    "responsibilities": [
        "<key responsibility 1>",
        "<key responsibility 2>"
    ],
    "team_info": "<information about the team or null>",
    "reporting_to": "<who this role reports to or null>"
}}

## EXTRACTION RULES:
1. Use EXACT job title as written (don't normalize)
2. Extract ALL technical skills mentioned anywhere in the posting
3. Separate REQUIRED from PREFERRED qualifications carefully
4. For ATS keywords, include variations (React, React.js, ReactJS)
5. Set to null if information is not present - don't guess
6. For salary, extract numbers only if explicitly stated
7. Do not convert responsibilities into unstated required skills
8. Include soft skills mentioned in "ideal candidate" sections
9. responsibilities MUST be a JSON array of strings — never one long prose paragraph as a substitute. Break "What you'll do" into one element per bullet or discrete duty (minimum 3 items when the posting lists multiple duties).
10. company_name: use the employer named in the posting header, "Company:", or overview. For confidential or recruiter posts with no named legal entity, use null — do not invent a company (the dashboard will show "Unknown").
"""


def _validate_posted_date(date_str: str | None) -> str | None:
    """Validate and normalise an LLM-extracted posted_date.

    Accepts any common date string and returns it in YYYY-MM-DD format only if:
      - It can be parsed
      - It is not in the future (> today)
      - It is not before 2026-01-01 (avoids stale / hallucinated dates)

    Returns None for any invalid, future, or too-old input.

    Args:
        date_str: Raw date string from LLM output.

    Returns:
        Normalised YYYY-MM-DD string or None.
    """
    if not date_str:
        return None
    try:
        raw = str(date_str).strip()
        parsed = None
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m",
            "%B %d, %Y",
            "%b %d, %Y",
            "%m/%d/%Y",
            "%d/%m/%Y",
        ):
            try:
                parsed = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        now = datetime.now(UTC)
        min_date = datetime(2026, 1, 1, tzinfo=UTC)
        if parsed > now:
            return None
        if parsed < min_date:
            return None
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None


class JobAnalyzerAgent:
    """
    Job Analyzer Agent for extracting structured data from job postings.

    Supports pasted text and Chrome extension-extracted content, and uses AI-powered
    analysis to extract comprehensive job information including requirements,
    qualifications, and ATS-optimized keywords.

    Attributes:
        gemini_client: AI analysis client for content parsing
    """

    def __init__(self, gemini_client: Any):
        """
        Initialize JobAnalyzerAgent with required clients.

        Args:
            gemini_client: Gemini client for AI text generation

        Raises:
            TypeError: If client is None
        """
        if gemini_client is None:
            raise TypeError("Gemini client is required")

        self.gemini_client = gemini_client

    async def process(self, state: WorkflowState) -> WorkflowState:
        """
        Process job posting and extract structured data.

        Routes to the appropriate processing method based on input type (file/manual/extension),
        performs AI-powered analysis, and updates workflow state with results.
        Utilizes caching to avoid redundant LLM calls for the same job posting.

        Args:
            state: Workflow state containing job input data and processing status

        Returns:
            Updated workflow state with job analysis results or error information

        Raises:
            ValueError: If input method is invalid or required data is missing
            Exception: Any exception will be caught by _execute_agent_node
        """
        logger.info(
            f"Starting job analysis for session {state.get('session_id', 'unknown')}"
        )
        start_time: datetime = datetime.now(UTC)

        # Store user API key for use in LLM calls (BYOK mode)
        self._current_user_api_key = state.get("user_api_key")
        prefs: dict[str, Any] = state.get("workflow_preferences") or {}
        use_local = prefs.get("ai_provider") == "local"
        user_model: str | None = (
            prefs.get("local_model") if use_local else prefs.get("preferred_model")
        )
        reasoning_effort: str | None = (
            prefs.get("local_reasoning_effort") if use_local else None
        )

        try:
            analysis_result: JobAnalysisResult

            # Get job input data from state
            job_input_data = state.get("job_input_data")
            if not job_input_data:
                raise ValueError("Job input data is required for processing")
            input_method = job_input_data.get("input_method")

            # Determine cache lookup parameters
            job_url: str | None = job_input_data.get("job_url")
            job_content: str | None = job_input_data.get("job_content")

            # Check cache first
            cached_analysis = await get_cached_job_analysis(
                job_url=job_url,
                job_content=job_content,
            )
            if cached_analysis and _has_usable_job_title(
                cached_analysis.get("job_title")
            ):
                logger.info("Using cached job analysis result")
                state["job_analysis"] = cached_analysis
                state["job_analysis"]["from_cache"] = True
                return state
            if cached_analysis:
                logger.warning(
                    "Ignoring cached job analysis without a usable job title"
                )

            # Stampede protection: only one coroutine should compute for this job at a time
            cache_key = _get_job_cache_key(job_url, job_content)
            lock_claimed = await acquire_compute_lock(cache_key) if cache_key else True

            if not lock_claimed:
                import asyncio as _asyncio

                logger.info(
                    "Compute lock busy for job analysis, waiting for cache population"
                )
                for _ in range(6):  # up to ~3 seconds
                    await _asyncio.sleep(0.5)
                    cached_analysis = await get_cached_job_analysis(
                        job_url=job_url, job_content=job_content
                    )
                    if cached_analysis and _has_usable_job_title(
                        cached_analysis.get("job_title")
                    ):
                        logger.info("Using cached job analysis result (lock wait)")
                        state["job_analysis"] = cached_analysis
                        state["job_analysis"]["from_cache"] = True
                        return state
                logger.warning(
                    "Compute lock wait timed out for job analysis, computing independently"
                )

            try:
                # Process input based on input method
                if input_method in (
                    InputMethod.FILE,
                    InputMethod.MANUAL,
                    InputMethod.EXTENSION,
                ):
                    # For file, manual text, and extension-extracted content
                    if not job_content:
                        raise ValueError(
                            "Job content is required for file/manual/extension input method"
                        )
                    # For extension, use "extension" as source type
                    source_type = (
                        "extension"
                        if input_method == InputMethod.EXTENSION
                        else "manual"
                    )
                    analysis_result = await self._process_manual_input(
                        job_content,
                        source_type,
                        user_model=user_model,
                        force_local=use_local,
                        local_reasoning_effort=reasoning_effort,
                    )
                else:
                    raise ValueError(f"Unknown job input method: {input_method}")

                # Calculate and record processing time
                processing_time: float = (
                    datetime.now(UTC) - start_time
                ).total_seconds()
                analysis_result.processing_time = processing_time
                logger.info(f"Job analysis completed in {processing_time:.2f} seconds")

                # Update state with successful results
                analysis_dict = analysis_result.to_dict()
                state["job_analysis"] = analysis_dict

                # Cache the result for future requests
                await cache_job_analysis(
                    analysis=analysis_dict,
                    job_url=job_url,
                    job_content=job_content,
                )
            finally:
                if lock_claimed and cache_key:
                    await release_compute_lock(cache_key)

            logger.info("Job analysis completed successfully and cached")

        except TimeoutError:
            logger.error("Job analyzer timed out", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Job analyzer failed: {str(e)}", exc_info=True)
            raise

        return state

    async def _process_manual_input(
        self,
        job_text: str,
        source_type: str = "manual",
        *,
        user_model: str | None = None,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> JobAnalysisResult:
        """
        Process manually entered or extension-extracted job posting text.

        Args:
            job_text: Raw job posting text entered by user or extracted by extension
            source_type: Source type ("manual", "file", or "extension")

        Returns:
            Structured job analysis result from text input

        Raises:
            ValueError: If job text is too short or processing fails
        """
        logger.info(f"Processing {source_type} job input")

        # Validate minimum content length to ensure quality analysis
        if not job_text or len(job_text.strip()) < MIN_JOB_TEXT_LENGTH:
            raise ValueError(
                "Job posting text is too short. Please paste the complete job posting including title, company, description, and any other details."
            )

        # Process the validated text using AI extraction
        analysis_result: JobAnalysisResult = await self._parse_generic_job_content(
            job_text,
            source_type,
            user_model=user_model,
            force_local=force_local,
            local_reasoning_effort=local_reasoning_effort,
        )
        return analysis_result

    async def _parse_generic_job_content(
        self,
        content: str,
        source: str,
        *,
        user_model: str | None = None,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> JobAnalysisResult:
        """
        Parse job content using AI to extract structured information.

        Args:
            content: Raw job posting content
            source: Source type ("file", "manual", or "extension")

        Returns:
            Structured job analysis result

        Raises:
            ValueError: If AI parsing fails or returns invalid data
        """
        logger.info("Processing job content using AI")

        # Clean and truncate the content
        cleaned_content: str = clean_text(content)
        truncated_content = cleaned_content[:MAX_CONTENT_LENGTH_FOR_AI]

        try:
            # Build prompt by replacing the content placeholder
            prompt: str = JOB_ANALYSIS_PROMPT.replace("{content}", truncated_content)

            # Generate AI analysis with expert system context
            response: dict[str, Any] = await self.gemini_client.generate(
                prompt=prompt,
                system=AI_SYSTEM_CONTEXT,
                temperature=AI_TEMPERATURE,
                max_tokens=AI_MAX_TOKENS,
                user_api_key=self._current_user_api_key,
                model=user_model,
                structured_output=True,
                force_local=force_local,
                local_reasoning_effort=local_reasoning_effort,
            )

            # Use our shared utility function to parse JSON from LLM response
            parsed_data: dict[str, Any] = parse_json_from_llm_response(response)

            # Validate parsed data
            if not parsed_data:
                raise ValueError("No valid JSON response received from AI")

            # Helper to safely get string values
            def get_str(key: str, default: str = "") -> str:
                val = parsed_data.get(key)
                return str(val).strip() if val else default

            # Create structured result from flat JSON (new format)
            job_title = get_str("job_title")
            if not _has_usable_job_title(job_title):
                raise ValueError(
                    "The job posting did not contain a usable job title. "
                    "Please paste the complete posting or use the job URL."
                )

            job_analysis_result = JobAnalysisResult(
                source=source,
                # Basic information
                job_title=job_title,
                company_name=get_str("company_name"),
                job_city=parsed_data.get("job_city"),
                job_state=parsed_data.get("job_state"),
                job_country=parsed_data.get("job_country"),
                employment_type=parsed_data.get("employment_type"),
                work_arrangement=parsed_data.get("work_arrangement"),
                salary_range=parsed_data.get("salary_range") or {},
                posted_date=_validate_posted_date(parsed_data.get("posted_date")),
                application_deadline=parsed_data.get("application_deadline"),
                benefits=_normalize_string_list(parsed_data.get("benefits")),
                is_student_position=parsed_data.get("is_student_position"),
                company_size=parsed_data.get("company_size"),
                # Skills and qualifications
                required_skills=_normalize_string_list(
                    parsed_data.get("required_skills")
                ),
                soft_skills=_normalize_string_list(parsed_data.get("soft_skills")),
                required_qualifications=_normalize_string_list(
                    parsed_data.get("required_qualifications")
                ),
                preferred_qualifications=_normalize_string_list(
                    parsed_data.get("preferred_qualifications")
                ),
                education_requirements=parsed_data.get("education_requirements") or {},
                years_experience_required=parsed_data.get("years_experience_required"),
                language_requirements=_normalize_string_list(
                    parsed_data.get("language_requirements")
                ),
                # Classification and keywords
                industry=parsed_data.get("industry"),
                role_classification=parsed_data.get("role_classification"),
                keywords=_normalize_string_list(parsed_data.get("keywords")),
                ats_keywords=_normalize_string_list(parsed_data.get("ats_keywords")),
                # Additional details
                visa_sponsorship=parsed_data.get("visa_sponsorship"),
                security_clearance=parsed_data.get("security_clearance"),
                max_travel_preference=parsed_data.get("max_travel_preference"),
                contact_information=get_str("contact_information"),
                # Role context
                responsibilities=_normalize_string_list(
                    parsed_data.get("responsibilities"), split_lines=True
                ),
                team_info=parsed_data.get("team_info"),
                reporting_to=parsed_data.get("reporting_to"),
            )

            return job_analysis_result

        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse AI response as JSON: {str(e)}") from e
        except Exception as e:
            raise ValueError(f"AI extraction failed: {str(e)}") from e
