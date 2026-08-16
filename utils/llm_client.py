"""
LLM Client utility for Gemini integration.
Provides async Gemini API client with singleton pattern for text generation and health monitoring.
Includes optional caching for identical prompts to reduce API costs.

Supports two backends:
1. Google AI Studio (google-genai SDK, api_key) - BYOK / free tier with rate limits
2. Vertex AI (google-genai SDK, vertexai=True) - Higher rate limits, pay-per-use
"""

import asyncio
import logging
from time import perf_counter
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import get_settings
from utils.llm_prompting import extend_system_prompt
from utils.logging_config import get_structured_logger

# Both backends use google-genai (imported lazily inside methods)

# =============================================================================
# CONSTANTS AND CONFIGURATION
# =============================================================================

# Default generation parameters
DEFAULT_TEMPERATURE: float = 0.7
DEFAULT_MAX_TOKENS: int = 16000

# Timeout and connection constants
DEFAULT_TIMEOUT: int = 180  # 3 minutes for larger prompts

# Generation parameters
DEFAULT_TOP_P: float = 0.95
DEFAULT_TOP_K: int = 40

# Retry configuration
MAX_RETRIES: int = 3
RETRY_MIN_WAIT: int = 2  # seconds
RETRY_MAX_WAIT: int = 10  # seconds

LOCAL_LLM_TRUNCATION_REASONS = {"length", "max_length", "max_tokens"}
GPT_OSS_REASONING_TIMEOUTS = {"low": 180, "medium": 300, "high": 600}
# High reasoning can spend more than 12k tokens before it emits the final JSON.
# Keep this below the typical local context window once the prompt is included.
GPT_OSS_REASONING_TOKEN_FLOORS = {"low": 4096, "medium": 8192, "high": 24576}

# Configure module loggers
logger = logging.getLogger(__name__)
structured_logger = get_structured_logger(__name__)

# =============================================================================
# CUSTOM EXCEPTIONS
# =============================================================================


class GeminiError(Exception):
    """
    Custom exception for Gemini-related errors.

    This exception is raised when API calls fail due to connection issues,
    HTTP errors, or other Gemini-specific problems.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP status code from the upstream API response, if available.
        original_error: The underlying exception that caused this error, if any.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.original_error = original_error

    def __repr__(self) -> str:
        parts = [f"GeminiError({self.message!r}"]
        if self.status_code is not None:
            parts.append(f", status_code={self.status_code}")
        if self.original_error is not None:
            parts.append(f", original_error={self.original_error!r}")
        return "".join(parts) + ")"


# =============================================================================
# USER-FACING ERROR STRINGS (workflow / dashboard)
# =============================================================================

_GEMINI_QUOTA_USER_MESSAGE: str = (
    "The AI quota or rate limit for the configured API key was reached. "
    "Try again in a little while, review your plan and quotas for that key, "
    "or update your key under Settings → AI Setup."
)


def _text_indicates_gemini_quota_exhausted(text: str) -> bool:
    """
    Return True if exception text looks like a Gemini / Google AI quota or
    rate-limit response (429 RESOURCE_EXHAUSTED, free-tier caps, etc.).
    """
    if not text:
        return False
    upper = text.upper()
    if "RESOURCE_EXHAUSTED" in upper:
        return True
    lower = text.lower()
    if "exceeded your current quota" in lower or "quota exceeded" in lower:
        return True
    if "free_tier" in lower and "quota" in lower:
        return True
    if "429" in text and (
        "quota" in lower
        or "rate" in lower
        or "resource_exhausted" in lower
        or "generativelanguage" in lower
    ):
        return True
    return False


def _text_indicates_gemini_service_unavailable(text: str) -> bool:
    """Return True for Gemini service outages or high-demand 503-style failures."""
    if not text:
        return False
    upper = text.upper()
    if "503" in upper:
        return True
    if "UNAVAILABLE" in upper:
        return True
    if "SERVICE_UNAVAILABLE" in upper:
        return True
    if "HIGH_DEMAND" in upper or "HIGH DEMAND" in upper:
        return True
    return False


def _exception_chain_text(exc: BaseException) -> str:
    """Join str() of exc, GeminiError.original_error, and __cause__/__context__."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and id(cur) not in seen and depth < 8:
        seen.add(id(cur))
        parts.append(str(cur))
        if isinstance(cur, GeminiError) and cur.original_error is not None:
            inner = cur.original_error
            if id(inner) not in seen:
                parts.append(str(inner))
        nxt = cur.__cause__ if cur.__cause__ is not None else cur.__context__
        cur = nxt
        depth += 1
    return " ".join(parts)


def user_facing_message_from_llm_exception(exc: BaseException) -> str:
    """
    Map an LLM/SDK exception to text safe to store in workflow error_messages
    and show on the dashboard. Quota / rate-limit errors become a short,
    actionable message; everything else returns str(exc).
    """
    combined = _exception_chain_text(exc)
    if _text_indicates_gemini_quota_exhausted(combined):
        return _GEMINI_QUOTA_USER_MESSAGE
    return str(exc)


# =============================================================================
# SINGLETON CLIENT CLASS
# =============================================================================

# Global client instance for singleton pattern
_gemini_client = None


class GeminiClient:
    """
    Async client for interacting with Google Gemini API with singleton pattern.

    This class provides access to the Gemini API with proper error handling,
    connection management, and async support. It implements a singleton pattern
    to ensure efficient resource usage across the application.

    Supports two backends:
    1. Vertex AI (USE_VERTEX_AI=true) - Higher rate limits, requires ADC auth
    2. Google AI Studio (default) - Free tier with rate limits, uses API key

    Supports per-user API keys (BYOK) when user_api_key is provided to generate().

    Attributes:
        api_key (str): Default API key for Gemini API (from environment)
        default_model (str): Default model to use for requests (gemini-flash-2.5)
        timeout (int): Request timeout in seconds
        use_vertex_ai (bool): Whether to use Vertex AI backend
    """

    def __init__(self):
        """
        Initialize Gemini client with settings from configuration.
        This constructor should not be called directly - use get_gemini_client() instead.

        Backend selection:
        - USE_VERTEX_AI=true + VERTEX_AI_PROJECT → Vertex AI (ADC auth, no rate limits)
        - Otherwise → Google AI Studio (API key auth, has rate limits)
        """
        settings = get_settings()
        self.timeout = DEFAULT_TIMEOUT

        # Vertex AI settings
        self.use_vertex_ai = getattr(settings, "use_vertex_ai", False)
        self.vertex_project = getattr(settings, "vertex_ai_project", None)
        self.vertex_location = getattr(settings, "vertex_ai_location", "us-central1")

        # Google AI Studio API key
        self.api_key = getattr(settings, "gemini_api_key", None)

        # Local LLM fallback settings
        self.local_llm_url = getattr(settings, "local_llm_url", None)
        self.local_llm_model = getattr(settings, "local_llm_model", None)
        self.local_llm_models = set(getattr(settings, "local_llm_models", {}))
        self.local_llm_timeout = getattr(settings, "local_llm_timeout", DEFAULT_TIMEOUT)

        # Validate Vertex AI config
        if self.use_vertex_ai and not self.vertex_project:
            logger.warning(
                "USE_VERTEX_AI=true but VERTEX_AI_PROJECT not set. Falling back to Google AI Studio."
            )
            self.use_vertex_ai = False

        # Log the backend
        if self.use_vertex_ai:
            logger.info(
                f"[LLM] Ready  model={settings.gemini_model}  backend=Vertex AI"
                f"  project={self.vertex_project}  location={self.vertex_location}"
            )
        else:
            logger.info(
                f"[LLM] Ready  model={settings.gemini_model}  backend=Google AI Studio"
                f"  BYOK={'enabled (server key set)' if self.api_key else 'user-key only'}"
            )

        # google-genai uses Client(api_key=...) per-call — no global configure needed.

    def _is_local_model(self, model: str | None) -> bool:
        """Return True when the requested model should be routed to a local LLM endpoint."""
        if not model:
            return False
        return model == self.local_llm_model or model in self.local_llm_models

    def _should_fallback_to_local(self, exc: GeminiError) -> bool:
        """Return True when a Gemini failure should be retried against local LLM."""
        if not self.local_llm_url:
            return False
        combined = _exception_chain_text(exc)
        return _text_indicates_gemini_service_unavailable(
            combined
        ) or _text_indicates_gemini_quota_exhausted(combined)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        use_cache: bool = False,
        user_api_key: str | None = None,
        user_id: str | None = None,
        structured_output: bool = False,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a response from the Gemini model with optional caching.

        Args:
            prompt: The prompt to generate a response for
            model: Name of the model to use (optional, uses default if not specified)
            system: System message to set context
            temperature: Sampling temperature (0.0-1.0, default: 0.7)
            max_tokens: Maximum tokens to generate (default: 16000)
            use_cache: Whether to use Redis cache for this request (default: False)
                      Enable for deterministic prompts that don't need fresh responses.
            user_api_key: Optional user-provided API key (BYOK mode).
                         If provided, uses this key instead of the default.
            user_id: User UUID string. When provided, scopes the cache key per user
                     to prevent cross-user hits on prompts that contain personal content
                     (resumes, cover letters, etc.). Omit only for fully public prompts.
        structured_output: Request exactly one JSON object from the selected backend.
        force_local: Route to the configured local endpoint, using ``model`` if supplied.
        local_reasoning_effort: gpt-oss reasoning level (low, medium, or high).

        Returns:
            Dict[str, Any]: Response from the model containing generated text

        Raises:
            GeminiError: If the generation fails or returns an error
        """
        system = extend_system_prompt(system, structured=structured_output)

        # Check cache if enabled
        if use_cache:
            try:
                from utils.cache import get_cached_llm_response

                cached_response = await get_cached_llm_response(prompt, system, user_id)
                if cached_response:
                    logger.info("LLM response served from cache")
                    cached_response["from_cache"] = True
                    return cached_response
            except Exception as cache_error:
                logger.warning(
                    f"Cache lookup failed, proceeding with API call: {cache_error}"
                )

        # Call the internal method with retry logic
        result = await self._generate_with_retry(
            prompt=prompt,
            model=model,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            user_api_key=user_api_key,
            structured_output=structured_output,
            force_local=force_local,
            local_reasoning_effort=local_reasoning_effort,
        )

        # Cache the response if caching is enabled
        if use_cache and result:
            try:
                from utils.cache import cache_llm_response

                await cache_llm_response(prompt, result, system, user_id)
            except Exception as cache_error:
                logger.warning(f"Failed to cache LLM response: {cache_error}")

        return result

    @retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
        retry=retry_if_exception_type((GeminiError, ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _generate_with_retry(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        user_api_key: str | None = None,
        structured_output: bool = False,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """
        Internal generate method with retry logic.

        Uses tenacity for automatic retries with exponential backoff
        on transient failures (connection errors, timeouts).

        Args:
            prompt: The prompt to generate a response for
            model: Name of the model to use
            system: System message to set context
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            user_api_key: Optional user-provided API key (BYOK mode)
            structured_output: Whether the backend should constrain output to JSON.

        Returns:
            Dict[str, Any]: Response from the model

        Raises:
            GeminiError: If all retries fail
        """
        # Route local model requests to the local endpoint before checking Gemini/Vertex.
        if force_local or self._is_local_model(model):
            return await self._generate_with_local_llm(
                prompt=prompt,
                model=model,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                structured_output=structured_output,
                force_local=force_local,
                local_reasoning_effort=local_reasoning_effort,
            )

        # Route to the configured backend
        if self.use_vertex_ai:
            return await self._generate_with_vertex_ai(
                prompt=prompt,
                model=model,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                structured_output=structured_output,
            )

        effective_api_key = user_api_key or self.api_key
        if effective_api_key:
            try:
                return await self._generate_with_google_ai(
                    prompt=prompt,
                    model=model,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    user_api_key=user_api_key,
                    structured_output=structured_output,
                )
            except GeminiError as exc:
                if self._should_fallback_to_local(exc):
                    logger.warning(
                        "[LLM] Gemini service unavailable; falling back to local LLM"
                    )
                    return await self._generate_with_local_llm(
                        prompt=prompt,
                        model=model,
                        system=system,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        structured_output=structured_output,
                    )
                raise

        if self.local_llm_url:
            return await self._generate_with_local_llm(
                prompt=prompt,
                model=model,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                structured_output=structured_output,
            )

        raise GeminiError(
            "No API key available. Please configure your Gemini API key in Settings or set LOCAL_LLM_URL for a local fallback."
        )

    async def _generate_with_local_llm(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        structured_output: bool = False,
        force_local: bool = False,
        local_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Generate text through an Ollama-compatible ``/api/generate`` endpoint."""
        if not self.local_llm_url:
            raise GeminiError(
                "No local LLM endpoint is configured. Set LOCAL_LLM_URL in env."
            )

        model_to_use = (
            model
            if force_local and model
            else (model if self._is_local_model(model) else self.local_llm_model)
        )
        if not model_to_use:
            raise GeminiError(
                "Local LLM model is not configured. Set LOCAL_LLM_MODEL in env or provide a supported local model name."
            )

        is_gpt_oss = model_to_use.startswith("gpt-oss:")
        is_qwen3 = model_to_use.startswith("qwen3:")
        request_url = self.local_llm_url
        if is_gpt_oss and request_url.rstrip("/").endswith("/api/generate"):
            request_url = f"{request_url.rsplit('/', 1)[0]}/chat"

        reasoning_effort = (
            local_reasoning_effort
            if is_gpt_oss and local_reasoning_effort in {"low", "medium", "high"}
            else ("medium" if is_gpt_oss else None)
        )
        effective_max_tokens = max(
            max_tokens,
            GPT_OSS_REASONING_TOKEN_FLOORS.get(reasoning_effort, 0),
        )
        request_body: dict[str, Any] = {
            "model": model_to_use,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": effective_max_tokens,
            },
        }
        if is_gpt_oss:
            # GPT-OSS produces usable content through Ollama's chat protocol.
            # This mirrors the validated notebook while preserving role separation.
            request_body["messages"] = [
                *([{"role": "system", "content": system}] if system else []),
                {"role": "user", "content": prompt},
            ]
        else:
            request_body["prompt"] = prompt
            if system:
                request_body["system"] = system
        if structured_output:
            request_body["format"] = "json"
        # Qwen3 separates its reasoning from generated text. Structured agents
        # need the final JSON directly, so disable thinking for that model.
        if is_qwen3:
            request_body["think"] = False
        # GPT-OSS can spend its entire generation on reasoning and return an
        # empty ``response``. Resume extraction only needs factual JSON, so
        # retain a small reasoning budget, matching the working Ollama notebook.
        if is_gpt_oss:
            request_body["think"] = reasoning_effort
        effective_timeout = max(
            float(self.local_llm_timeout),
            float(GPT_OSS_REASONING_TIMEOUTS.get(reasoning_effort, 0)),
        )

        safe_model = model_to_use.replace("\r", " ").replace("\n", " ")[:128]
        logger.info(
            "[LLM] Local request model=%s prompt_chars=%d system_chars=%d "
            "max_tokens=%d structured=%s reasoning=%s timeout_seconds=%d",
            safe_model,
            len(prompt),
            len(system or ""),
            effective_max_tokens,
            structured_output,
            reasoning_effort or "n/a",
            effective_timeout,
        )
        api_start_time = perf_counter()

        try:
            async with httpx.AsyncClient(timeout=effective_timeout) as client:
                response = await client.post(
                    request_url,
                    json=request_body,
                )

            if response.status_code != 200:
                raise GeminiError(
                    f"Local LLM request failed with HTTP {response.status_code}.",
                    status_code=response.status_code,
                )

            data = response.json()
            if not isinstance(data, dict):
                raise GeminiError("Local LLM response must be a JSON object.")

            # Do not use ``or`` here: Ollama legitimately returns an empty
            # ``response`` when a reasoning model exhausts its output budget.
            # Preserving that value lets the truncation check below report the
            # real cause instead of claiming the response field is absent.
            text = next(
                (
                    candidate
                    for candidate in (
                        (
                            data.get("message", {}).get("content")
                            if isinstance(data.get("message"), dict)
                            else None
                        ),
                        data.get("text"),
                        data.get("response"),
                        data.get("result"),
                    )
                    if isinstance(candidate, str)
                ),
                None,
            )
            if not isinstance(text, str):
                raise GeminiError("Local LLM response is missing generated text.")

            done = data.get("done", True)
            if not isinstance(done, bool):
                raise GeminiError("Local LLM response has an invalid done field.")

            raw_done_reason = data.get("done_reason")
            done_reason = (
                str(raw_done_reason).strip() if raw_done_reason is not None else None
            )
            if not done:
                raise GeminiError("Local LLM response was incomplete (done=false).")
            if done_reason and done_reason.casefold() in LOCAL_LLM_TRUNCATION_REASONS:
                raise GeminiError(
                    "Local LLM response was truncated at the output token limit."
                )
            if not text.strip():
                raise GeminiError("Local LLM returned an empty response.")
            if structured_output and text.strip() in {"{}", "[]"}:
                raise GeminiError("Local LLM returned an empty structured response.")

            api_duration_ms = (perf_counter() - api_start_time) * 1000
            logger.info(
                "[LLM] Local done model=%s duration_ms=%.0f response_chars=%d "
                "done_reason=%s prompt_tokens=%s output_tokens=%s",
                safe_model,
                api_duration_ms,
                len(text),
                done_reason or "unspecified",
                data.get("prompt_eval_count", "unknown"),
                data.get("eval_count", "unknown"),
            )
            structured_logger.log_external_api_call(
                service="local_llm",
                operation="generate",
                duration_ms=api_duration_ms,
                success=True,
            )

            result: dict[str, Any] = {
                "model": model_to_use,
                "response": text,
                "done": done,
            }
            if done_reason is not None:
                result["done_reason"] = done_reason
            return result
        except GeminiError as local_error:
            api_duration_ms = (perf_counter() - api_start_time) * 1000
            structured_logger.log_external_api_call(
                service="local_llm",
                operation="generate",
                duration_ms=api_duration_ms,
                success=False,
                error=local_error.message,
            )
            logger.warning(
                "[LLM] Local request failed model=%s duration_ms=%.0f status=%s",
                safe_model,
                api_duration_ms,
                local_error.status_code or "unavailable",
            )
            raise
        except httpx.HTTPError as http_err:
            api_duration_ms = (perf_counter() - api_start_time) * 1000
            structured_logger.log_external_api_call(
                service="local_llm",
                operation="generate",
                duration_ms=api_duration_ms,
                success=False,
                error=type(http_err).__name__,
            )
            logger.warning(
                "[LLM] Local transport failed model=%s duration_ms=%.0f error=%s",
                safe_model,
                api_duration_ms,
                type(http_err).__name__,
            )
            raise GeminiError(
                "Local LLM request failed due to a transport error.",
                original_error=http_err,
            ) from http_err
        except ValueError as parse_err:
            api_duration_ms = (perf_counter() - api_start_time) * 1000
            structured_logger.log_external_api_call(
                service="local_llm",
                operation="generate",
                duration_ms=api_duration_ms,
                success=False,
                error="invalid_json",
            )
            logger.warning(
                "[LLM] Local response was not JSON model=%s duration_ms=%.0f",
                safe_model,
                api_duration_ms,
            )
            raise GeminiError(
                "Invalid JSON from local LLM.",
                original_error=parse_err,
            ) from parse_err

    async def _generate_with_vertex_ai(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        structured_output: bool = False,
    ) -> dict[str, Any]:
        """Generate using Vertex AI backend with ADC authentication (higher rate limits)."""
        try:
            from google import genai as google_genai
            from google.genai import types

            current_settings = get_settings()
            model_to_use = model or current_settings.gemini_model

            # Create client with Vertex AI using ADC (Application Default Credentials)
            # Requires: gcloud auth application-default login
            client = google_genai.Client(
                vertexai=True,
                project=self.vertex_project,
                location=self.vertex_location,
            )

            # Combine system and user prompts
            if system:
                combined_prompt = f"{system}\n\n{prompt}"
            else:
                combined_prompt = prompt

            # Create generation config
            # Disable thinking mode — on flash models it consumes the output token
            # budget for internal reasoning, leaving too little for actual output
            config_options: dict[str, Any] = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "top_p": DEFAULT_TOP_P,
                "top_k": DEFAULT_TOP_K,
                "thinking_config": types.ThinkingConfig(thinking_budget=0),
            }
            if structured_output:
                config_options["response_mime_type"] = "application/json"
            config = types.GenerateContentConfig(**config_options)

            prompt_chars = len(combined_prompt)
            logger.info(
                f"[LLM] Vertex AI  model={model_to_use}"
                f"  prompt={prompt_chars:,} chars"
                f"  temp={temperature}"
            )

            # Generate response (bounded by DEFAULT_TIMEOUT to prevent indefinite hangs)
            api_start_time = perf_counter()
            response = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=model_to_use,
                        contents=combined_prompt,
                        config=config,
                    ),
                ),
                timeout=self.timeout,
            )
            api_duration_ms = (perf_counter() - api_start_time) * 1000

            # Extract response text
            try:
                response_text = response.text
            except Exception as text_error:
                logger.error(
                    f"[LLM] Failed to extract response text: {text_error}",
                    exc_info=True,
                )
                response_text = "Error retrieving response. Please try again."

            logger.info(
                f"[LLM] Done  {api_duration_ms:.0f}ms"
                f"  response={len(response_text):,} chars"
            )

            # Log API call performance
            structured_logger.log_external_api_call(
                service="vertex_ai",
                operation="generate_content",
                duration_ms=api_duration_ms,
                success=True,
            )

            return {"model": model_to_use, "response": response_text, "done": True}

        except Exception as e:
            structured_logger.log_external_api_call(
                service="vertex_ai",
                operation="generate_content",
                duration_ms=0,
                success=False,
                error=str(e),
            )
            logger.error(f"Error in Vertex AI generate: {e}", exc_info=True)
            raise GeminiError(
                f"Vertex AI generate failed: {str(e)}", original_error=e
            ) from e

    async def _generate_with_google_ai(
        self,
        prompt: str,
        model: str | None = None,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        user_api_key: str | None = None,
        structured_output: bool = False,
    ) -> dict[str, Any]:
        """Generate using Google AI Studio backend (BYOK / free tier)."""
        try:
            from google import genai as google_genai
            from google.genai import types

            current_settings = get_settings()

            effective_api_key = user_api_key or self.api_key
            if not effective_api_key:
                raise GeminiError(
                    "No API key available. Please configure your Gemini API key in Settings."
                )

            client = google_genai.Client(api_key=effective_api_key)
            model_to_use = model or current_settings.gemini_model

            if system:
                combined_prompt = f"{system}\n\n{prompt}"
            else:
                combined_prompt = prompt

            config_options: dict[str, Any] = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "top_p": DEFAULT_TOP_P,
                "top_k": DEFAULT_TOP_K,
                "thinking_config": types.ThinkingConfig(thinking_budget=0),
            }
            if structured_output:
                config_options["response_mime_type"] = "application/json"
            config = types.GenerateContentConfig(**config_options)

            prompt_chars = len(combined_prompt)
            byok_label = "  byok=user-key" if user_api_key else ""
            logger.info(
                f"[LLM] Google AI Studio  model={model_to_use}"
                f"  prompt={prompt_chars:,} chars"
                f"  temp={temperature}"
                f"{byok_label}"
            )

            api_start_time = perf_counter()
            response = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=model_to_use,
                        contents=combined_prompt,
                        config=config,
                    ),
                ),
                timeout=self.timeout,
            )
            api_duration_ms = (perf_counter() - api_start_time) * 1000

            try:
                response_text: str = response.text
            except Exception as text_error:
                logger.error(
                    f"[LLM] Failed to extract response text: {text_error}",
                    exc_info=True,
                )
                response_text = (
                    "Error retrieving response from Gemini API. Please try again."
                )

            # Check for safety filter (finish_reason OTHER than STOP/MAX_TOKENS)
            filtered = False
            if hasattr(response, "candidates") and response.candidates:
                finish_reason = getattr(response.candidates[0], "finish_reason", None)
                if finish_reason and str(finish_reason) not in (
                    "FinishReason.STOP",
                    "FinishReason.MAX_TOKENS",
                    "1",
                    "2",
                ):
                    filtered = True

            logger.info(
                f"[LLM] Done  {api_duration_ms:.0f}ms"
                f"  response={len(response_text):,} chars"
            )

            structured_logger.log_external_api_call(
                service="gemini",
                operation="generate_content",
                duration_ms=api_duration_ms,
                success=True,
            )

            if filtered:
                logger.warning(
                    f"[LLM] Content filtered by safety settings  model={model_to_use}"
                )
                return {
                    "model": model_to_use,
                    "response": "The content generation was blocked by safety filters. Please try with different input or contact support.",
                    "done": True,
                    "filtered": True,
                }

            return {"model": model_to_use, "response": response_text, "done": True}

        except Exception as e:
            structured_logger.log_external_api_call(
                service="gemini",
                operation="generate_content",
                duration_ms=0,
                success=False,
                error=str(e),
            )
            logger.error(f"Error in Gemini generate: {e}", exc_info=True)
            raise GeminiError(f"Generate failed: {str(e)}", original_error=e) from e

    def _local_health_url(self) -> str:
        """Return the Ollama tags endpoint associated with the generation URL."""
        if not self.local_llm_url:
            raise GeminiError("No local LLM endpoint is configured.")

        url = httpx.URL(self.local_llm_url)
        path = url.path.rstrip("/")
        if path.endswith("/api/generate") or path.endswith("/api/chat"):
            path = f"{path.rsplit('/', 1)[0]}/tags"
        elif path.endswith("/api"):
            path = f"{path}/tags"
        elif not path.endswith("/api/tags"):
            path = f"{path}/api/tags"
        return str(url.copy_with(path=path, query=None, fragment=None))

    async def _check_local_llm_health(self) -> bool:
        """Check the configured local Ollama service without running generation."""
        try:
            timeout = min(float(self.local_llm_timeout), 10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self._local_health_url())
            healthy = 200 <= response.status_code < 300
            if not healthy:
                logger.warning(
                    "Local LLM health check failed with HTTP %s",
                    response.status_code,
                )
            return healthy
        except (GeminiError, httpx.HTTPError, ValueError) as health_error:
            logger.warning(
                "Local LLM health check failed: %s",
                type(health_error).__name__,
            )
            return False

    async def health_check(self) -> bool:
        """
        Check if Gemini service is healthy by making a simple API call.

        Returns:
            bool: True if service is healthy and accessible, False otherwise
        """
        try:
            if self.use_vertex_ai:
                # Use a quota-free model metadata fetch instead of generate_content
                # so health checks don't burn tokens or trigger rate limits.
                from google import genai as google_genai

                client = google_genai.Client(
                    vertexai=True,
                    project=self.vertex_project,
                    location=self.vertex_location,
                )
                settings = get_settings()
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: client.models.get(model=settings.gemini_model),
                    ),
                    timeout=10.0,
                )
            else:
                # Without a server Gemini key, verify the configured local fallback.
                if not self.api_key:
                    if self.local_llm_url:
                        return await self._check_local_llm_health()
                    # BYOK-only mode: no server key to verify, nothing to check.
                    return True
                from google import genai as google_genai

                _hc_client = google_genai.Client(api_key=self.api_key)
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: list(_hc_client.models.list())
                    ),
                    timeout=10.0,
                )
            return True
        except Exception as e:
            # 429 RESOURCE_EXHAUSTED means the service is reachable and responding
            # correctly — it is simply enforcing quota. Treat as healthy.
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                logger.info(
                    "Gemini health check: quota limit hit but service is reachable"
                )
                return True
            logger.warning(f"Gemini health check failed: {e}")
            return False


# =============================================================================
# GLOBAL FUNCTIONS
# =============================================================================


def reset_gemini_client() -> None:
    """
    Reset the global Gemini client instance.
    This forces a new client to be created with the latest settings.
    """
    global _gemini_client
    _gemini_client = None
    logger.info("Reset Gemini client")


async def get_gemini_client() -> GeminiClient:
    """
    Get or create the global Gemini client instance.

    This function implements the singleton pattern to ensure only one
    client instance is used across the application.

    Returns:
        GeminiClient: Shared Gemini client instance
    """
    global _gemini_client

    if _gemini_client is None:
        _gemini_client = GeminiClient()
        logger.info("Initialized Gemini client")

    return _gemini_client


async def check_gemini_health() -> bool:
    """
    Check if Gemini service is running and accessible.

    This function performs a health check by attempting to connect to
    the Gemini service and verify its accessibility. It's useful for
    startup checks and monitoring.

    Returns:
        bool: True if service is healthy and accessible, False otherwise
    """
    try:
        # Get client instance (this will create it if needed)
        client = await get_gemini_client()

        # Perform health check using the client
        response = await client.health_check()
        if not response:
            logger.warning("Gemini health check failed: Service not responsive")
            return False

        # Only log when a real server-key check was performed (not BYOK-only no-op).
        server_has_key = bool(client.api_key) or client.use_vertex_ai
        if server_has_key:
            logger.info("Gemini health check successful")
        return True
    except Exception as e:
        logger.error(f"Gemini health check failed: {e}", exc_info=True)
        return False


async def close_gemini_client() -> None:
    """
    Close Gemini client connection and clean up resources.

    This function should be called during application shutdown to ensure
    proper cleanup of resources.
    """
    global _gemini_client

    if _gemini_client:
        _gemini_client = None
        logger.info("Gemini client connection closed")
