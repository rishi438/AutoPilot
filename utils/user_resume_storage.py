"""Private storage for user resume uploads (PDF/DOCX/TXT).

Filesystem paths remain readable during the MinIO migration. New object-store
paths are explicitly prefixed so a database row can never be interpreted as
the wrong storage backend.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import uuid
from asyncio import to_thread
from pathlib import Path
from typing import Optional, Tuple

from config.settings import get_settings

logger = logging.getLogger(__name__)

_MINIO_PATH_PREFIX = "minio:"
_MINIO_OBJECT_KEY_PATTERN = re.compile(
    r"users/[0-9a-f-]{36}/resumes/[0-9a-f]{32}\.(?:pdf|docx|txt|bin)"
)


def _object_key(user_id: uuid.UUID, filename: str) -> tuple[str, str]:
    ext = "bin"
    if filename and "." in filename:
        candidate = filename.rsplit(".", 1)[-1].lower()
        if candidate in ("pdf", "docx", "txt"):
            ext = candidate
    return f"users/{user_id}/resumes/{uuid.uuid4().hex}.{ext}", ext


def is_minio_storage_path(storage_relative_path: str) -> bool:
    """Return whether a stored path is an object-store path."""
    return storage_relative_path.startswith(_MINIO_PATH_PREFIX)


def _minio_object_key(storage_relative_path: str) -> str:
    if not is_minio_storage_path(storage_relative_path):
        raise ValueError("Expected a MinIO resume storage path")
    key = storage_relative_path.removeprefix(_MINIO_PATH_PREFIX)
    if not _MINIO_OBJECT_KEY_PATTERN.fullmatch(key):
        raise ValueError("Invalid MinIO resume storage path")
    return key


def _minio_client():
    cfg = get_settings()
    if not (cfg.minio_endpoint and cfg.minio_access_key and cfg.minio_secret_key):
        raise RuntimeError("MinIO resume storage is not configured.")
    from minio import Minio

    return Minio(
        cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        secure=cfg.minio_secure,
    )


async def save_resume_content(
    base_dir: str, user_id: uuid.UUID, content: bytes, original_filename: str
) -> Tuple[str, str, str]:
    """Persist one resume privately, using MinIO when configured."""
    cfg = get_settings()
    if cfg.resume_storage_backend != "minio":
        return save_resume_bytes(base_dir, user_id, content, original_filename)
    key, ext = _object_key(user_id, original_filename)
    digest = hashlib.sha256(content).hexdigest()

    def _put() -> None:
        client = _minio_client()
        for attempt in range(3):
            try:
                if not client.bucket_exists(cfg.minio_bucket):
                    client.make_bucket(cfg.minio_bucket)
                client.put_object(
                    cfg.minio_bucket, key, io.BytesIO(content), len(content)
                )
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))

    await to_thread(_put)
    return f"{_MINIO_PATH_PREFIX}{key}", digest, ext


async def read_resume_content(base_dir: str, storage_relative_path: str) -> bytes:
    if not is_minio_storage_path(storage_relative_path):
        return resume_absolute_path(base_dir, storage_relative_path).read_bytes()
    cfg = get_settings()
    key = _minio_object_key(storage_relative_path)

    def _get() -> bytes:
        response = _minio_client().get_object(cfg.minio_bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    return await to_thread(_get)


async def delete_resume_content(
    base_dir: str, storage_relative_path: Optional[str]
) -> None:
    if not storage_relative_path:
        return
    if not is_minio_storage_path(storage_relative_path):
        delete_resume_file(base_dir, storage_relative_path)
        return
    cfg = get_settings()
    key = _minio_object_key(storage_relative_path)
    await to_thread(_minio_client().remove_object, cfg.minio_bucket, key)


def _abs_root(base_dir: str) -> Path:
    root = Path(base_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resume_absolute_path(base_dir: str, storage_relative_path: str) -> Path:
    """
    Resolve a DB-stored relative path under the configured root.

    Raises:
        ValueError: If the path escapes the storage root.
    """
    root = _abs_root(base_dir)
    rel = Path(storage_relative_path)
    if rel.is_absolute():
        raise ValueError("Invalid storage path")
    full = (root / rel).resolve()
    try:
        full.relative_to(root)
    except ValueError as e:
        raise ValueError("Path traversal rejected") from e
    return full


def save_resume_bytes(
    base_dir: str, user_id: uuid.UUID, content: bytes, original_filename: str
) -> Tuple[str, str, str]:
    """
    Write resume bytes to ``{root}/{user_id}/{random}.{ext}``.

    Returns:
        Tuple of (storage_relative_path, sha256_hex, normalized_extension_without_dot)
    """
    root = _abs_root(base_dir)
    user_dir = root / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    ext = "bin"
    if original_filename and "." in original_filename:
        raw = original_filename.rsplit(".", 1)[-1].lower()
        if raw in ("pdf", "docx", "txt"):
            ext = raw

    fname = f"{uuid.uuid4().hex}.{ext}"
    rel = f"{user_id}/{fname}"
    dest = user_dir / fname

    h = hashlib.sha256(content).hexdigest()
    dest.write_bytes(content)
    return rel, h, ext


def delete_resume_file(base_dir: str, storage_relative_path: Optional[str]) -> None:
    """Remove the on-disk file if present; ignores missing files."""
    if not storage_relative_path:
        return
    try:
        path = resume_absolute_path(base_dir, storage_relative_path)
    except ValueError as e:
        logger.warning(
            "Refusing to delete resume path %s: %s", storage_relative_path, e
        )
        return
    try:
        if path.is_file():
            path.unlink()
    except OSError as e:
        logger.warning("Failed to delete resume file %s: %s", path, e, exc_info=True)

    # Remove empty user directory
    try:
        parent = path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as e:
        logger.debug("Could not remove empty resume directory: %s", e, exc_info=True)
