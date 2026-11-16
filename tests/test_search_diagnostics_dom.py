from playwright.sync_api import sync_playwright
from whatnot_monitor.bounded_browser_read import bounded_evaluate
from whatnot_monitor.whatnot import _SEARCH_ERROR_DIAGNOSTICS


def test_diagnostics_collect_counts_without_input_values_or_page_text():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, timeout=10000)
        try:
            page = browser.new_page()
            page.set_content(
                "<main>PRIVATE_BODY"
                "<input type='search' value='SECRET_VALUE'>"
                "<input type='search' disabled>"
                "<input type='search' readonly style='display:none'>"
                "</main>"
            )
            result = bounded_evaluate(page, _SEARCH_ERROR_DIAGNOSTICS, timeout_seconds=3)
            assert result["inputs_total"] == 3
            assert result["inputs_visible"] == 2
            assert result["inputs_enabled"] == 2
            assert result["inputs_readonly"] == 1
            assert result["inputs_editable"] == 1
            assert "SECRET_VALUE" not in str(result) and "PRIVATE_BODY" not in str(result)
        finally:
            browser.close()
