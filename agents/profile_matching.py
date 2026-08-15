"""
Agent for comprehensive AI-powered profile matching using LLM.
Performs deep semantic analysis to determine candidate-job compatibility with intelligent insights.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from utils.llm_client import get_gemini_client
from utils.llm_parsing import parse_json_from_llm_response
from utils.llm_prompting import build_llm_system_prompt
from workflows.state_schema import WorkflowState

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

# LLM Configuration
LLM_TEMPERATURE: float = 0.2  # Low temperature for consistent, analytical responses
LLM_MAX_TOKENS: int = 5000

# =============================================================================
# PROMPTS
# =============================================================================

SYSTEM_CONTEXT: str = build_llm_system_prompt(
    "Profession-neutral candidate-to-job evidence analyst",
    "Assess candidate fit against the supplied job requirements and preferences.",
    extra_rules=(
        "Support every matched qualification with supplied profile evidence.",
        "Do not infer hidden skills, employer intentions, applicant-pool rankings, or success probabilities as facts.",
        "Treat total career years as domain-specific experience only when dated work history supports that claim.",
        "Preserve unknown visa, clearance, location, certification, and preference states as uncertainty.",
        "Apply stated legal or credential requirements as written; do not discount them using generic hiring assumptions.",
    ),
)

PROFILE_MATCHING_PROMPT: str = """You are analyzing a candidate's fit for a job. Think like an elite executive recruiter.

=== CANDIDATE PROFILE ===
{user_profile}

=== JOB REQUIREMENTS ===
{job_analysis}

=== YOUR TASK ===
Analyze the match between this candidate and job. Follow the step-by-step process below, then output ONLY valid JSON.

IMPORTANT: Your response must be ONLY the JSON object below. No explanations, no markdown, no text before or after. Start with {{ and end with }}.

=== REQUIRED JSON OUTPUT FORMAT ===

{{
    "executive_summary": {{
        "fit_assessment": "<Write 2-3 sentences summarizing the overall match. Be specific about WHY they match or don't match. Use actual details from their profile.>",
        "recommendation": "<Choose exactly one: STRONG_MATCH | GOOD_MATCH | MODERATE_MATCH | WEAK_MATCH | NOT_RECOMMENDED>",
        "confidence_level": "<Choose exactly one: HIGH | MEDIUM | LOW>",
        "one_line_verdict": "<One sentence a hiring manager would say, e.g., 'Strong technical fit but may need visa sponsorship'>"
    }},

    "qualification_analysis": {{
        "overall_score": <number between 0.0 and 1.0>,
        "skills_assessment": {{
            "score": <number between 0.0 and 1.0>,
            "matched_skills": [
                {{"skill": "<skill name from job requirements>", "evidence": "<exact quote or reference from their profile proving this>", "strength": "<STRONG or MODERATE or BASIC>"}}
            ],
            "missing_critical_skills": [
                {{"skill": "<required skill they don't have>", "importance": "<CRITICAL or IMPORTANT or NICE_TO_HAVE>", "can_learn_quickly": <true or false>, "learning_time_estimate": "<e.g., '2-4 weeks' or '3-6 months'>"}}
            ],
            "hidden_skills": [
                {{"skill": "<skill they likely have but didn't explicitly list>", "reasoning": "<why you believe they have this based on their experience>"}}
            ],
            "skill_gaps_analysis": "<2-3 sentences honestly assessing the severity of any skill gaps>"
        }},
        "experience_assessment": {{
            "score": <number between 0.0 and 1.0>,
            "years_evaluation": {{
                "candidate_years": <integer, their years of experience>,
                "required_years": <integer, job's required years>,
                "assessment": "<EXCEEDS or MEETS or CLOSE or BELOW>",
                "context": "<Explain if the years gap actually matters in this industry/role>"
            }},
            "experience_quality": {{
                "relevance_score": <number between 0.0 and 1.0>,
                "most_relevant_experience": "<Which specific role from their history is most relevant and why>",
                "transferable_achievements": ["<specific achievement that would impress this employer>"],
                "experience_gaps": ["<specific experience the job wants that they lack>"],
                "career_trajectory": "<ASCENDING or LATERAL or DESCENDING or EARLY_CAREER>"
            }},
            "industry_fit": {{
                "score": <number between 0.0 and 1.0>,
                "same_industry": <true or false>,
                "transferability_assessment": "<How well their industry experience transfers to this role>"
            }}
        }},
        "education_assessment": {{
            "score": <number between 0.0 and 1.0>,
            "degree_level_match": "<EXCEEDS or MEETS or BELOW or NOT_REQUIRED>",
            "field_relevance": "<EXACT_MATCH or RELATED or TRANSFERABLE or UNRELATED>",
            "education_notes": "<Any relevant observations>",
            "profile_education_summary": "<One line listing institution and degree from the EDUCATION section of the profile when present; if none, say so>"
        }},
        "certification_assessment": {{
            "score": <number between 0.0 and 1.0>,
            "matched_certifications": ["<certs they have that match job requirements>"],
            "missing_required": ["<required certs they don't have>"],
            "recommended_certifications": ["<certs that would strengthen their application>"]
        }}
    }},

    "preference_analysis": {{
        "overall_score": <number between 0.0 and 1.0>,
        "salary_fit": {{
            "score": <number between 0.0 and 1.0>,
            "assessment": "<WITHIN_RANGE or ABOVE_RANGE or BELOW_RANGE or UNKNOWN>",
            "notes": "<Explain the salary situation>"
        }},
        "work_arrangement_fit": {{
            "score": <number between 0.0 and 1.0>,
            "candidate_preference": ["<their stated preferences>"],
            "job_offers": "<what the job offers: remote/hybrid/onsite>",
            "compatible": <true or false>
        }},
        "location_fit": {{
            "score": <number between 0.0 and 1.0>,
            "assessment": "<Explain location compatibility: perfect match, same metro area / short commute, needs relocation, remote OK, etc.>"
        }},
        "company_culture_signals": {{
            "score": <number between 0.0 and 1.0>,
            "potential_fit_indicators": ["<signals they might fit the culture>"],
            "potential_concerns": ["<potential culture mismatches>"]
        }},
        "career_growth_alignment": {{
            "score": <number between 0.0 and 1.0>,
            "assessment": "<Does this role advance their career in the direction they want?>"
        }}
    }},

    "deal_breaker_analysis": {{
        "overall_passed": <true or false - false means they CANNOT take this job>,
        "deal_breakers_found": [
            {{"issue": "<description of the blocker>", "severity": "<BLOCKING or CONCERNING or MINOR>", "workaround": "<possible solution or 'None'>"}}
        ],
        "location_viable": <true or false>,
        "visa_viable": <true or false>,
        "student_status_compatible": <true or false>,
        "security_clearance_viable": <true or false>,
        "certification_requirements_met": <true or false>
    }},

    "competitive_positioning": {{
        "estimated_candidate_pool_percentile": <integer 0-100, where they rank vs typical applicants>,
        "strengths_vs_typical_applicant": ["<advantage they have over other candidates>"],
        "weaknesses_vs_typical_applicant": ["<disadvantage vs other candidates>"],
        "unique_value_proposition": "<What makes them stand out from the typical applicant?>",
        "likely_competition": "<Who else typically applies for roles like this?>"
    }},

    "application_strategy": {{
        "should_apply": <true or false>,
        "application_priority": "<HIGH or MEDIUM or LOW or SKIP>",
        "success_probability": "<HIGH or MEDIUM or LOW or VERY_LOW>",
        "key_talking_points": [
            "<Specific point #1 they MUST emphasize - use their actual experience>",
            "<Specific point #2>",
            "<Specific point #3>"
        ],
        "address_these_concerns": [
            {{"concern": "<what the employer might worry about>", "how_to_address": "<specific strategy to address it>"}}
        ],
        "resume_optimization_tips": [
            "<Specific, actionable tip for THIS job>",
            "<Another specific tip>"
        ],
        "cover_letter_angle": "<The narrative/story they should tell in their cover letter>",
        "interview_preparation": [
            {{"likely_question": "<question they'll probably be asked — be specific to this company and role>", "suggested_answer_strategy": "<concrete approach: what experience to reference, what angle to take>"}},
            {{"likely_question": "<second question>", "suggested_answer_strategy": "<strategy>"}},
            {{"likely_question": "<third question>", "suggested_answer_strategy": "<strategy>"}},
            {{"likely_question": "<fourth question>", "suggested_answer_strategy": "<strategy>"}},
            {{"likely_question": "<fifth question>", "suggested_answer_strategy": "<strategy>"}}
        ],
        "networking_suggestions": "<Give 2-3 concrete tactics: specific team names or roles to target on professional networking platforms, what to reference in the outreach message, and any warm-path angles based on the candidate's background. Be actionable, not generic.>"
    }},

    "risk_assessment": {{
        "candidate_risks": [
            {{"risk": "<risk the EMPLOYER faces hiring this person>", "mitigation": "<how candidate can reduce this risk>"}}
        ],
        "role_risks": [
            {{"risk": "<risk the CANDIDATE faces taking this role>", "consideration": "<what they should think about>"}}
        ],
        "red_flags_for_employer": ["<things that might concern the employer>"],
        "yellow_flags_for_candidate": ["<things the candidate should investigate about this company/role>"]
    }},

    "final_scores": {{
        "qualification_score": <number between 0.0 and 1.0>,
        "preference_score": <number between 0.0 and 1.0>,
        "deal_breaker_score": <1.0 if all passed, 0.0 if any failed>,
        "overall_match_score": <number between 0.0 and 1.0>,
        "weighted_recommendation_score": <number between 0.0 and 1.0>
    }},

    "ai_insights": {{
        "unexpected_findings": "<Anything surprising you noticed during this analysis>",
        "career_advice": "<Brief advice for this candidate beyond this specific job>",
        "alternative_roles": ["<other job titles they might be well-suited for>"],
        "skill_development_priority": ["<skill #1 to develop>", "<skill #2>", "<skill #3>"]
    }}
}}

## STEP-BY-STEP ANALYSIS PROCESS:

**STEP 1: DEAL BREAKER CHECK (Do this first!)**
- Location: Can they work where the job is? (Consider remote, relocation willingness, AND geographic proximity — cities within the same metro area, e.g. Hoboken NJ ↔ New York City, or Oakland ↔ San Francisco, are NOT a relocation requirement and should be treated as a viable commute)
- Visa: Do they need sponsorship? Does the job offer it?
- Student Status: Is this a student job? Are they a student?
- Certifications/Licenses: Any legally required credentials missing?
- Security Clearance: Required but not held?
→ If ANY deal breaker fails, recommendation cannot be higher than "NOT_RECOMMENDED"

**STEP 2: SKILLS DEEP DIVE**
For EACH required skill in the job:
- Is it in their profile? (exact match = 100% confidence)
- Is a synonym in their profile? (e.g., "JS" for "JavaScript" = 95% confidence)
- Is an equivalent term explicitly supported by their profile? Cite that evidence.
- Do not infer an unlisted skill from a title, another skill, or a generic role expectation.
- Is it completely missing? → Note as gap, estimate learning time

**STEP 3: EXPERIENCE QUALITY ANALYSIS**
- Calculate relevance of EACH past role to THIS job (0-100%)
- Weight recent roles higher (last 3 years = full weight, older = diminishing)
- Identify transferable achievements that would impress this employer
- Spot experience gaps that might concern the hiring manager

**STEP 4: COMPETITIVE POSITIONING**
- Compared to the TYPICAL applicant for this role, where does this candidate rank?
- What's their unique advantage? (something most applicants won't have)
- What's their biggest weakness vs. competition?

**STEP 5: STRATEGIC RECOMMENDATIONS**
- Should they apply? (Yes/No/Maybe with conditions)
- What should they emphasize in their application?
- What concerns should they proactively address?
- What specific resume changes would help?
- What interview questions should they prepare for?

## SCORING CALIBRATION:

**Qualification Score (0.0 - 1.0):**
- 0.9-1.0: Exceeds requirements, ideal candidate
- 0.7-0.89: Meets most requirements, strong candidate
- 0.5-0.69: Meets some requirements, could work with training
- 0.3-0.49: Below requirements but has potential
- 0.0-0.29: Significantly underqualified

**Preference Score (0.0 - 1.0):**
- 0.9-1.0: Perfect alignment with candidate's goals
- 0.7-0.89: Good alignment, minor compromises
- 0.5-0.69: Acceptable, notable trade-offs
- 0.3-0.49: Misaligned in important ways
- 0.0-0.29: Poor fit for candidate's goals

**Location Fit Sub-Score (0.0 - 1.0):**
- 0.9-1.0: Exact city match OR same metro area / easy commute (e.g., Hoboken → NYC, Oakland → SF)
- 0.7-0.89: Same region, reasonable commute or remote-friendly role
- 0.4-0.69: Different metro but candidate is open to relocation
- 0.1-0.39: Significant relocation required, candidate NOT open to it
- 0.0: Location is an absolute blocker (e.g., international move, no relocation willingness)

**Deal Breaker Score:**
- 1.0: All deal breakers passed
- 0.0: At least one deal breaker failed

**Recommendation Mapping:**
- STRONG_MATCH: Qualification ≥ 0.8 AND Preference ≥ 0.7 AND Deal Breakers passed
- GOOD_MATCH: Qualification ≥ 0.6 AND Preference ≥ 0.6 AND Deal Breakers passed
- MODERATE_MATCH: Qualification ≥ 0.4 AND Preference ≥ 0.5 AND Deal Breakers passed
- WEAK_MATCH: Below moderate thresholds but Deal Breakers passed
- NOT_RECOMMENDED: Deal Breakers failed OR Qualification < 0.3

## CRITICAL RULES:
1. Use ACTUAL DETAILS from their profile - never be generic
2. If information is MISSING, explicitly state it affects confidence
3. Consider INDUSTRY NORMS - expectations vary by field
4. Treat required qualifications as written, especially legal, safety, license, and clearance requirements
5. Do not invent hidden skills; record transferable evidence only when the profile supports it
6. RECENCY matters - weight recent experience more heavily
7. GROWTH POTENTIAL counts - consider ability to learn, not just current state
8. Be HONEST about weaknesses - but always suggest how to address them
9. Think about BOTH SIDES - risks for employer AND risks for candidate
10. ACTIONABLE advice only - every insight should lead to a specific action
11. LOCATION PROXIMITY matters - use your knowledge of real-world geography. Candidates in cities adjacent to or within the same metro area as the job location (e.g., Hoboken/Jersey City ↔ New York City, Oakland/Berkeley ↔ San Francisco, Evanston ↔ Chicago, Cambridge ↔ Boston, Alexandria/Arlington ↔ Washington DC) should receive a HIGH location score (0.85-1.0) since no relocation is needed. Only assign a low location score if a genuine cross-city or cross-state move would be required and the candidate has not expressed willingness to relocate.
12. NEVER include internal reasoning, uncertainty commentary, or parenthetical self-corrections in any output field. All string values must be clean, final statements — no "wait, ...", no "(this might be a typo)", no "actually, ..." style annotations. Resolve any uncertainty internally before writing the output.
13. ALWAYS use second-person language ("you", "your") when referring to the candidate in all text fields — never use third-person ("he", "she", "they", "his", "her", "the candidate"). Every insight must speak directly to the user reading the report.
"""


class ProfileMatchingAgent:
    """
    AI-Powered Profile Matching Agent.

    Uses advanced LLM capabilities to perform deep semantic analysis of
    candidate-job fit, going far beyond simple keyword matching to provide
    intelligent, recruiter-level insights and recommendations.

    This agent answers:
    - "Am I qualified for this job?" (Qualification Analysis)
    - "Do I want this job?" (Preference Analysis)
    - "Can I actually take this job?" (Deal Breaker Analysis)
    - "How should I apply?" (Application Strategy)
    - "What are the risks?" (Risk Assessment)
    """

    def __init__(self) -> None:
        """Initialize Profile Matching Agent."""
        self.gemini_client = None

    async def process(self, state: WorkflowState) -> WorkflowState:
        """
        Process user profile matching against job requirements using AI analysis.

        Performs comprehensive LLM-powered analysis to determine candidate fit
        with intelligent insights, competitive positioning, and actionable advice.

        Args:
            state: Current workflow state containing user profile and job analysis data

        Returns:
            Updated workflow state with comprehensive profile matching results

        Raises:
            ValueError: If user profile or job analysis is missing from state
        """
        logger.info(f"Starting AI profile matching for session {state['session_id']}")
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
            # Validate required data
            user_profile: dict[str, Any] = state.get("user_profile")
            job_analysis: dict[str, Any] | None = state.get("job_analysis")

            if not user_profile:
                raise ValueError("User profile is required for matching analysis")
            if not job_analysis:
                raise ValueError("Job analysis is required for matching analysis")

            # Initialize Gemini client
            self.gemini_client = await get_gemini_client()

            # Perform AI-powered matching analysis
            matching_result: dict[str, Any] = await self._analyze_match(
                user_profile,
                job_analysis,
                user_model=user_model,
                force_local=use_local,
                local_reasoning_effort=reasoning_effort,
            )

            # Add metadata
            matching_result["processing_time"] = (
                datetime.now(UTC) - start_time
            ).total_seconds()
            matching_result["analysis_method"] = "AI_POWERED"
            matching_result["model_used"] = user_model or "gemini"

            # Extract scores for backward compatibility
            final_scores = matching_result.get("final_scores", {})
            matching_result["qualification_score"] = final_scores.get(
                "qualification_score", 0.0
            )
            matching_result["preference_score"] = final_scores.get(
                "preference_score", 0.0
            )
            matching_result["deal_breaker_score"] = final_scores.get(
                "deal_breaker_score", 0.0
            )
            matching_result["overall_score"] = final_scores.get(
                "overall_match_score", 0.0
            )

            # Store results in workflow state
            state["profile_matching"] = matching_result

            # Log summary
            exec_summary = matching_result.get("executive_summary", {})
            logger.info(
                f"AI profile matching completed for session {state['session_id']} - "
                f"Recommendation: {exec_summary.get('recommendation', 'N/A')}, "
                f"Overall Score: {matching_result.get('overall_score', 0):.2f}"
            )

        except TimeoutError:
            logger.error("AI profile matching timed out", exc_info=True)
            state["profile_matching"] = self._create_error_result("Analysis timed out")
            raise
        except Exception as e:
            logger.error(f"AI profile matching failed: {str(e)}", exc_info=True)
            state["profile_matching"] = self._create_error_result(str(e))
            raise

        return state

    async def _analyze_match(
        self,
        user_profile: dict[str, Any],
        job_analysis: dict[str, Any],
        *,
        user_model: str | None = None,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute comprehensive AI-powered matching analysis.

        Uses LLM to perform deep semantic analysis of candidate-job fit,
        providing intelligent insights that go far beyond keyword matching.

        Args:
            user_profile: User profile data with skills, experience, and preferences
            job_analysis: Job analysis result with requirements and details

        Returns:
            Comprehensive matching analysis with scores, insights, and recommendations
        """
        logger.info("Performing AI-powered matching analysis")

        # Format inputs for LLM
        formatted_profile: str = self._format_user_profile(user_profile)
        formatted_job: str = self._format_job_analysis(job_analysis)

        # Build the prompt
        prompt: str = PROFILE_MATCHING_PROMPT.format(
            user_profile=formatted_profile, job_analysis=formatted_job
        )

        # Call LLM for analysis
        response = await self.gemini_client.generate(
            prompt=prompt,
            system=SYSTEM_CONTEXT,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            user_api_key=self._current_user_api_key,
            model=user_model,
            structured_output=True,
            force_local=force_local,
            local_reasoning_effort=local_reasoning_effort,
        )

        # Handle safety filter
        if response.get("filtered"):
            logger.warning("LLM response was filtered, using fallback analysis")
            return self._create_filtered_result(response.get("response", ""))

        # Parse the response
        response_text: str = response.get("response", "")
        result: dict[str, Any] = parse_json_from_llm_response(response_text)

        if not result:
            logger.error("Failed to parse LLM response as JSON")
            return self._create_parse_error_result(response_text)

        logger.info("AI matching analysis completed successfully")
        return result

    @staticmethod
    def _format_salary_range(salary_range: Any) -> str:
        """Format a salary range with its supplied currency; never assume USD."""
        if not isinstance(salary_range, dict):
            return str(salary_range) if salary_range else "Not specified"

        minimum = salary_range.get("min")
        maximum = salary_range.get("max")
        if minimum is None or maximum is None:
            return "Not specified"

        currency = (
            str(salary_range.get("currency") or "Currency unspecified").strip().upper()
        )
        try:
            return f"{currency} {minimum:,} - {currency} {maximum:,}"
        except (TypeError, ValueError):
            return f"{currency} {minimum} - {currency} {maximum}"

    def _format_user_profile(self, profile: dict[str, Any]) -> str:
        """
        Format user profile data for LLM consumption.

        Creates a comprehensive, readable representation of the candidate's
        profile that the LLM can analyze effectively.

        Args:
            profile: User profile dictionary

        Returns:
            Formatted string representation of the profile
        """
        # Basic Information
        basic_info = f"""
### BASIC INFORMATION
- Full Name: {profile.get('full_name', 'Not provided')}
- Professional Title: {profile.get('professional_title', 'Not provided')}
- Location: {profile.get('city', 'N/A')}, {profile.get('state', 'N/A')}, {profile.get('country', 'N/A')}
- Years of Experience: {profile.get('years_experience', 0)}
- Currently a Student: {profile.get('is_student', False)}
"""

        # Professional Summary
        summary = profile.get("summary", "")
        summary_section = f"""
### PROFESSIONAL SUMMARY
{summary if summary else 'No summary provided'}
"""

        # Skills
        skills = profile.get("skills", [])
        skills_section = f"""
### SKILLS
{', '.join(skills) if skills else 'No skills listed'}
"""

        # Work Experience
        work_exp = profile.get("work_experience", [])
        exp_section = "\n### WORK EXPERIENCE\n"
        if work_exp:
            for i, exp in enumerate(work_exp, 1):
                exp_section += f"""
**Position {i}:**
- Job Title: {exp.get('job_title', 'N/A')}
- Company: {exp.get('company', 'N/A')}
- Duration: {exp.get('start_date', 'N/A')} to {exp.get('end_date', 'Present')}
- Current Position: {exp.get('is_current', False)}
- Description: {exp.get('description', 'No description provided')}
"""
        else:
            exp_section += "No work experience listed\n"

        edu_rows = profile.get("education", []) or []
        edu_section = "\n### EDUCATION\n"
        if edu_rows:
            for i, edu in enumerate(edu_rows, 1):
                inst = edu.get("institution", "N/A")
                deg = edu.get("degree", "N/A")
                fos = edu.get("field_of_study") or "Not specified"
                start_d = edu.get("start_date", "N/A")
                end_d = edu.get("end_date")
                if edu.get("is_current"):
                    dur = f"{start_d} to Present (currently enrolled)"
                else:
                    dur = f"{start_d} to {end_d or 'N/A'}"
                edu_section += f"""
**Program {i}:**
- Institution: {inst}
- Degree: {deg}
- Field of study: {fos}
- Duration: {dur}
"""
        else:
            edu_section += "No structured education entries listed.\n"

        # Preferences
        salary_str = self._format_salary_range(profile.get("desired_salary_range"))

        preferences_section = f"""
### JOB PREFERENCES
- Desired Salary Range: {salary_str}
- Preferred Work Arrangements: {', '.join(profile.get('work_arrangements', [])) or 'Not specified'}
- Preferred Job Types: {', '.join(profile.get('job_types', [])) or 'Not specified'}
- Preferred Company Sizes: {', '.join(profile.get('desired_company_sizes', [])) or 'Not specified'}
- Willing to Relocate: {profile.get('willing_to_relocate', False)}
- Maximum Travel: {profile.get('max_travel_preference', 'Not specified')}
"""

        # Requirements/Constraints
        constraints_section = f"""
### CANDIDATE REQUIREMENTS/CONSTRAINTS
- Requires Visa Sponsorship: {profile.get('requires_visa_sponsorship', False)}
- Work authorization: {profile.get('work_authorization') or 'Not specified'}
- Has Security Clearance: {profile.get('has_security_clearance', False)}
"""

        return (
            basic_info
            + summary_section
            + skills_section
            + exp_section
            + edu_section
            + preferences_section
            + constraints_section
        )

    def _format_job_analysis(self, job: dict[str, Any]) -> str:
        """
        Format job analysis data for LLM consumption.

        Creates a comprehensive, readable representation of the job
        requirements that the LLM can analyze effectively.

        Args:
            job: Job analysis dictionary

        Returns:
            Formatted string representation of the job
        """
        # Basic Job Info
        basic_info = f"""
### JOB OVERVIEW
- Job Title: {job.get('job_title', 'Not provided')}
- Company: {job.get('company_name', 'Not provided')}
- Location: {job.get('job_city', 'N/A')}, {job.get('job_state', 'N/A')}, {job.get('job_country', 'N/A')}
- Work Arrangement: {job.get('work_arrangement', 'Not specified')}
- Employment Type: {job.get('employment_type', 'Not specified')}
- Company Size: {job.get('company_size', 'Not specified')}
- Industry: {job.get('industry', 'Not specified')}
"""

        # Salary
        salary_str = self._format_salary_range(job.get("salary_range"))

        salary_section = f"""
### COMPENSATION
- Salary Range: {salary_str}
"""

        # Requirements
        required_skills = job.get("required_skills", [])
        soft_skills = job.get("soft_skills", [])
        required_quals = job.get("required_qualifications", [])
        preferred_quals = job.get("preferred_qualifications", [])

        requirements_section = f"""
### REQUIREMENTS

**Required Technical Skills:**
{chr(10).join(['- ' + s for s in required_skills]) if required_skills else '- Not specified'}

**Soft Skills:**
{chr(10).join(['- ' + s for s in soft_skills]) if soft_skills else '- Not specified'}

**Required Qualifications:**
{chr(10).join(['- ' + q for q in required_quals]) if required_quals else '- Not specified'}

**Preferred Qualifications:**
{chr(10).join(['- ' + q for q in preferred_quals]) if preferred_quals else '- Not specified'}

**Experience Required:** {job.get('years_experience_required', 'Not specified')} years
"""

        # Job Details
        responsibilities = job.get("responsibilities", [])
        benefits = job.get("benefits", [])

        details_section = f"""
### JOB DETAILS

**Key Responsibilities:**
{chr(10).join(['- ' + r for r in responsibilities]) if responsibilities else '- Not specified'}

**Benefits:**
{chr(10).join(['- ' + b for b in benefits]) if benefits else '- Not specified'}
"""

        # Special Requirements
        special_section = f"""
### SPECIAL REQUIREMENTS
- Student Position: {job.get('is_student_position', False)}
- Visa Sponsorship Available: {job.get('visa_sponsorship', False)}
- Security Clearance Required: {job.get('security_clearance', False)}
- Travel Required: {job.get('max_travel_preference', 'Not specified')}
"""

        # ATS Keywords (if available)
        ats_keywords = job.get("ats_keywords", [])
        keywords_section = ""
        if ats_keywords:
            keywords_section = f"""
### ATS/IMPORTANT KEYWORDS
{', '.join(ats_keywords)}
"""

        return (
            basic_info
            + salary_section
            + requirements_section
            + details_section
            + special_section
            + keywords_section
        )

    def _create_error_result(self, error_message: str) -> dict[str, Any]:
        """
        Create a fallback result when analysis fails.

        Args:
            error_message: Description of the error

        Returns:
            Minimal result structure with error information
        """
        return {
            "executive_summary": {
                "fit_assessment": f"Analysis could not be completed due to an error: {error_message}",
                "recommendation": "UNKNOWN",
                "confidence_level": "LOW",
                "one_line_verdict": "Unable to assess - please try again",
            },
            "qualification_score": 0.0,
            "preference_score": 0.0,
            "deal_breaker_score": 0.0,
            "overall_score": 0.0,
            "final_scores": {
                "qualification_score": 0.0,
                "preference_score": 0.0,
                "deal_breaker_score": 0.0,
                "overall_match_score": 0.0,
            },
            "error": True,
            "error_message": error_message,
            "analysis_method": "ERROR_FALLBACK",
        }

    def _create_filtered_result(self, filter_message: str) -> dict[str, Any]:
        """
        Create a result when content is filtered by safety settings.

        Args:
            filter_message: Message from the safety filter

        Returns:
            Result structure with filter information
        """
        return {
            "executive_summary": {
                "fit_assessment": "Analysis was limited due to content filtering. Please review the job posting for any sensitive content.",
                "recommendation": "UNKNOWN",
                "confidence_level": "LOW",
                "one_line_verdict": "Unable to complete full assessment",
            },
            "qualification_score": 0.5,
            "preference_score": 0.5,
            "deal_breaker_score": 1.0,
            "overall_score": 0.5,
            "final_scores": {
                "qualification_score": 0.5,
                "preference_score": 0.5,
                "deal_breaker_score": 1.0,
                "overall_match_score": 0.5,
            },
            "safety_filter_message": filter_message,
            "filtered": True,
            "analysis_method": "FILTERED_FALLBACK",
        }

    def _create_parse_error_result(self, raw_response: str) -> dict[str, Any]:
        """
        Create a safe result when JSON parsing fails.

        Args:
            raw_response: The raw LLM response that couldn't be parsed

        Returns:
            Result structure requesting regeneration
        """
        return {
            "executive_summary": {
                "fit_assessment": "The generated analysis could not be validated. Please try again.",
                "recommendation": "UNKNOWN",
                "confidence_level": "LOW",
                "one_line_verdict": "No validated assessment is available",
            },
            "qualification_score": 0.5,
            "preference_score": 0.5,
            "deal_breaker_score": 1.0,
            "overall_score": 0.5,
            "final_scores": {
                "qualification_score": 0.5,
                "preference_score": 0.5,
                "deal_breaker_score": 1.0,
                "overall_match_score": 0.5,
            },
            "parse_error": True,
            "analysis_method": "PARSE_ERROR_FALLBACK",
        }
