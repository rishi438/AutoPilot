"""Encrypted portal-credential policy for the local autonomous worker."""

from __future__ import annotations

import re
import secrets
import string
import uuid
import base64
import hashlib
import hmac
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

_VAULT_PREFIX = "portal-vault:v1:"
_PORTAL_SCOPE = re.compile(r"[a-z0-9][a-z0-9._:-]{2,199}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_LOWER = string.ascii_lowercase
_UPPER = string.ascii_uppercase
_DIGITS = string.digits
_SPECIAL = "!@#$%^&*_-+="
_PASSWORD_ALPHABET = _LOWER + _UPPER + _DIGITS + _SPECIAL


class PortalCredentialError(ValueError):
    """Raised when vault data is invalid, unavailable, or cannot be decrypted."""


@dataclass(frozen=True)
class PortalVaultCollections:
    """MongoDB collections kept behind one injectable dependency boundary."""

    credentials: Any
    events: Any


@dataclass(frozen=True)
class WorkerPortalCredential:
    """Short-lived in-process secret for the local browser worker only."""

    credential_id: str
    portal_scope: str
    account_email: str
    status: Literal["active", "pending_registration"]
    password: str = dataclass_field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkerPortalAccountMetadata:
    """Safe owned-account identity resolved without decrypting its secret."""

    account_ref: uuid.UUID
    portal_scope: str
    status: Literal["active", "pending_registration"]


def generate_portal_password(length: int = 24) -> str:
    """Generate a Workday-compatible password with cryptographic randomness."""
    if length < 16:
        raise PortalCredentialError(
            "Generated portal passwords must be at least 16 characters."
        )
    characters = [
        secrets.choice(_LOWER),
        secrets.choice(_UPPER),
        secrets.choice(_DIGITS),
        secrets.choice(_SPECIAL),
    ]
    characters.extend(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length - 4))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def derive_portal_password(
    *,
    recovery_key: str,
    account_email: str,
    portal_scope: str,
    version: int = 1,
    length: int = 24,
) -> str:
    """Reproduce one unique password after MongoDB loss without cross-tenant reuse."""
    if length < 16:
        raise PortalCredentialError(
            "Derived portal passwords must be at least 16 characters."
        )
    scope = normalize_portal_scope(portal_scope)
    email = account_email.strip().lower()
    if not email or "@" not in email:
        raise PortalCredentialError("A stable account email is required for recovery.")
    try:
        key_bytes = base64.urlsafe_b64decode(recovery_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise PortalCredentialError("Portal password recovery is unavailable.") from exc
    if len(key_bytes) != 32:
        raise PortalCredentialError("Portal password recovery is unavailable.")
    message = f"portal-password:v{version}\x00{email}\x00{scope}".encode("utf-8")
    material = hmac.new(key_bytes, message, hashlib.sha512).digest()
    characters = [
        _LOWER[material[0] % len(_LOWER)],
        _UPPER[material[1] % len(_UPPER)],
        _DIGITS[material[2] % len(_DIGITS)],
        _SPECIAL[material[3] % len(_SPECIAL)],
    ]
    characters.extend(
        _PASSWORD_ALPHABET[material[index] % len(_PASSWORD_ALPHABET)]
        for index in range(4, length)
    )
    return "".join(characters)


def normalize_portal_scope(value: str) -> str:
    """Return a stable, adapter-defined portal account scope."""
    normalized = value.strip().lower()
    if not _PORTAL_SCOPE.fullmatch(normalized):
        raise PortalCredentialError(
            "Portal scope must be a lowercase stable identifier such as workday:wf:wellsfargojobs."
        )
    return normalized


def normalize_portal_login_url(value: str) -> str:
    """Return a safe HTTPS URL without query parameters or fragments."""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise PortalCredentialError("Portal login URLs must use HTTPS.")
    if parsed.username or parsed.password:
        raise PortalCredentialError("Portal login URLs must not contain credentials.")
    host = parsed.hostname.lower()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def validate_portal_password(value: str) -> str:
    """Validate a generated or user-supplied external portal password."""
    if not 8 <= len(value) <= 256:
        raise PortalCredentialError(
            "Portal passwords must be 8 to 256 characters long."
        )
    if _CONTROL_CHARACTERS.search(value):
        raise PortalCredentialError(
            "Portal passwords cannot contain control characters."
        )
    return value


def encrypt_portal_password(value: str, encryption_key: str) -> str:
    """Encrypt a recoverable external-system password with the vault key."""
    password = validate_portal_password(value)
    try:
        token = Fernet(encryption_key.encode("ascii")).encrypt(password.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PortalCredentialError("Portal vault encryption is unavailable.") from exc
    return f"{_VAULT_PREFIX}{token.decode('ascii')}"


def decrypt_portal_password(value: str, encryption_key: str) -> str:
    """Decrypt a portal password only at an authorized use boundary."""
    if not value.startswith(_VAULT_PREFIX):
        raise PortalCredentialError("Unsupported portal credential format.")
    try:
        plaintext = Fernet(encryption_key.encode("ascii")).decrypt(
            value.removeprefix(_VAULT_PREFIX).encode("ascii")
        )
    except (InvalidToken, TypeError, ValueError) as exc:
        raise PortalCredentialError(
            "Portal credential could not be decrypted."
        ) from exc
    return validate_portal_password(plaintext.decode("utf-8"))


def safe_credential_view(document: dict[str, Any]) -> dict[str, Any]:
    """Serialize safe metadata without ciphertext, password, or session material."""
    return {
        "id": str(document["_id"]),
        "portal_scope": document["portal_scope"],
        "portal_name": document["portal_name"],
        "portal_login_url": document["portal_login_url"],
        "account_email": document["account_email"],
        "credential_source": document["credential_source"],
        "status": document["status"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "last_revealed_at": document.get("last_revealed_at"),
    }


class PortalAccountMetadataRepository:
    """Resolve owned opaque account identity without reading credential material."""

    def __init__(self, credentials: Any):
        self._credentials = credentials

    async def resolve_account_ref(
        self, *, user_id: str, portal_scope: str
    ) -> uuid.UUID | None:
        scope = normalize_portal_scope(portal_scope)
        document = await self._credentials.find_one(
            {"user_id": user_id, "portal_scope": scope},
            {"_id": 1, "user_id": 1, "portal_scope": 1},
        )
        if document is None:
            return None
        if document.get("user_id") != user_id or document.get("portal_scope") != scope:
            raise PortalCredentialError("Portal account metadata ownership is invalid.")
        try:
            return uuid.UUID(str(document["_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PortalCredentialError("Portal account reference is invalid.") from exc


class PortalCredentialRepository:
    """User-scoped MongoDB repository for encrypted portal credentials."""

    def __init__(
        self,
        collections: PortalVaultCollections,
        encryption_key: str,
        password_recovery_key: str,
    ):
        self._credentials = collections.credentials
        self._events = collections.events
        self._encryption_key = encryption_key
        self._password_recovery_key = password_recovery_key

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        cursor = self._credentials.find({"user_id": user_id}).sort("updated_at", -1)
        documents = await cursor.to_list(length=200)
        return [safe_credential_view(document) for document in documents]

    async def generate_if_missing(
        self,
        *,
        user_id: str,
        portal_scope: str,
        portal_name: str,
        portal_login_url: str,
        account_email: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create one random password per portal scope without rotating an existing one."""
        scope = normalize_portal_scope(portal_scope)
        now = datetime.now(UTC)
        document = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "portal_scope": scope,
            "portal_name": portal_name.strip(),
            "portal_login_url": normalize_portal_login_url(portal_login_url),
            "account_email": account_email.strip().lower(),
            "password_encrypted": encrypt_portal_password(
                derive_portal_password(
                    recovery_key=self._password_recovery_key,
                    account_email=account_email,
                    portal_scope=scope,
                ),
                self._encryption_key,
            ),
            "password_derivation": "hmac-sha512:v1",
            "credential_source": "generated",
            "status": "pending_registration",
            "created_at": now,
            "updated_at": now,
            "reveal_count": 0,
        }
        stored = await self._credentials.find_one_and_update(
            {"user_id": user_id, "portal_scope": scope},
            {"$setOnInsert": document},
            upsert=True,
            return_document=True,
        )
        created = stored["_id"] == document["_id"]
        if created:
            await self._record_event(user_id, stored, "credential_generated")
        return safe_credential_view(stored), created

    async def store_existing(
        self,
        *,
        user_id: str,
        portal_scope: str,
        portal_name: str,
        portal_login_url: str,
        account_email: str,
        password: str,
    ) -> dict[str, Any]:
        """Encrypt and upsert credentials explicitly supplied by their owner."""
        scope = normalize_portal_scope(portal_scope)
        now = datetime.now(UTC)
        stored = await self._credentials.find_one_and_update(
            {"user_id": user_id, "portal_scope": scope},
            {
                "$set": {
                    "portal_name": portal_name.strip(),
                    "portal_login_url": normalize_portal_login_url(portal_login_url),
                    "account_email": account_email.strip().lower(),
                    "password_encrypted": encrypt_portal_password(
                        password, self._encryption_key
                    ),
                    "credential_source": "user_supplied",
                    "status": "active",
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "portal_scope": scope,
                    "created_at": now,
                    "reveal_count": 0,
                },
            },
            upsert=True,
            return_document=True,
        )
        await self._record_event(user_id, stored, "credential_imported")
        return safe_credential_view(stored)

    async def reveal_for_user(self, *, user_id: str, credential_id: str) -> str | None:
        """Decrypt one owned credential and record a value-free audit event."""
        document = await self._credentials.find_one(
            {"_id": credential_id, "user_id": user_id}
        )
        if document is None:
            return None
        password = decrypt_portal_password(
            document["password_encrypted"], self._encryption_key
        )
        now = datetime.now(UTC)
        await self._credentials.update_one(
            {"_id": credential_id, "user_id": user_id},
            {"$set": {"last_revealed_at": now}, "$inc": {"reveal_count": 1}},
        )
        await self._record_event(user_id, document, "credential_revealed")
        return password

    async def credential_for_worker(
        self, *, user_id: str, portal_scope: str
    ) -> WorkerPortalCredential | None:
        """Decrypt an owned credential only for immediate local form use."""
        scope = normalize_portal_scope(portal_scope)
        document = await self._credentials.find_one(
            {"user_id": user_id, "portal_scope": scope}
        )
        if document is None:
            return None
        if document.get("user_id") != user_id or document.get("portal_scope") != scope:
            raise PortalCredentialError("Portal account metadata ownership is invalid.")
        status = document.get("status")
        if status not in {"active", "pending_registration"}:
            raise PortalCredentialError("Portal credential status is invalid.")
        credential = WorkerPortalCredential(
            credential_id=str(document["_id"]),
            portal_scope=document["portal_scope"],
            account_email=document["account_email"],
            status=status,
            password=decrypt_portal_password(
                document["password_encrypted"], self._encryption_key
            ),
        )
        await self._record_event(user_id, document, "credential_used_by_worker")
        return credential

    async def account_metadata_for_worker(
        self, *, user_id: str, portal_scope: str
    ) -> WorkerPortalAccountMetadata | None:
        """Resolve owned account identity/status without reading ciphertext."""
        scope = normalize_portal_scope(portal_scope)
        document = await self._credentials.find_one(
            {"user_id": user_id, "portal_scope": scope},
            {"_id": 1, "user_id": 1, "portal_scope": 1, "status": 1},
        )
        if document is None:
            return None
        if document.get("user_id") != user_id or document.get("portal_scope") != scope:
            raise PortalCredentialError("Portal account metadata ownership is invalid.")
        status = document.get("status")
        if status not in {"active", "pending_registration"}:
            raise PortalCredentialError("Portal credential status is invalid.")
        try:
            account_ref = uuid.UUID(str(document["_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PortalCredentialError("Portal account reference is invalid.") from exc
        return WorkerPortalAccountMetadata(
            account_ref=account_ref,
            portal_scope=scope,
            status=status,
        )

    async def credential_for_auth_broker(
        self, *, user_id: str, account_ref: uuid.UUID, portal_scope: str
    ) -> WorkerPortalCredential | None:
        """Decrypt exactly one owned account reference for the auth broker."""
        scope = normalize_portal_scope(portal_scope)
        document = await self._credentials.find_one(
            {
                "_id": str(account_ref),
                "user_id": user_id,
                "portal_scope": scope,
            }
        )
        if document is None:
            return None
        if (
            str(document.get("_id")) != str(account_ref)
            or document.get("user_id") != user_id
            or document.get("portal_scope") != scope
        ):
            raise PortalCredentialError("Portal credential ownership is invalid.")
        status = document.get("status")
        if status not in {"active", "pending_registration"}:
            raise PortalCredentialError("Portal credential status is invalid.")
        credential = WorkerPortalCredential(
            credential_id=str(document["_id"]),
            portal_scope=document["portal_scope"],
            account_email=document["account_email"],
            status=status,
            password=decrypt_portal_password(
                document["password_encrypted"], self._encryption_key
            ),
        )
        await self._record_event(user_id, document, "credential_used_by_auth_broker")
        return credential

    async def mark_account_ready(
        self,
        *,
        user_id: str,
        portal_scope: str,
        method: Literal["login", "registration"],
    ) -> bool:
        """Mark a credential active only after the portal confirms account access."""
        scope = normalize_portal_scope(portal_scope)
        now = datetime.now(UTC)
        stored = await self._credentials.find_one_and_update(
            {"user_id": user_id, "portal_scope": scope},
            {"$set": {"status": "active", "updated_at": now}},
            upsert=False,
            return_document=True,
        )
        if stored is None:
            return False
        await self._record_event(
            user_id,
            stored,
            f"credential_{method}_succeeded",
        )
        return True

    async def _record_event(
        self, user_id: str, credential: dict[str, Any], event_type: str
    ) -> None:
        await self._events.insert_one(
            {
                "_id": str(uuid.uuid4()),
                "user_id": user_id,
                "credential_id": str(credential["_id"]),
                "portal_scope": credential["portal_scope"],
                "event_type": event_type,
                "created_at": datetime.now(UTC),
            }
        )
