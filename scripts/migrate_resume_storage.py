#!/usr/bin/env python3
"""Move legacy filesystem resume originals to configured MinIO storage.

Run inside the app container after MinIO is healthy. The command is safe to
rerun: rows already using the ``minio:`` prefix are skipped. Legacy files are
intentionally retained in the Compose volume for recovery until verified.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import get_database_settings, get_settings
from models.database import UserResumeAsset
from utils.user_resume_storage import (
    is_minio_storage_path,
    read_resume_content,
    save_resume_content,
)

logger = logging.getLogger(__name__)


async def migrate() -> int:
    """Migrate each legacy row and return nonzero when any row could not move."""
    settings = get_settings()
    if settings.resume_storage_backend != "minio":
        logger.error("Set RESUME_STORAGE_BACKEND=minio before migrating resumes.")
        return 2

    database_settings = get_database_settings()
    engine = create_async_engine(database_settings.async_database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    migrated = 0
    failures = 0
    try:
        async with session_factory() as session:
            assets = (await session.scalars(select(UserResumeAsset))).all()
            for asset in assets:
                if is_minio_storage_path(asset.storage_relative_path):
                    continue
                try:
                    content = await read_resume_content(
                        settings.user_resume_storage_dir, asset.storage_relative_path
                    )
                    key, digest, _extension = await save_resume_content(
                        settings.user_resume_storage_dir,
                        asset.user_id,
                        content,
                        asset.original_filename,
                    )
                    asset.storage_relative_path = key
                    asset.sha256_hex = digest
                    asset.byte_size = len(content)
                    await session.commit()
                    migrated += 1
                except (OSError, RuntimeError, ValueError):
                    await session.rollback()
                    failures += 1
                    logger.exception(
                        "Resume migration failed for asset_id=%s", asset.id
                    )
    finally:
        await engine.dispose()

    logger.info(
        "Resume migration complete: migrated=%s failures=%s", migrated, failures
    )
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(asyncio.run(migrate()))
