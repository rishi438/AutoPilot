#!/usr/bin/env python3
"""Create .env from .env.local.example with random secrets (Unix / macOS / manual dev)."""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    example = root / ".env.local.example"
    if env_path.exists():
        print("  .env already exists — skipping.")
        return
    content = example.read_text(encoding="utf-8")
    jwt_val = secrets.token_urlsafe(48)
    enc_val = base64.urlsafe_b64encode(os.urandom(32)).decode()
    minio_root_user = secrets.token_urlsafe(18)
    minio_root_password = secrets.token_urlsafe(48)
    minio_access_key = secrets.token_urlsafe(18)
    minio_secret_key = secrets.token_urlsafe(48)
    portal_vault_root_username = f"vaultroot_{secrets.token_hex(8)}"
    portal_vault_root_password = secrets.token_urlsafe(48)
    portal_vault_app_username = f"vaultapp_{secrets.token_hex(8)}"
    portal_vault_app_password = secrets.token_urlsafe(48)
    portal_vault_encryption_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    portal_vault_password_recovery_key = base64.urlsafe_b64encode(
        os.urandom(32)
    ).decode()
    content = content.replace("REPLACE_WITH_STRONG_SECRET_AT_LEAST_32_CHARS", jwt_val)
    content = content.replace("REPLACE_WITH_FERNET_KEY", enc_val)
    content += (
        f"\nMINIO_ROOT_USER={minio_root_user}\n"
        f"MINIO_ROOT_PASSWORD={minio_root_password}\n"
        f"MINIO_ACCESS_KEY={minio_access_key}\n"
        f"MINIO_SECRET_KEY={minio_secret_key}\n"
        f"PORTAL_VAULT_ROOT_USERNAME={portal_vault_root_username}\n"
        f"PORTAL_VAULT_ROOT_PASSWORD={portal_vault_root_password}\n"
        f"PORTAL_VAULT_APP_USERNAME={portal_vault_app_username}\n"
        f"PORTAL_VAULT_APP_PASSWORD={portal_vault_app_password}\n"
        "PORTAL_VAULT_MONGODB_DATABASE=autopilot_vault\n"
        f"PORTAL_VAULT_ENCRYPTION_KEY={portal_vault_encryption_key}\n"
        f"PORTAL_VAULT_PASSWORD_RECOVERY_KEY={portal_vault_password_recovery_key}\n"
    )
    env_path.write_text(content, encoding="utf-8")
    print("  .env created with auto-generated secrets.")


if __name__ == "__main__":
    main()
