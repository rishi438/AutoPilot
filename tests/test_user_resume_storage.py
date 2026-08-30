from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from utils.user_resume_storage import (
    delete_resume_content,
    read_resume_content,
    save_resume_content,
)


@pytest.mark.asyncio
async def test_filesystem_resume_stays_readable_after_minio_is_enabled(tmp_path):
    user_id = uuid4()
    user_dir = tmp_path / str(user_id)
    user_dir.mkdir()
    legacy_path = f"{user_id}/legacy.pdf"
    (user_dir / "legacy.pdf").write_bytes(b"legacy resume")

    assert await read_resume_content(str(tmp_path), legacy_path) == b"legacy resume"


@pytest.mark.asyncio
async def test_minio_upload_uses_scoped_prefixed_key():
    config = SimpleNamespace(
        resume_storage_backend="minio",
        minio_endpoint="minio:9000",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket="autopilot-resumes",
        minio_secure=False,
    )
    client = MagicMock()
    client.bucket_exists.return_value = True

    with (
        patch("utils.user_resume_storage.get_settings", return_value=config),
        patch("utils.user_resume_storage._minio_client", return_value=client),
    ):
        path, digest, extension = await save_resume_content(
            "unused", uuid4(), b"resume content", "resume.pdf"
        )

    assert path.startswith("minio:users/")
    assert path.endswith(".pdf")
    assert len(digest) == 64
    assert extension == "pdf"
    client.put_object.assert_called_once()


@pytest.mark.asyncio
async def test_invalid_minio_path_is_rejected_before_object_read():
    with pytest.raises(ValueError, match="Invalid MinIO"):
        await read_resume_content("unused", "minio:users/other-user/private.txt")


@pytest.mark.asyncio
async def test_minio_delete_removes_only_valid_object_key():
    config = SimpleNamespace(minio_bucket="autopilot-resumes")
    client = MagicMock()
    key = "minio:users/12345678-1234-1234-1234-123456789abc/resumes/abcdef0123456789abcdef0123456789.txt"

    with (
        patch("utils.user_resume_storage.get_settings", return_value=config),
        patch("utils.user_resume_storage._minio_client", return_value=client),
    ):
        await delete_resume_content("unused", key)

    client.remove_object.assert_called_once_with(
        "autopilot-resumes",
        "users/12345678-1234-1234-1234-123456789abc/resumes/abcdef0123456789abcdef0123456789.txt",
    )
