"""Async MongoDB connection boundary for the isolated portal credential vault."""

from __future__ import annotations

import logging
from typing import Any

from config.settings import get_settings
from services.portal_credentials import PortalVaultCollections

logger = logging.getLogger(__name__)

_client: Any | None = None
_collections: PortalVaultCollections | None = None


async def connect_to_portal_vault() -> None:
    """Connect and create required indexes when the vault is enabled."""
    global _client, _collections
    settings = get_settings()
    if not settings.portal_vault_enabled:
        return
    if _client is not None:
        return
    try:
        from pymongo import AsyncMongoClient
    except ImportError as exc:
        raise RuntimeError(
            "PyMongo is required when the portal vault is enabled."
        ) from exc

    uri = settings.portal_vault_mongodb_url
    if uri is None:
        raise RuntimeError("Portal vault MongoDB URL is not configured.")
    client = AsyncMongoClient(
        uri.get_secret_value(),
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )
    try:
        await client.admin.command("ping")
        database = client[settings.portal_vault_mongodb_database]
        credentials = database["portal_credentials"]
        events = database["portal_credential_events"]
        await credentials.create_index(
            [("user_id", 1), ("portal_scope", 1)],
            unique=True,
            name="uq_portal_credential_user_scope",
        )
        await events.create_index(
            [("user_id", 1), ("created_at", -1)],
            name="ix_portal_credential_event_user_created",
        )
    except Exception:
        await client.close()
        raise
    _client = client
    _collections = PortalVaultCollections(credentials=credentials, events=events)
    logger.info("Portal credential vault connection initialized")


async def close_portal_vault() -> None:
    """Close the MongoDB client without exposing connection details."""
    global _client, _collections
    client = _client
    _client = None
    _collections = None
    if client is not None:
        await client.close()


def get_portal_vault_collections() -> PortalVaultCollections:
    """Return initialized collections for request dependency injection."""
    if _collections is None:
        raise RuntimeError("Portal credential vault is unavailable.")
    return _collections
