from __future__ import annotations

import unittest

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from whatnot_monitor.whatnot import _AUDIENCE_PAGE_SCRIPT, _viewer_observation


class WhatnotAudienceDomTests(unittest.TestCase):
    def test_reads_explicit_live_viewer_element_not_other_page_counts(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                self.skipTest(f"Playwright Chromium unavailable: {exc}")
            try:
                page = browser.new_page()
                page.set_content(
                    """<main>
                      <p>2,500 total views and 80 items sold</p>
                      <div aria-label="Live viewers"><span>99</span></div>
                    </main>"""
                )
                result = page.evaluate(_AUDIENCE_PAGE_SCRIPT)
                self.assertEqual(
                    _viewer_observation(result["audienceCandidates"])[0], 99
                )
            finally:
                browser.close()

    def test_live_title_and_explicit_end_take_precedence(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                self.skipTest(f"Playwright Chromium unavailable: {exc}")
            try:
                page = browser.new_page()
                page.set_content("<title>seller123 is live · Whatnot</title><main>Welcome</main>")
                result = page.evaluate(_AUDIENCE_PAGE_SCRIPT)
                self.assertTrue(result["live"])
                page.set_content("<title>seller123 is live · Whatnot</title><main>Show has ended</main>")
                result = page.evaluate(_AUDIENCE_PAGE_SCRIPT)
                self.assertTrue(result["ended"])
                self.assertFalse(result["live"])
                page.set_content("<title>seller123 on Whatnot</title><main>Welcome</main>")
                self.assertFalse(page.evaluate(_AUDIENCE_PAGE_SCRIPT)["live"])
            finally:
                browser.close()

    def test_unlabeled_page_counts_remain_unknown(self) -> None:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                self.skipTest(f"Playwright Chromium unavailable: {exc}")
            try:
                page = browser.new_page()
                page.set_content(
                    """<main>
                      <p>2,500 total views</p>
                      <p>80 items sold</p>
                      <span>101</span>
                    </main>"""
                )
                result = page.evaluate(_AUDIENCE_PAGE_SCRIPT)
                self.assertEqual(
                    _viewer_observation(result["audienceCandidates"]), (None, None)
                )
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
