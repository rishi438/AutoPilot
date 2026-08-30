#!/usr/bin/env python3
"""Run one local Workday account-gate lease; submission is not implemented."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from services.local_workday_runner import LocalWorkdayRunner
from services.portal_control_resolver import LocalLLMPortalControlResolver
from services.portal_account_automation import NativePortalAccountCoordinator
from services.portal_credentials import (
    PortalCredentialError,
    PortalCredentialRepository,
    PortalVaultCollections,
)
from services.workday_browser_runtime import (
    PersistentWorkdayBrowserRuntime,
    user_scoped_autopilot_browser_profile,
)
from services.workday_playwright_worker import WorkdayWorkerError
from services.workday_unit1_runtime import ProductionWorkdayUnit1Executor
from services.workday_worker_api import (
    LeasedWorkdayApplication,
    WorkdayWorkerApi,
    WorkdayWorkerTransportError,
)
from utils.llm_client import get_gemini_client
from utils.logging_config import (
    clear_request_context,
    generate_request_id,
    set_request_context,
    setup_logging,
)

logger = logging.getLogger(__name__)
DEFAULT_WORKFLOW_LOG_DIR = PROJECT_ROOT / "logs" / "workday-runs"
WORKER_DEVICE_TOKEN_ENV = "AUTOPILOT_WORKDAY_DEVICE_TOKEN"
WORKDAY_APPLICATION_ID_ENV = "AUTOPILOT_WORKDAY_APPLICATION_ID"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Lease one queued Workday job and complete only its account gate. "
            "Application form submission remains disabled."
        )
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000",
        help="AutoPilot API base URL (default: loopback port 8000).",
    )
    parser.add_argument(
        "--vault-port",
        type=int,
        default=27118,
        help="Loopback portal-vault port (default: 27118).",
    )
    parser.add_argument(
        "--application-id",
        type=uuid.UUID,
        default=os.environ.pop(WORKDAY_APPLICATION_ID_ENV, None),
        help="Lease only this owned, eligible Workday application UUID.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Hide Chromium; unsuitable when CAPTCHA or OTP review may be needed.",
    )
    parser.add_argument(
        "--accept-account-terms",
        action="store_true",
        help=(
            "Approve the recognized Workday account-creation terms checkbox for "
            "this run; unfamiliar consent wording remains blocked."
        ),
    )
    parser.add_argument(
        "--local-model",
        default=None,
        help=(
            "Configured local model for portal control discovery. Defaults to "
            "LOCAL_LLM_MODEL."
        ),
    )
    parser.add_argument(
        "--log-control-decisions",
        action="store_true",
        help=(
            "Print safe model/control decision metadata without prompts, page text, "
            "field values, or credentials."
        ),
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_WORKFLOW_LOG_DIR),
        help=(
            "Directory for per-run secret-redacted JSON logs "
            "(default: logs/workday-runs)."
        ),
    )
    return parser


def prompt_device_token(*, max_attempts: int = 3) -> str:
    injected_token = os.environ.pop(WORKER_DEVICE_TOKEN_ENV, "").strip()
    if injected_token:
        return injected_token
    for attempt in range(max_attempts):
        token = getpass.getpass("Workday worker-device token: ").strip()
        if token:
            return token
        if attempt + 1 < max_attempts:
            print(
                "Worker-device token was empty; type or paste it and press Enter. "
                "Input remains hidden.",
                file=sys.stderr,
            )
    raise RuntimeError("A worker-device token is required.")


def print_control_decision(event: str) -> None:
    """Log one credential-free local-agent decision record."""
    logger.info("workday_control_decision event=%s", event)


def configure_workflow_logging(
    log_dir: str | Path,
) -> tuple[str, Path, dict[str, Any]]:
    """Create one correlated, rotating, secret-redacted JSON log for this run."""
    resolved_log_dir = Path(log_dir).expanduser()
    if not resolved_log_dir.is_absolute():
        resolved_log_dir = PROJECT_ROOT / resolved_log_dir
    run_id = generate_request_id()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    app_name = f"workday-account-gate-{timestamp}-{run_id}"
    setup_logging(
        log_level="INFO",
        log_format="json",
        log_dir=str(resolved_log_dir),
        enable_file_logging=True,
        enable_console_logging=True,
        redact_sensitive=True,
        app_name=app_name,
        service_name="autopilot-workday-worker",
        service_version="local",
        environment="local",
    )
    context_tokens = set_request_context(request_id=run_id)
    return run_id, resolved_log_dir / f"{app_name}.log", context_tokens


async def _open_vault_repository(
    *, port: int
) -> tuple[PortalCredentialRepository, Any]:
    logger.info("portal_vault_connect_started port=%s", port)
    if not 1 <= port <= 65535:
        raise RuntimeError("The portal-vault port is invalid.")
    username = os.environ.get("PORTAL_VAULT_APP_USERNAME")
    password = os.environ.get("PORTAL_VAULT_APP_PASSWORD")
    database_name = os.environ.get("PORTAL_VAULT_MONGODB_DATABASE", "autopilot_vault")
    settings = get_settings()
    encryption_key = settings.portal_vault_encryption_key
    recovery_key = settings.portal_vault_password_recovery_key
    if not username or not password or encryption_key is None or recovery_key is None:
        raise RuntimeError("The local portal vault is not configured.")

    try:
        from pymongo import AsyncMongoClient
    except ImportError as exc:
        raise RuntimeError("PyMongo is required by the local Workday runner.") from exc

    client = AsyncMongoClient(
        host="127.0.0.1",
        port=port,
        username=username,
        password=password,
        authSource=database_name,
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )
    try:
        await client.admin.command("ping")
    except Exception as exc:
        await client.close()
        raise RuntimeError("The local portal vault is unavailable.") from exc
    logger.info("portal_vault_connect_completed")
    database = client[database_name]
    repository = PortalCredentialRepository(
        PortalVaultCollections(
            credentials=database["portal_credentials"],
            events=database["portal_credential_events"],
        ),
        encryption_key.get_secret_value(),
        recovery_key.get_secret_value(),
    )
    return repository, client


async def run_once(args: argparse.Namespace, device_token: str) -> str:
    settings = get_settings()
    local_model = (args.local_model or settings.local_llm_model or "").strip()
    approved_models = set(settings.local_llm_models)
    if settings.local_llm_model:
        approved_models.add(settings.local_llm_model)
    if not local_model or local_model not in approved_models:
        raise RuntimeError("The selected local portal-control model is not configured.")
    logger.info(
        "workday_runner_configuration model=%s headless=%s account_terms_approved=%s",
        local_model,
        args.headless,
        args.accept_account_terms,
    )
    decision_reporter = print_control_decision if args.log_control_decisions else None
    async with WorkdayWorkerApi(
        base_url=args.api_url,
        bearer_token=device_token,
    ) as api:
        lease_next_unit1 = getattr(api, "lease_next_unit1", None)
        preflight_lease = (
            await lease_next_unit1(application_id=args.application_id)
            if callable(lease_next_unit1)
            else None
        )
        if callable(lease_next_unit1) and preflight_lease is None:
            return "idle"

        repository = None
        vault_client = None
        handed_to_runner = False
        try:
            control_resolver = LocalLLMPortalControlResolver(
                await get_gemini_client(),
                model=local_model,
                decision_reporter=decision_reporter,
            )
            repository, vault_client = await _open_vault_repository(
                port=args.vault_port
            )

            def browser_runtime_factory(
                lease: LeasedWorkdayApplication,
            ) -> PersistentWorkdayBrowserRuntime:
                return PersistentWorkdayBrowserRuntime(
                    profile_dir=user_scoped_autopilot_browser_profile(
                        str(lease.user_id)
                    ),
                    repository_root=PROJECT_ROOT,
                    headless=args.headless,
                    accept_account_terms=args.accept_account_terms,
                    control_resolver=control_resolver,
                    decision_reporter=decision_reporter,
                )

            runner = LocalWorkdayRunner(
                api=api,
                coordinator=NativePortalAccountCoordinator(repository),
                initial_lease=preflight_lease,
                unit1_orchestrator=ProductionWorkdayUnit1Executor(
                    api=api,
                    credential_reader=repository,
                    runtime_factory=browser_runtime_factory,
                    resolver=control_resolver,
                    history_limit=settings.workday_transition_history_limit,
                    lock_cooldown_hours=getattr(
                        settings, "workday_account_lock_cooldown_hours", 6
                    ),
                ),
            )
            handed_to_runner = True
            result = await runner.run_once(application_id=args.application_id)
            if result.safe_reason:
                return f"{result.status.value} ({result.safe_reason})"
            return result.status.value
        except Exception:
            if preflight_lease is not None and not handed_to_runner:
                await api.release_startup_lease(preflight_lease)
            raise
        finally:
            if vault_client is not None:
                await vault_client.close()
                logger.info("portal_vault_connection_closed")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    run_id, log_path, context_tokens = configure_workflow_logging(args.log_dir)
    print(f"Workday workflow log: {log_path}", flush=True)
    selected_model = (
        args.local_model or get_settings().local_llm_model or "not configured"
    )
    logger.info(
        "workday_account_gate_started run_id=%s model=%s",
        run_id,
        selected_model,
    )
    try:
        status = asyncio.run(run_once(args, prompt_device_token()))
    except (
        PortalCredentialError,
        RuntimeError,
        WorkdayWorkerError,
        WorkdayWorkerTransportError,
    ) as exc:
        logger.exception(
            "workday_account_gate_stopped_safely error_type=%s error=%s",
            type(exc).__name__,
            exc,
        )
        return 1
    except Exception as exc:
        logger.exception(
            "workday_account_gate_failed_unexpectedly error_type=%s",
            type(exc).__name__,
        )
        return 1
    else:
        logger.info("workday_account_gate_completed status=%s", status)
        return 0
    finally:
        clear_request_context(context_tokens)


if __name__ == "__main__":
    raise SystemExit(main())
