"""Launch and close an isolated blank Chromium profile without portal access."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from services.workday_browser_runtime import PersistentWorkdayBrowserRuntime


async def _smoke() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="autopilot-browser-smoke-") as temporary_dir:
        profile_dir = Path(temporary_dir) / "profile"
        async with PersistentWorkdayBrowserRuntime(
            profile_dir=profile_dir,
            repository_root=repository_root,
            headless=True,
        ):
            pass


def main() -> None:
    asyncio.run(_smoke())
    print("Autopilot Chromium persistent-profile smoke test passed.")


if __name__ == "__main__":
    main()
