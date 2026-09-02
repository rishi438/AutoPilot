"""Dedicated persistent Playwright runtime for the local Workday worker."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Callable

from filelock import FileLock, Timeout

from services.portal_control_resolver import PortalControlResolver
from services.workday_playwright_worker import (
    PlaywrightWorkdayBrowser,
    WorkdayWorkerError,
)

_PROFILE_DIRECTORY_NAME = "Autopilot Browser"
_USER_PROFILE_DIRECTORY_NAME = "Autopilot Browser Users"
_PROFILE_LOCK_SUFFIX = ".worker.lock"
logger = logging.getLogger(__name__)


def default_autopilot_browser_profile() -> Path:
    """Return the local, non-repository browser profile path on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise WorkdayWorkerError("LOCALAPPDATA is required for the browser profile.")
    return Path(local_app_data) / "Autopilot" / _PROFILE_DIRECTORY_NAME


def user_scoped_autopilot_browser_profile(
    user_id: str, *, must_exist: bool = False
) -> Path:
    """Return an isolated persistent profile for exactly one AutoPilot user."""
    try:
        normalized_user_id = str(uuid.UUID(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkdayWorkerError(
            "A valid AutoPilot user ID is required for browser isolation."
        ) from exc
    profile_path = (
        default_autopilot_browser_profile().parent
        / _USER_PROFILE_DIRECTORY_NAME
        / normalized_user_id
    )
    if must_exist and not profile_path.exists():
        raise WorkdayWorkerError(
            "The existing browser profile for this user is missing."
        )
    return profile_path


def validate_browser_profile_path(
    profile_dir: Path,
    *,
    repository_root: Path,
) -> Path:
    """Keep secret-bearing browser state outside the repository and its parents."""
    resolved_profile = profile_dir.expanduser().resolve(strict=False)
    resolved_repository = repository_root.resolve(strict=True)
    if (
        resolved_profile == resolved_repository
        or resolved_repository in resolved_profile.parents
    ):
        raise WorkdayWorkerError(
            "The Autopilot Browser profile must be stored outside the repository."
        )
    if resolved_profile.parent == resolved_profile:
        raise WorkdayWorkerError(
            "A filesystem root cannot be used as a browser profile."
        )
    return resolved_profile


def _load_async_playwright() -> Callable[[], Any]:
    """Load Playwright only for the runnable local-worker path."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise WorkdayWorkerError(
            "Playwright is not installed. Install the pinned worker dependencies."
        ) from exc
    return async_playwright


class PersistentWorkdayBrowserRuntime:
    """Own one locked persistent Chromium profile without remote debugging."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        repository_root: Path,
        headless: bool = False,
        timeout_ms: int = 15_000,
        accept_account_terms: bool = False,
        control_resolver: PortalControlResolver | None = None,
        decision_reporter: Callable[[str], None] | None = None,
    ):
        self._profile_dir = validate_browser_profile_path(
            profile_dir, repository_root=repository_root
        )
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._accept_account_terms = accept_account_terms
        self._control_resolver = control_resolver
        self._decision_reporter = decision_reporter
        # Acquisition runs in a helper thread so it cannot block the event loop;
        # shared context lets the event-loop thread release the same OS handle.
        self._lock = FileLock(
            f"{self._profile_dir}{_PROFILE_LOCK_SUFFIX}", thread_local=False
        )
        self._playwright: Any | None = None
        self._context: Any | None = None

    async def __aenter__(self) -> PlaywrightWorkdayBrowser:
        logger.info(
            "workday_browser_runtime_starting headless=%s",
            self._headless,
        )
        self._profile_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._lock.acquire, timeout=0)
        except Timeout as exc:
            logger.warning("workday_browser_profile_busy")
            raise WorkdayWorkerError(
                "The dedicated Autopilot Browser profile is already in use."
            ) from exc

        try:
            manager = _load_async_playwright()()
            self._playwright = await manager.start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir),
                headless=self._headless,
                accept_downloads=False,
            )
            logger.info(
                "workday_browser_runtime_started existing_pages=%s",
                len(self._context.pages),
            )
            page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            return PlaywrightWorkdayBrowser(
                page,
                timeout_ms=self._timeout_ms,
                accept_account_terms=self._accept_account_terms,
                control_resolver=self._control_resolver,
                decision_reporter=self._decision_reporter,
            )
        except BaseException:
            logger.exception("workday_browser_runtime_start_failed")
            await self._close_runtime()
            self._lock.release()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        try:
            await self._close_runtime()
        finally:
            self._lock.release()
            logger.info("workday_browser_runtime_stopped")

    async def _close_runtime(self) -> None:
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        if context is not None:
            await context.close()
        if playwright is not None:
            await playwright.stop()

    def verify_account_continuity(self, user_id: Any) -> bool:
        """Factual check that this runtime owns the locked persistent profile for user_id."""
        try:
            expected_profile = user_scoped_autopilot_browser_profile(
                str(user_id), must_exist=True
            ).resolve()
            if self._profile_dir.resolve() != expected_profile:
                return False
            if not getattr(self._lock, "is_locked", False):
                return False
            if self._context is None:
                return False
            return True
        except Exception:
            return False

    @property
    def existing_pages(self) -> list[Any]:
        """Return the open pages in the active persistent browser context."""
        if self._context is None:
            return []
        return list(self._context.pages)

    def borrow_playwright_browser_for_page(
        self, page: Any | None = None
    ) -> PlaywrightWorkdayBrowser:
        """Wrap an existing open page from this runtime into a PlaywrightWorkdayBrowser."""
        target_page = page
        if target_page is None:
            pages = self.existing_pages
            if not pages:
                raise WorkdayWorkerError(
                    "No existing browser page available to borrow."
                )
            target_page = pages[0]
        return PlaywrightWorkdayBrowser(
            target_page,
            timeout_ms=self._timeout_ms,
            accept_account_terms=self._accept_account_terms,
            control_resolver=self._control_resolver,
            decision_reporter=self._decision_reporter,
        )
