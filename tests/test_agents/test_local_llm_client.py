"""Focused tests for the Ollama-compatible local LLM transport."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import utils.llm_client as llm_module
from utils.llm_client import GeminiClient, GeminiError


def _make_client(
    *,
    model: str = "custom-local:latest",
    api_key: str | None = None,
) -> GeminiClient:
    """Build a client without loading repository or process environment settings."""
    client = object.__new__(GeminiClient)
    client.timeout = 180
    client.use_vertex_ai = False
    client.vertex_project = None
    client.vertex_location = "us-central1"
    client.api_key = api_key
    client.local_llm_url = "http://local.test:11434/api/generate"
    client.local_llm_model = model
    client.local_llm_timeout = 30
    return client


def _install_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Route clients constructed by the production code through a mock transport."""
    original_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def build_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", build_client)


@pytest.mark.asyncio
async def test_structured_local_request_uses_ollama_payload_and_keeps_logs_private(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Send role-separated prompts and Ollama options without logging their content."""
    requests: list[httpx.Request] = []
    private_prompt = "candidate-secret-123"
    private_response = '{"private":"response-secret-456"}'

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "custom-local:latest",
                "response": private_response,
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 42,
                "eval_count": 12,
            },
        )

    _install_http_transport(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger=llm_module.__name__)
    client = _make_client(api_key="cloud-key")

    result = await client.generate(
        prompt=private_prompt,
        system="Original system instruction",
        model="custom-local:latest",
        temperature=0.2,
        max_tokens=321,
        structured_output=True,
    )

    assert result == {
        "model": "custom-local:latest",
        "response": private_response,
        "done": True,
        "done_reason": "stop",
    }
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["model"] == "custom-local:latest"
    assert payload["prompt"] == private_prompt
    assert payload["system"].startswith("Original system instruction")
    assert "Return exactly one JSON object" in payload["system"]
    assert payload["options"] == {"temperature": 0.2, "num_predict": 321}
    assert payload["format"] == "json"
    assert payload["stream"] is False
    assert "temperature" not in payload
    assert "max_tokens" not in payload

    captured = capsys.readouterr()
    observable_output = f"{captured.out}{captured.err}{caplog.text}"
    assert private_prompt not in observable_output
    assert private_response not in observable_output


@pytest.mark.asyncio
async def test_uses_configured_local_model_when_model_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route arbitrary configured model names without a hardcoded allowlist entry."""
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "ok", "done": True})

    _install_http_transport(monkeypatch, handler)
    client = _make_client(model="org/private-model:2026")

    result = await client.generate(prompt="hello")

    assert result["model"] == "org/private-model:2026"
    assert payloads[0]["model"] == "org/private-model:2026"
    assert "format" not in payloads[0]


@pytest.mark.asyncio
async def test_http_error_does_not_expose_response_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Raise a status-bearing error without copying a private response into logs."""
    private_body = "backend-secret-diagnostic"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=private_body)

    _install_http_transport(monkeypatch, handler)
    client = _make_client()

    with pytest.raises(GeminiError) as error_info:
        await client._generate_with_local_llm(prompt="hello")

    assert error_info.value.status_code == 503
    assert private_body not in str(error_info.value)
    captured = capsys.readouterr()
    assert private_body not in f"{captured.out}{captured.err}{caplog.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("done", "done_reason", "message"),
    [
        (False, None, "incomplete"),
        (True, "length", "truncated"),
        (True, "max_tokens", "truncated"),
    ],
)
async def test_incomplete_or_truncated_response_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    done: bool,
    done_reason: str | None,
    message: str,
) -> None:
    """Never report an incomplete local generation as successfully done."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": '{"partial":true}',
                "done": done,
                "done_reason": done_reason,
            },
        )

    _install_http_transport(monkeypatch, handler)
    client = _make_client()

    with pytest.raises(GeminiError, match=message):
        await client._generate_with_local_llm(prompt="hello")


@pytest.mark.asyncio
async def test_invalid_json_response_is_wrapped_without_raw_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convert invalid endpoint JSON into a stable transport exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="private-invalid-json")

    _install_http_transport(monkeypatch, handler)
    client = _make_client()

    with pytest.raises(GeminiError, match="Invalid JSON from local LLM") as error_info:
        await client._generate_with_local_llm(prompt="hello")

    assert "private-invalid-json" not in str(error_info.value)


@pytest.mark.asyncio
async def test_health_check_uses_ollama_tags_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the local service without spending tokens on generation."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"models": []})

    _install_http_transport(monkeypatch, handler)
    client = _make_client()

    assert await client.health_check() is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url == httpx.URL("http://local.test:11434/api/tags")


@pytest.mark.asyncio
async def test_structured_flag_reaches_google_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep JSON-mode routing consistent across hosted and local providers."""
    client = _make_client(api_key="cloud-key")
    received: dict[str, Any] = {}

    async def fake_google(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {"model": "gemini-test", "response": "{}", "done": True}

    monkeypatch.setattr(client, "_generate_with_google_ai", fake_google)

    result = await client.generate(
        prompt="hello",
        model="gemini-test",
        structured_output=True,
    )

    assert result["done"] is True
    assert received["structured_output"] is True
    assert "Return exactly one JSON object" in received["system"]
