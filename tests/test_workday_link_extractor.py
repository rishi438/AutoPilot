"""Unit tests for Workday application-link discovery."""

from unittest.mock import MagicMock, patch

from app.scrapers.workday_link_extractor import get_workday_link


def test_get_workday_link_returns_embedded_workday_url_without_clicking() -> None:
    sync_playwright = MagicMock()
    playwright = MagicMock()
    browser = MagicMock()
    page = MagicMock()
    sync_playwright.return_value.__enter__.return_value = playwright
    playwright.chromium.launch.return_value = browser
    browser.new_page.return_value = page
    page.content.return_value = (
        '<a href="https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123">Apply</a>'
    )

    with (
        patch(
            "app.scrapers.workday_link_extractor._load_playwright",
            return_value=(sync_playwright, TimeoutError),
        ),
        patch("app.scrapers.workday_link_extractor.random.uniform", return_value=2),
        patch("app.scrapers.workday_link_extractor.time.sleep") as sleep,
    ):
        result = get_workday_link("https://example.com/jobs/123")

    assert result == "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123"
    sleep.assert_called_once_with(2)
    page.get_by_role.assert_not_called()
    browser.close.assert_called_once()
