from __future__ import annotations

from pathlib import Path

import pytest

from services.workday_browser_runtime import (
    PersistentWorkdayBrowserRuntime,
    default_autopilot_browser_profile,
    user_scoped_autopilot_browser_profile,
    validate_browser_profile_path,
)
from services.workday_playwright_worker import (
    PlaywrightWorkdayBrowser,
    WorkdayWorkerError,
)


def test_default_profile_uses_local_app_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_autopilot_browser_profile() == (
        tmp_path / "Autopilot" / "Autopilot Browser"
    )


def test_user_scoped_profile_isolates_autopilot_accounts(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert user_scoped_autopilot_browser_profile(
        "00000000-0000-0000-0000-000000000003"
    ) == (
        tmp_path
        / "Autopilot"
        / "Autopilot Browser Users"
        / "00000000-0000-0000-0000-000000000003"
    )

    with pytest.raises(WorkdayWorkerError, match="valid AutoPilot user ID"):
        user_scoped_autopilot_browser_profile("../shared-profile")


def test_profile_must_stay_outside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    with pytest.raises(WorkdayWorkerError, match="outside the repository"):
        validate_browser_profile_path(
            repository / ".tmp" / "browser",
            repository_root=repository,
        )

    assert (
        validate_browser_profile_path(
            tmp_path / "local-app-data" / "browser",
            repository_root=repository,
        )
        == (tmp_path / "local-app-data" / "browser").resolve()
    )


class _FakePage:
    pass


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]
        self.closed = False

    async def new_page(self):
        raise AssertionError("existing page should be reused")

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext):
        self.context = context
        self.launch_options = None

    async def launch_persistent_context(self, **options):
        self.launch_options = options
        return self.context


class _FakePlaywright:
    def __init__(self):
        self.context = _FakeContext()
        self.chromium = _FakeChromium(self.context)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeManager:
    def __init__(self, playwright: _FakePlaywright):
        self.playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self.playwright


@pytest.mark.asyncio
async def test_runtime_locks_and_closes_persistent_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    profile = tmp_path / "local-app-data" / "browser"
    fake_playwright = _FakePlaywright()
    control_resolver = object()
    decision_reporter = lambda event: None
    monkeypatch.setattr(
        "services.workday_browser_runtime._load_async_playwright",
        lambda: lambda: _FakeManager(fake_playwright),
    )

    runtime = PersistentWorkdayBrowserRuntime(
        profile_dir=profile,
        repository_root=repository,
        accept_account_terms=True,
        control_resolver=control_resolver,
        decision_reporter=decision_reporter,
    )
    async with runtime as browser:
        assert isinstance(browser, PlaywrightWorkdayBrowser)
        assert browser._accept_account_terms is True
        assert browser._control_resolver is control_resolver
        assert browser._decision_reporter is decision_reporter
        assert fake_playwright.chromium.launch_options == {
            "user_data_dir": str(profile.resolve()),
            "headless": False,
            "accept_downloads": False,
        }

    assert fake_playwright.context.closed is True
    assert fake_playwright.stopped is True
    assert runtime._lock.is_locked is False
