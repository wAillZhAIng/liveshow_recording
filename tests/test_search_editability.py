from playwright.sync_api import sync_playwright
from whatnot_monitor.whatnot import WhatnotBrowser


def test_search_locator_tracks_editable_visible_input_when_dom_changes():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, timeout=10_000)
        try:
            page = browser.new_page()
            page.set_content(
                '<input type="search" aria-label="Search Whatnot" disabled>'
                '<input type="search" aria-label="Search Whatnot" id="usable">'
            )
            client = WhatnotBrowser(context=None)
            search = client._search_box(page)
            search.fill("needoh", timeout=1500)
            assert page.locator("#usable").input_value() == "needoh"
            page.set_content(
                '<input type="search" aria-label="Search Whatnot" id="old">'
                '<input type="search" aria-label="Search Whatnot" id="new" disabled>'
            )
            search = client._search_box(page)
            page.locator("#old").evaluate("e => e.disabled = true")
            page.locator("#new").evaluate("e => e.disabled = false")
            search.fill("again", timeout=1500)
            assert page.locator("#new").input_value() == "again"
        finally:
            browser.close()
