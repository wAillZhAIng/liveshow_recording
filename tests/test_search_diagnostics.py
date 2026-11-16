import traceback

import pytest
from playwright.sync_api import Error as PlaywrightError
from test_whatnot import Context, FakeLocator, VerificationPage

from whatnot_monitor.bounded_browser_read import BrowserReadTimeout
from whatnot_monitor.whatnot import WhatnotBrowser, WhatnotPageStateUnknown


class DiagnosticPage(VerificationPage):
    def evaluate(self, script):
        if "page_kind: pageKind" in script:
            return {"page_kind": "search", "platform_host": True, "search_mode": "category",
                    "inputs_total": 2, "inputs_visible": 1, "inputs_enabled": 1,
                    "inputs_readonly": 0, "inputs_editable": 1, "password": "SECRET_VALUE"}
        return super().evaluate(script)


class TimeoutSearch(FakeLocator):
    def fill(self, value, **_kwargs):
        raise PlaywrightError("SECRET_VALUE should never be included")


class FillTimeoutPage(DiagnosticPage):
    def locator(self, selector):
        return TimeoutSearch(self, "searchbox", self.searchbox_visible)


def test_fill_error_reports_phase_and_only_whitelisted_diagnostics():
    page = FillTimeoutPage("Whatnot search")
    with pytest.raises(RuntimeError) as caught:
        WhatnotBrowser(Context(page)).scan_keyword("needoh")
    message = str(caught.value)
    assert "填写关键词" in message and "inputs_editable" in message
    assert "SECRET_VALUE" not in message and "password" not in message
    assert "SECRET_VALUE" not in "".join(traceback.format_exception(caught.value))
    assert not page.closed


@pytest.mark.parametrize("fail_stage, expected", [("keyword =>", "等待搜索 URL"),
                                                  ("searchVertical') === 'LIVESTREAM'", "等待 Shows 跳转")])
def test_wait_failure_reports_precise_phase(fail_stage, expected):
    class Page(DiagnosticPage):
        def wait_for_function(self, script, *, timeout, arg=None):
            if fail_stage in script:
                raise PlaywrightError("SECRET_VALUE")
            return super().wait_for_function(script, timeout=timeout, arg=arg)
    page = Page("Whatnot search")
    with pytest.raises(RuntimeError) as caught:
        WhatnotBrowser(Context(page)).scan_keyword("needoh")
    assert expected in str(caught.value)
    assert "SECRET_VALUE" not in str(caught.value)


def test_unreadable_after_fill_timeout_blocks_any_next_navigation(monkeypatch):
    import whatnot_monitor.whatnot as module
    page = FillTimeoutPage("Whatnot search")
    context = Context(page)
    browser = WhatnotBrowser(context)
    def stalled(*_args, **_kwargs):
        raise BrowserReadTimeout("synthetic")
    monkeypatch.setattr(module, "bounded_evaluate", stalled)
    with pytest.raises(WhatnotPageStateUnknown):
        browser.scan_keyword("needoh")
    assert browser._uncertain_page is page
    actions = list(page.actions)
    with pytest.raises(WhatnotPageStateUnknown):
        browser.scan_keyword("other")
    assert page.actions == actions and not page.closed


def test_failed_post_timeout_inspection_retains_unknown_page(monkeypatch):
    page = FillTimeoutPage("Whatnot search")
    browser = WhatnotBrowser(Context(page))
    def broken(_page):
        raise PlaywrightError("document became unavailable")
    monkeypatch.setattr(browser, "_inspect_document", broken)
    with pytest.raises(WhatnotPageStateUnknown):
        browser.scan_keyword("needoh")
    assert browser._uncertain_page is page
    assert not page.closed


def test_raw_title_body_and_url_never_enter_full_traceback(monkeypatch):
    page = DiagnosticPage("Whatnot search")
    browser = WhatnotBrowser(Context(page))
    monkeypatch.setattr(browser, "_collect_search_results", lambda *_args: {
        "rows": [], "candidateLinks": 0, "emptyState": False,
        "href": "https://www.whatnot.com/search?q=SECRET_VALUE",
        "title": "SECRET_VALUE", "body": "PRIVATE_BODY SECRET_VALUE",
    })
    keyword = "SECRET_VALUE"
    with pytest.raises(Exception) as caught:
        browser.scan_keyword(keyword)
    full = "".join(traceback.format_exception(caught.value))
    assert "SECRET_VALUE" not in full and "PRIVATE_BODY" not in full
    assert "whatnot.com/search?q=" not in full


def test_malicious_diagnostic_fields_are_typed_and_redacted():
    class Page(FillTimeoutPage):
        def evaluate(self, script):
            if "page_kind: pageKind" in script:
                return {"page_kind": "search", "platform_host": True, "search_mode": "SECRET_VALUE",
                        "inputs_total": "SECRET_VALUE", "inputs_visible": -1,
                        "inputs_enabled": True, "inputs_readonly": 10001, "inputs_editable": 1}
            return super().evaluate(script)
    with pytest.raises(RuntimeError) as caught:
        WhatnotBrowser(Context(Page("normal"))).scan_keyword("private")
    full = "".join(traceback.format_exception(caught.value))
    assert "SECRET_VALUE" not in full
    assert '"inputs_total": null' in full and '"inputs_visible": null' in full
    assert '"inputs_enabled": null' in full and '"inputs_readonly": null' in full
    assert '"inputs_editable": 1' in full


@pytest.mark.parametrize("kind, host", [("search", False), ("other", True), ("SECRET_VALUE", True)])
def test_unknown_origin_or_path_quarantines_page_without_leaking(kind, host):
    class Page(FillTimeoutPage):
        def evaluate(self, script):
            if "page_kind: pageKind" in script:
                return {"page_kind": kind, "platform_host": host}
            return super().evaluate(script)
    page = Page("Whatnot search")
    browser = WhatnotBrowser(Context(page))
    with pytest.raises(WhatnotPageStateUnknown) as caught:
        browser.scan_keyword("private")
    assert browser._uncertain_page is page and not page.closed
    assert "SECRET_VALUE" not in "".join(traceback.format_exception(caught.value))


def test_non_playwright_diagnostic_failure_quarantines_page(monkeypatch):
    import whatnot_monitor.whatnot as module
    page = FillTimeoutPage("Whatnot search")
    browser = WhatnotBrowser(Context(page))
    def diagnostic_fail(target, expression, **_kwargs):
        if "page_kind: pageKind" in expression:
            raise ValueError("SECRET_VALUE")
        return target.evaluate(expression)
    monkeypatch.setattr(module, "bounded_evaluate", diagnostic_fail)
    with pytest.raises(WhatnotPageStateUnknown) as caught:
        browser.scan_keyword("private")
    assert browser._uncertain_page is page and not page.closed
    assert "SECRET_VALUE" not in "".join(traceback.format_exception(caught.value))


def test_failure_inspection_verification_hides_prior_error_chain():
    from whatnot_monitor.whatnot import WhatnotVerificationRequired
    class Page(FillTimeoutPage):
        def locator(self, selector):
            class Blocked(TimeoutSearch):
                def fill(self, value, **kwargs):
                    self.page.body = "正在进行安全验证。本网站使用安全服务防护恶意自动程序。"
                    raise PlaywrightError("SECRET_VALUE")
            return Blocked(self, "searchbox", True)
    page = Page("Whatnot search")
    keyword = "SECRET_VALUE"
    with pytest.raises(WhatnotVerificationRequired) as caught:
        WhatnotBrowser(Context(page)).scan_keyword(keyword)
    assert "SECRET_VALUE" not in "".join(traceback.format_exception(caught.value))


def test_missing_shows_error_hides_page_contents_and_full_url(monkeypatch):
    from whatnot_monitor.whatnot import WhatnotPageChanged
    class Page(DiagnosticPage):
        def evaluate(self, script):
            if "new URL(location.href).searchParams.get('searchVertical')" in script:
                return False
            if "document.title" in script and "const anchors" not in script:
                return {"title": "SECRET_VALUE", "body": "PRIVATE_BODY"}
            return super().evaluate(script)
    page = Page("normal")
    browser = WhatnotBrowser(Context(page))
    monkeypatch.setattr(browser, "_search_control", lambda *_args: None)
    keyword = "SECRET_VALUE"
    with pytest.raises(WhatnotPageChanged) as caught:
        browser.scan_keyword(keyword)
    full = "".join(traceback.format_exception(caught.value))
    assert "SECRET_VALUE" not in full and "PRIVATE_BODY" not in full


def test_non_playwright_inspection_failure_is_unknown_and_redacted(monkeypatch):
    page = FillTimeoutPage("Whatnot search")
    browser = WhatnotBrowser(Context(page))
    def broken(_page):
        raise ValueError("SECRET_VALUE")
    monkeypatch.setattr(browser, "_inspect_document", broken)
    with pytest.raises(WhatnotPageStateUnknown) as caught:
        browser.scan_keyword("private")
    assert browser._uncertain_page is page and not page.closed
    assert "SECRET_VALUE" not in "".join(traceback.format_exception(caught.value))


def test_runtime_scan_error_events_hide_keyword_and_untrusted_exception(tmp_path, monkeypatch):
    from test_runtime import make_runtime
    keyword = "SECRET_SEARCH_TERM"
    store, runtime = make_runtime(tmp_path, monkeypatch, {keyword: RuntimeError("PRIVATE_BODY SECRET_URL")})
    store.upsert_keyword(keyword)
    runtime._wait_with_browser_events = lambda *_args, **_kwargs: None
    try:
        runtime.scan_once()
        saved = store.list_keywords()[0]
        assert saved.phrase == keyword and saved.scan_status == "ERROR"
        assert "PRIVATE_BODY" not in saved.last_result and "SECRET_URL" not in saved.last_result
        warnings = [e.message for e in store.recent_events() if e.level in {"WARNING", "ERROR"}]
        notices = [message for level, message in list(runtime.events.queue) if level in {"warning", "error"}]
        assert warnings and notices
        assert all(keyword not in text and "PRIVATE_BODY" not in text and "SECRET_URL" not in text
                   for text in warnings + notices)
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()
