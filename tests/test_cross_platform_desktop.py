from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError

from whatnot_monitor.app import MonitorApp
from whatnot_monitor.runtime import _search_failure_detail
from whatnot_monitor.whatnot import WhatnotBrowser, WhatnotBrowserProfileInUse
import whatnot_monitor.whatnot as whatnot


@pytest.mark.parametrize("platform,opener", [("darwin", "open"), ("linux", "xdg-open")])
def test_open_file_uses_platform_opener(tmp_path, monkeypatch, platform, opener):
    import subprocess
    import sys

    filename = tmp_path / "audio file.webm"
    filename.touch()
    popen = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", platform)
        MonitorApp._open_path(filename)
    popen.assert_called_once_with([opener, str(filename)])


def test_non_windows_falls_back_to_playwright_chromium_only_for_missing_browser(monkeypatch):
    import playwright.sync_api

    context = Mock()
    chromium = Mock()
    chromium.launch_persistent_context.side_effect = [
        PlaywrightError("Chromium distribution 'chrome' is not found at /opt/google/chrome"),
        context,
    ]
    manager = Mock(chromium=chromium)
    monkeypatch.setattr(whatnot, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: manager))
    monkeypatch.setattr(WhatnotBrowser, "__init__", lambda self, ctx: None)
    result, returned_manager = WhatnotBrowser.launch_persistent("/tmp/test-profile", "chrome")
    assert isinstance(result, WhatnotBrowser)
    assert returned_manager is manager
    assert chromium.launch_persistent_context.call_args_list[0].kwargs["channel"] == "chrome"
    assert "channel" not in chromium.launch_persistent_context.call_args_list[1].kwargs
    manager.stop.assert_not_called()


def test_non_windows_profile_lock_does_not_try_a_second_browser(monkeypatch):
    import playwright.sync_api

    chromium = Mock()
    chromium.launch_persistent_context.side_effect = PlaywrightError("profile already in use")
    manager = Mock(chromium=chromium)
    monkeypatch.setattr(whatnot, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: manager))
    with pytest.raises(PlaywrightError, match="profile already in use"):
        WhatnotBrowser.launch_persistent("/tmp/test-profile", "chrome")
    assert chromium.launch_persistent_context.call_count == 1
    manager.stop.assert_called_once()


def test_chrome_profile_in_use_explains_how_to_resume_without_leaking_logs(monkeypatch):
    import playwright.sync_api

    chromium = Mock()
    chromium.launch_persistent_context.side_effect = PlaywrightError(
        "Failed to create a ProcessSingleton for your profile directory.\n"
        "Call log: /private/profile/SingletonLock"
    )
    manager = Mock(chromium=chromium)
    monkeypatch.setattr(whatnot, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: manager))

    with pytest.raises(WhatnotBrowserProfileInUse) as failure:
        WhatnotBrowser.launch_persistent("/private/profile", "chrome")

    detail = _search_failure_detail(failure.value)
    assert "点击启动" in detail
    assert "/private/profile" not in detail
    chromium.launch_persistent_context.assert_called_once()
    manager.stop.assert_called_once()


def test_failed_chromium_fallback_stops_playwright(monkeypatch):
    import playwright.sync_api

    chromium = Mock()
    chromium.launch_persistent_context.side_effect = [
        PlaywrightError("Chromium distribution 'chrome' is not found at /opt/google/chrome"),
        PlaywrightError("Executable doesn't exist; run playwright install chromium"),
    ]
    manager = Mock(chromium=chromium)
    monkeypatch.setattr(whatnot, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: manager))
    with pytest.raises(PlaywrightError, match="playwright install chromium"):
        WhatnotBrowser.launch_persistent("/tmp/test-profile", "chrome")
    manager.stop.assert_called_once()
