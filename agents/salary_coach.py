"""
Salary Negotiation Coach Agent.
Generates personalized salary negotiation strategies and scripts.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from utils.llm_client import get_gemini_client
from utils.llm_parsing import parse_json_from_llm_response
from utils.llm_prompting import build_llm_system_prompt
from utils.logging_config import get_structured_logger

# =============================================================================
# CONSTANTS AND CONFIGURATION
# =============================================================================

logger = logging.getLogger(__name__)
structured_logger = get_structured_logger(__name__)

# LLM Configuration
LLM_TEMPERATURE = 0.6
LLM_MAX_TOKENS = 3000

# =============================================================================
# PROMPT TEMPLATES
# =============================================================================

SYSTEM_CONTEXT = build_llm_system_prompt(
    "Compensation negotiation coach",
    "Create a grounded negotiation strategy from the supplied offer and candidate constraints.",
    extra_rules=(
        "Do not invent market rates, targets, leverage, or walk-away numbers when explicit market data is absent.",
        "Preserve the offer's supplied currency and never assume US dollars.",
        "Treat total career years as domain-specific experience only when work history supports that claim.",
        "Write scripts as complete natural prose without placeholders.",
    ),
)

NEGOTIATION_PROMPT = """Generate a comprehensive salary negotiation strategy and scripts for this specific situation.

Job & Offer Details:
- Position: {job_title}
- Company: {company_name}
- Company Size: {company_size}
- Industry: {industry}
- Location: {location}
- Currency: {currency}
- Offered Base Salary: {offered_salary}
- Offered Benefits: {offered_benefits}

Candidate Profile:
- Years of Experience: {years_experience}
- Current/Previous Salary: {current_salary}
- Key Achievements: {achievements}
- Unique Value Propositions: {unique_value}
- Other Offers/Leverage: {other_offers}
- Urgency Level: {urgency}

Market Context:
- Target Salary Range: {target_range}
- Market Rate Information: {market_info}

Negotiation Parameters:
- Priority Areas: {priority_areas}
- Flexibility Areas: {flexibility_areas}
- Non-Negotiables: {non_negotiables}
- Negotiation Style Preference: {style_preference}

CRITICAL WRITING RULES — every rule is mandatory:
1. All script fields (opening, value_statement, counter_offer, closing, response_script) \
must be word-for-word ready to say. No placeholder text, no brackets like [X], \
no "mention your achievement here" style instructions.
2. Reference the actual company name ({company_name}) and role ({job_title}) in the scripts \
where natural — this makes them feel personal, not generic.
3. The recommended_target in market_analysis may be numeric only when the supplied target \
range or market evidence supports it. Otherwise write "Insufficient market data".
4. The walk_away_point may include a number only when the candidate supplied that \
non-negotiable; otherwise state that the candidate must choose it.
5. Pushback scenarios must cover the 2-3 most likely objections for this specific \
company type and role, with a concrete response script for each.
6. Preserve the supplied currency. Do not assign a monetary value to alternative asks unless \
the input supports it; use "Not specified" when it does not.
7. MINIMUM ARRAY LENGTHS (non-negotiable): pushback_responses ≥ 3 items, \
alternative_asks ≥ 3 items, dos ≥ 4 items, donts ≥ 4 items.

Return your response as JSON with this exact structure:
{{
    "market_analysis": {{
        "salary_assessment": "Specific assessment of the offered salary vs market for this role/location",
        "market_position": "Where this offer falls — below/at/above market with context",
        "recommended_target": "Supported counter amount/range in the supplied currency, or 'Insufficient market data'",
        "negotiation_room": "Estimated negotiation room with reasoning",
        "leverage_assessment": "Honest assessment of candidate's negotiating leverage"
    }},
    "strategy_overview": {{
        "approach": "Specific, named negotiation approach (2-3 sentences) tailored to this company",
        "key_messages": ["Concrete message 1", "Concrete message 2", "Concrete message 3"],
        "timing_recommendation": "Specific advice on when and how to initiate the conversation",
        "confidence_level": "HIGH / MEDIUM / LOW — with a one-sentence reason specific to this situation"
    }},
    "main_script": {{
        "opening": "Word-for-word opening line(s) — ready to say out loud, references the company/role",
        "value_statement": "Word-for-word value pitch — specific, no brackets, references their business impact",
        "counter_offer": "Word-for-word counter offer delivery — states specific number confidently",
        "closing": "Word-for-word closing — warm, collaborative, leaves the door open"
    }},
    "pushback_responses": [
        {{
            "scenario": "Specific pushback scenario title (e.g., 'Budget is frozen')",
            "response_script": "Word-for-word response — complete sentences, ready to say, no placeholders",
            "key_points": ["What this response accomplishes"]
        }}
    ],
    "alternative_asks": [
        {{
            "item": "Specific benefit to negotiate (e.g., Signing Bonus)",
            "value": "Supported value in the supplied currency, or 'Not specified'",
            "script": "Word-for-word ask for this alternative",
            "likelihood": "high/medium/low"
        }}
    ],
    "email_template": {{
        "subject": "Specific email subject line",
        "body": "Complete email body for written negotiation — no placeholders"
    }},
    "dos_and_donts": {{
        "dos": ["Specific, actionable do — tied to this situation"],
        "donts": ["Specific, consequential don't — tied to this situation"]
    }},
    "red_flags": ["Specific red flag that suggests walking away from this offer"],
    "walk_away_point": "Specific conditions: state the minimum salary, minimum equity %, and/or timeline that make this offer unacceptable — be quantitative",
    "final_tips": ["Concrete, situation-specific tip for closing the negotiation successfully"]
}}"""


# =============================================================================
# AGENT CLASS
# =============================================================================


class SalaryCoachAgent:
    """
    Agent for generating salary negotiation strategies and scripts.

    Provides personalized negotiation guidance based on job offer,
    candidate profile, and market conditions.
    """

    def __init__(self):
        """Initialize the SalaryCoachAgent."""
        self.gemini_client = None
        self._current_user_api_key: str | None = None

    async def generate_strategy(
        self,
        job_title: str,
        company_name: str,
        offered_salary: str,
        years_experience: int | None = None,
        additional_context: str | None = None,
        location: str | None = None,
        currency: str | None = None,
        company_size: str | None = None,
        industry: str | None = None,
        offered_benefits: str | None = None,
        current_salary: str | None = None,
        achievements: list[str] | None = None,
        unique_value: list[str] | None = None,
        other_offers: str | None = None,
        urgency: str | None = None,
        target_range: str | None = None,
        market_info: str | None = None,
        priority_areas: list[str] | None = None,
        flexibility_areas: list[str] | None = None,
        non_negotiables: list[str] | None = None,
        style_preference: str | None = None,
        user_api_key: str | None = None,
        llm_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate a comprehensive salary negotiation strategy.

        Args:
            job_title: Position title
            company_name: Name of the company
            offered_salary: The salary offered
            years_experience: Years of relevant experience (optional, from profile)
            additional_context: Free-form additional info (target salary, achievements, etc.)
            location: Job location (for market context)
            currency: Profile salary currency (ISO 4217 code)
            company_size: Size of company (startup, mid-size, enterprise)
            industry: Industry sector
            offered_benefits: Description of offered benefits
            current_salary: Current or previous salary
            achievements: List of key achievements
            unique_value: List of unique value propositions
            other_offers: Description of other offers/leverage
            urgency: How urgently they need an answer
            target_range: Desired salary range
            market_info: Any market rate information
            priority_areas: What matters most to negotiate
            flexibility_areas: Where candidate can be flexible
            non_negotiables: Deal breakers
            style_preference: Preferred negotiation style
            user_api_key: Optional user API key for BYOK mode

        Returns:
            Dict containing negotiation strategy and scripts
        """
        self._current_user_api_key = user_api_key

        try:
            # Initialize Gemini client
            self.gemini_client = await get_gemini_client()

            # Format inputs with defaults
            location_str = location or "Not specified"
            currency_str = currency or "Not specified"
            company_size_str = company_size or "Not specified"
            industry_str = industry or "Not specified"
            offered_benefits_str = offered_benefits or "Standard benefits"
            current_salary_str = current_salary or "Not disclosed"
            achievements_str = (
                ", ".join(achievements) if achievements else "Not specified"
            )
            unique_value_str = (
                ", ".join(unique_value) if unique_value else "Not specified"
            )
            other_offers_str = other_offers or "None disclosed"
            urgency_str = urgency or "Normal timeline"
            target_range_str = (
                target_range or "Not specified; do not calculate a target"
            )
            market_info_str = (
                market_info or "No market data supplied; do not estimate market rates"
            )
            priority_areas_str = (
                ", ".join(priority_areas) if priority_areas else "Base salary"
            )
            flexibility_areas_str = (
                ", ".join(flexibility_areas)
                if flexibility_areas
                else "Open to discussion"
            )
            non_negotiables_str = (
                ", ".join(non_negotiables) if non_negotiables else "None specified"
            )
            style_str = style_preference or "Professional and assertive"
            years_exp_str = (
                str(years_experience)
                if years_experience is not None
                else "Not specified"
            )

            # Add additional context if provided
            additional_info = ""
            if additional_context:
                additional_info = (
                    f"\n\nAdditional Context from Candidate:\n{additional_context}"
                )

            # Build prompt
            prompt = (
                NEGOTIATION_PROMPT.format(
                    job_title=job_title,
                    company_name=company_name,
                    company_size=company_size_str,
                    industry=industry_str,
                    location=location_str,
                    currency=currency_str,
                    offered_salary=offered_salary,
                    offered_benefits=offered_benefits_str,
                    years_experience=years_exp_str,
                    current_salary=current_salary_str,
                    achievements=achievements_str,
                    unique_value=unique_value_str,
                    other_offers=other_offers_str,
                    urgency=urgency_str,
                    target_range=target_range_str,
                    market_info=market_info_str,
                    priority_areas=priority_areas_str,
                    flexibility_areas=flexibility_areas_str,
                    non_negotiables=non_negotiables_str,
                    style_preference=style_str,
                )
                + additional_info
            )

            structured_logger.log_agent_start("salary_coach", None)
            start_time = datetime.now(UTC)

            # Generate response
            response = await self.gemini_client.generate(
                prompt=prompt,
                system=SYSTEM_CONTEXT,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                user_api_key=self._current_user_api_key,
                structured_output=True,
                **(llm_options or {}),
            )

            duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000

            # Check for filtered content
            if response.get("filtered"):
                structured_logger.log_agent_complete("salary_coach", None, duration_ms)
                return self._create_filtered_result(response.get("response", ""))

            response_text = response.get("response", "")

            # Parse JSON response
            parsed = parse_json_from_llm_response(response_text)

            if not parsed:
                logger.error(
                    "Failed to parse salary coach response (%d characters)",
                    len(response_text),
                )
                structured_logger.log_agent_error(
                    "salary_coach", None, Exception("JSON parse failed"), duration_ms
                )
                return self._create_parse_error_result(response_text, job_title)

            structured_logger.log_agent_complete("salary_coach", None, duration_ms)

            return {
                "market_analysis": parsed.get("market_analysis", {}),
                "strategy_overview": parsed.get("strategy_overview", {}),
                "main_script": parsed.get("main_script", {}),
                "pushback_responses": parsed.get("pushback_responses", []),
                "alternative_asks": parsed.get("alternative_asks", []),
                "email_template": parsed.get("email_template", {}),
                "dos_and_donts": parsed.get("dos_and_donts", {"dos": [], "donts": []}),
                "red_flags": parsed.get("red_flags", []),
                "walk_away_point": parsed.get("walk_away_point", ""),
                "final_tips": parsed.get("final_tips", []),
                "job_title": job_title,
                "company_name": company_name,
                "offered_salary": offered_salary,
                "generated_at": datetime.now(UTC).isoformat(),
                "version": "1.0",
            }

        except Exception as e:
            logger.error(
                f"Salary negotiation strategy generation failed: {e}", exc_info=True
            )
            raise

    def _create_filtered_result(self, filter_message: str) -> dict[str, Any]:
        """Create a result when content was filtered."""
        return {
            "market_analysis": {},
            "strategy_overview": {
                "approach": "Content generation was filtered",
                "key_messages": [],
                "timing_recommendation": "Please try again",
                "confidence_level": "low",
            },
            "main_script": {},
            "pushback_responses": [],
            "alternative_asks": [],
            "email_template": {},
            "dos_and_donts": {"dos": [], "donts": []},
            "red_flags": [],
            "walk_away_point": "",
            "final_tips": ["Please try again with different input"],
            "filtered": True,
            "filter_message": filter_message,
            "generated_at": datetime.now(UTC).isoformat(),
            "version": "1.0",
        }

    def _create_parse_error_result(
        self, raw_response: str, job_title: str
    ) -> dict[str, Any]:
        """Create a result when JSON parsing failed."""
        return {
            "market_analysis": {},
            "strategy_overview": {
                "approach": "The generated response could not be validated. Please try again.",
                "key_messages": [],
                "timing_recommendation": "Regenerate before using this strategy",
                "confidence_level": "low",
            },
            "main_script": {},
            "pushback_responses": [],
            "alternative_asks": [],
            "email_template": {},
            "dos_and_donts": {"dos": [], "donts": []},
            "red_flags": [],
            "walk_away_point": "",
            "final_tips": ["Regenerate the strategy before negotiating"],
            "job_title": job_title,
            "parse_error": True,
            "generated_at": datetime.now(UTC).isoformat(),
            "version": "1.0",
        }
