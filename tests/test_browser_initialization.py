"""An unresponsive retained tab must not trigger repeated automatic attachment."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from test_runtime import make_runtime
from whatnot_monitor.whatnot import WhatnotBrowser, WhatnotPageStateUnknown


def test_cdp_initialization_timeout_is_unknown_and_redacts_protocol_details():
    chromium = Mock()
    chromium.connect_over_cdp.side_effect = PlaywrightTimeoutError(
        "Timeout; ws://127.0.0.1:1234/devtools/browser/private-id?token=secret "
        "page content must not be displayed"
    )
    manager = SimpleNamespace(chromium=chromium)

    with pytest.raises(WhatnotPageStateUnknown) as caught:
        WhatnotBrowser._connect_over_cdp("http://127.0.0.1:1234", manager)

    assert "初始化超时" in str(caught.value)
    assert "private-id" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "page content" not in str(caught.value)
    chromium.connect_over_cdp.assert_called_once_with(
        "http://127.0.0.1:1234", timeout=20_000
    )
    assert chromium.method_calls == [
        ("connect_over_cdp", ("http://127.0.0.1:1234",), {"timeout": 20_000})
    ]


def test_newly_launched_chrome_is_retained_when_initialization_is_unknown(tmp_path, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    chromium = Mock()
    chromium.connect_over_cdp.side_effect = PlaywrightTimeoutError("unresponsive tab")
    monkeypatch.setattr(WhatnotBrowser, "_find_installed_browser", staticmethod(lambda _: "chrome.exe"))
    monkeypatch.setattr(WhatnotBrowser, "_existing_profile_endpoint", classmethod(lambda *_: None))
    monkeypatch.setattr(WhatnotBrowser, "_free_local_port", staticmethod(lambda: 1234))
    monkeypatch.setattr(WhatnotBrowser, "_wait_for_cdp", staticmethod(lambda *_: None))
    launch = Mock(return_value=process)
    monkeypatch.setattr("whatnot_monitor.whatnot.subprocess.Popen", launch)

    with pytest.raises(WhatnotPageStateUnknown):
        WhatnotBrowser._launch_windows_chrome(
            str(tmp_path / "profile"), "chrome", SimpleNamespace(chromium=chromium)
        )

    launch.assert_called_once()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_not_called()


def test_cdp_timeout_pauses_scan_without_retrying_or_starting_rooms(tmp_path, monkeypatch):
    store, runtime = make_runtime(tmp_path, monkeypatch, {"all": []})
    store.upsert_keyword("all")
    attach = Mock(side_effect=WhatnotPageStateUnknown("浏览器调试连接初始化超时"))
    monkeypatch.setattr(runtime, "_ensure_browser", attach)
    try:
        with pytest.raises(WhatnotPageStateUnknown):
            runtime.scan_once()
        assert runtime._paused.is_set()
        assert runtime._verification_required.is_set()
        assert runtime.scan_status["state"] == "page_state_unknown"
        assert not runtime._scan_requested.is_set()
        assert not runtime._recorders
        # Automatic or queued subsequent scan cannot reconnect until a person
        # explicitly requests a new check via the existing Start path.
        runtime.scan_once()
        assert attach.call_count == 1
    finally:
        runtime._postprocess.shutdown(wait=True)
        store.close()
