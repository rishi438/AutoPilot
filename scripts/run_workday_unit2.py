"""Thin visible foreground entry point for Workday Unit 2.

Executes exactly one bounded run for one targeted application ID and prints safe fields only.
Does not execute live without separate explicit user authorization.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

# Ensure repository root is on sys.path before importing local packages
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from uuid import UUID

from pydantic import SecretStr

from services.workday_unit2_orchestrator import WorkdayUnit2Orchestrator
from services.workday_worker_api import WorkdayWorkerApi

logger = logging.getLogger("run_workday_unit2")

WORKER_DEVICE_TOKEN_ENV_VARS = (
    "AUTOPILOT_WORKDAY_DEVICE_TOKEN",
    "WORKER_BEARER_TOKEN",
    "AUTOPILOT_WORKER_TOKEN",
)


def resolve_worker_token() -> str:
    """Resolve bearer token securely without exposing secrets on the CLI.

    Checks environment variables first, then piped stdin, and finally an
    interactive masked prompt. Fails closed if empty.
    """
    for env_var in WORKER_DEVICE_TOKEN_ENV_VARS:
        token = os.environ.get(env_var, "").strip()
        if token:
            return token

    if not sys.stdin.isatty():
        token = sys.stdin.readline().strip()
        if token:
            return token

    try:
        token = getpass.getpass("Workday worker bearer token: ").strip()
        if token:
            return token
    except (EOFError, KeyboardInterrupt):
        pass

    raise RuntimeError(
        "A worker bearer token is required. Set AUTOPILOT_WORKDAY_DEVICE_TOKEN, "
        "pipe the token via stdin, or enter it at the masked prompt."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Workday Unit 2 in visible foreground for one application."
    )
    parser.add_argument(
        "--application-id",
        required=True,
        type=UUID,
        help="Target application UUID.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Worker API base URL.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    app_id: UUID = args.application_id
    token_val = resolve_worker_token()

    repo_root = _REPO_ROOT

    async with WorkdayWorkerApi(
        base_url=args.base_url,
        bearer_token=SecretStr(token_val),
    ) as worker_api:
        orchestrator = WorkdayUnit2Orchestrator(
            worker_api=worker_api,
            repository_root=repo_root,
            headless=False,  # Visible foreground browser
        )
        result = await orchestrator.run_once(application_id=app_id)

    # Print safe result fields only (zero secrets, credentials, tokens, or raw DOM)
    print(f"Status: {result.status}")
    print(f"Phase: {result.phase.value}")
    if result.hold_code:
        print(f"Hold code: {result.hold_code}")
    if result.safe_reason:
        print(f"Safe reason: {result.safe_reason}")
    if result.attempt_id:
        print(f"Attempt ID: {result.attempt_id}")

    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    sys.exit(asyncio.run(main()))
