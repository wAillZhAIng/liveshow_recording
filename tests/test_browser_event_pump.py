import threading
import time

from playwright.sync_api import sync_playwright
from whatnot_monitor.config import Settings
from whatnot_monitor.runtime import MonitorRuntime
from whatnot_monitor.store import Store
from whatnot_monitor.whatnot import WhatnotBrowser


def test_owner_wait_dispatches_live_browser_bindings(tmp_path):
    """A real browser proves idle waits keep delivering recorder-style writes."""
    with sync_playwright() as manager:
        browser = manager.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context()
        page = context.new_page()
        writes = []
        page.expose_binding("writeChunk", lambda _source, value: writes.append(value))
        page.evaluate("() => {window.timer = setInterval(() => window.writeChunk(Date.now()), 50)}")
        store = Store(tmp_path / "monitor.sqlite3")
        runtime = MonitorRuntime(store, Settings())
        runtime._browser = WhatnotBrowser(context, browser=browser)
        try:
            before = len(writes)
            # Deliberately demonstrate the original defect: browser callbacks
            # do not run on another Python thread during Event.wait.
            threading.Event().wait(0.25)
            assert len(writes) == before
            runtime._wait_with_browser_events(0.35)
            assert len(writes) >= before + 4
            page.evaluate("() => clearInterval(window.timer)")
        finally:
            runtime._postprocess.shutdown(wait=True)
            store.close()
            browser.close()


def test_owner_wait_wakes_immediately_for_login_or_shutdown(tmp_path):
    store = Store(tmp_path / "monitor.sqlite3")
    runtime = MonitorRuntime(store, Settings())
    try:
        runtime._login_requested.set()
        started = time.monotonic()
        runtime._wait_with_browser_events(10, wake_for_requests=True)
        assert time.monotonic() - started < 0.5
        runtime._login_requested.clear()
        runtime._stop.set()
        started = time.monotonic()
        runtime._wait_with_browser_events(10)
        assert time.monotonic() - started < 0.5
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()
