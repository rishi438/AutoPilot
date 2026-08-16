"""Extract Workday application links without retaining page content."""

from __future__ import annotations

import html
import random
import re
import time
from collections.abc import Callable
from typing import Any


_WORKDAY_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>]*myworkdayjobs\.com[^\s\"'<>]*",
    re.IGNORECASE,
)
_TIMEOUT_MS = 15_000


def _load_playwright() -> tuple[Callable[[], Any], type[Exception]] | None:
    """Load the optional Playwright dependency only when extraction is used."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright, PlaywrightTimeoutError


def _workday_url(page_html: str) -> str | None:
    """Return the first Workday URL embedded in HTML, if present."""
    match = _WORKDAY_URL_PATTERN.search(html.unescape(page_html))
    return match.group(0) if match else None


def get_workday_link(job_url: str) -> str | None:
    """Return a Workday application URL discovered from a job listing.

    The browser session and page HTML are discarded before this function returns.
    A missing Playwright installation or a 15-second Playwright timeout produces
    no result.
    """
    playwright_api = _load_playwright()
    if playwright_api is None:
        return None

    sync_playwright, playwright_timeout = playwright_api
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_default_timeout(_TIMEOUT_MS)
                page.set_default_navigation_timeout(_TIMEOUT_MS)
                time.sleep(random.uniform(2, 5))
                page.goto(job_url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)

                workday_url = _workday_url(page.content())
                if workday_url:
                    return workday_url

                with page.expect_popup(timeout=_TIMEOUT_MS) as popup_info:
                    page.get_by_role(
                        "button", name=re.compile("apply", re.IGNORECASE)
                    ).click()
                application_url = popup_info.value.url
                if "myworkdayjobs.com" in application_url.lower():
                    return application_url
                return None
            finally:
                browser.close()
    except playwright_timeout:
        return None
