from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from scripts.run_workday_account_gate import (
    DEFAULT_WORKFLOW_LOG_DIR,
    build_parser,
    configure_workflow_logging,
    prompt_device_token,
    run_once,
)


def test_cli_exposes_no_plaintext_token_argument() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--device-token", "must-not-enter-process-list"])


def test_cli_defaults_to_visible_loopback_runtime() -> None:
    args = build_parser().parse_args([])

    assert args.api_url == "http://127.0.0.1:8000"
    assert args.vault_port == 27118
    assert args.application_id is None
    assert args.headless is False
    assert args.accept_account_terms is False
    assert args.local_model is None
    assert args.log_control_decisions is False
    assert Path(args.log_dir) == DEFAULT_WORKFLOW_LOG_DIR


def test_cli_accepts_explicit_account_terms_approval() -> None:
    args = build_parser().parse_args(["--accept-account-terms"])

    assert args.accept_account_terms is True


def test_cli_accepts_target_application_id() -> None:
    application_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    args = build_parser().parse_args(["--application-id", str(application_id)])

    assert args.application_id == application_id


def test_cli_accepts_configured_local_model_selection() -> None:
    args = build_parser().parse_args(["--local-model", "dengcao/Qwen3-14B:Q5_K_M"])

    assert args.local_model == "dengcao/Qwen3-14B:Q5_K_M"


def test_cli_accepts_safe_control_decision_logging() -> None:
    args = build_parser().parse_args(["--log-control-decisions"])

    assert args.log_control_decisions is True


def test_cli_configures_correlated_secret_redacted_json_log(
    monkeypatch, tmp_path: Path
) -> None:
    configured: dict[str, object] = {}

    monkeypatch.setattr(
        "scripts.run_workday_account_gate.generate_request_id",
        lambda: "run123",
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.setup_logging",
        lambda **kwargs: configured.update(kwargs),
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.set_request_context",
        lambda **kwargs: {"request_id": kwargs["request_id"]},
    )

    run_id, log_path, context = configure_workflow_logging(tmp_path)

    assert run_id == "run123"
    assert log_path.parent == tmp_path
    assert log_path.name.startswith("workday-account-gate-")
    assert log_path.name.endswith("-run123.log")
    assert context == {"request_id": "run123"}
    assert configured["log_format"] == "json"
    assert configured["redact_sensitive"] is True
    assert configured["enable_file_logging"] is True


def test_cli_reads_device_token_from_hidden_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.getpass.getpass",
        lambda prompt: "  apw_test_token  ",
    )

    assert prompt_device_token() == "apw_test_token"


def test_cli_rejects_empty_hidden_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.getpass.getpass", lambda prompt: ""
    )

    with pytest.raises(RuntimeError, match="worker-device token"):
        prompt_device_token()


def test_cli_reprompts_after_accidental_blank_hidden_input(monkeypatch) -> None:
    responses = iter(["", "  apw_test_token  "])
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.getpass.getpass",
        lambda prompt: next(responses),
    )

    assert prompt_device_token() == "apw_test_token"


@pytest.mark.asyncio
async def test_cli_wires_production_unit1_executor_instead_of_legacy_runtime(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Api:
        def __init__(self, **kwargs):
            captured["api_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

    class _Executor:
        def __init__(self, **kwargs):
            captured["executor_options"] = kwargs

    class _Runner:
        def __init__(self, **kwargs):
            captured["runner_options"] = kwargs

        async def run_once(self, *, application_id):
            captured["application_id"] = application_id
            return SimpleNamespace(
                status=SimpleNamespace(value="unit1_complete"), safe_reason=None
            )

    class _VaultClient:
        async def close(self):
            captured["vault_closed"] = True

    settings = SimpleNamespace(
        local_llm_model="model-a",
        local_llm_models=["model-a"],
        workday_transition_history_limit=10,
    )
    repository = object()
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.get_settings", lambda: settings
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.get_gemini_client",
        lambda: _async_value(object()),
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.LocalLLMPortalControlResolver",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "scripts.run_workday_account_gate._open_vault_repository",
        lambda **kwargs: _async_value((repository, _VaultClient())),
    )
    monkeypatch.setattr("scripts.run_workday_account_gate.WorkdayWorkerApi", _Api)
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.ProductionWorkdayUnit1Executor", _Executor
    )
    monkeypatch.setattr("scripts.run_workday_account_gate.LocalWorkdayRunner", _Runner)
    monkeypatch.setattr(
        "scripts.run_workday_account_gate.NativePortalAccountCoordinator",
        lambda repository: object(),
    )
    application_id = uuid.uuid4()
    args = build_parser().parse_args(
        ["--application-id", str(application_id), "--local-model", "model-a"]
    )

    assert await run_once(args, "hidden-token") == "unit1_complete"
    runner_options = captured["runner_options"]
    assert "unit1_orchestrator" in runner_options
    assert "browser_runtime_factory" not in runner_options
    assert captured["application_id"] == application_id
    assert captured["vault_closed"] is True


async def _async_value(value):
    return value
