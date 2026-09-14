#!/usr/bin/env python3
"""Add missing portal-vault secrets to an existing dotenv without printing values."""

from __future__ import annotations

import base64
import os
import secrets
import tempfile
from pathlib import Path


def _new_values() -> dict[str, str]:
    return {
        "PORTAL_VAULT_ROOT_USERNAME": f"vaultroot_{secrets.token_hex(8)}",
        "PORTAL_VAULT_ROOT_PASSWORD": secrets.token_urlsafe(48),
        "PORTAL_VAULT_APP_USERNAME": f"vaultapp_{secrets.token_hex(8)}",
        "PORTAL_VAULT_APP_PASSWORD": secrets.token_urlsafe(48),
        "PORTAL_VAULT_MONGODB_DATABASE": "autopilot_vault",
        "PORTAL_VAULT_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(
            "ascii"
        ),
        "PORTAL_VAULT_PASSWORD_RECOVERY_KEY": base64.urlsafe_b64encode(
            os.urandom(32)
        ).decode("ascii"),
    }


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.is_file():
        raise SystemExit(".env is missing; run the normal dotenv setup first.")

    content = env_path.read_text(encoding="utf-8")
    existing_names = {
        line.split("=", 1)[0].strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    additions = {
        name: value
        for name, value in _new_values().items()
        if name not in existing_names
    }
    if not additions:
        print("  Portal vault environment is already configured.")
        return

    suffix = "" if content.endswith("\n") else "\n"
    suffix += "\n# Isolated portal credential vault\n"
    suffix += "".join(f"{name}={value}\n" for name, value in additions.items())
    handle, temporary_name = tempfile.mkstemp(
        prefix=".env.portal-vault-", dir=root, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.write(suffix)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, env_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"  Added {len(additions)} portal vault environment entries.")


if __name__ == "__main__":
    main()
