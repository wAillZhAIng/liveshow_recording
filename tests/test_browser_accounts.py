import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from whatnot_monitor import accounts, browser_pool
from whatnot_monitor.accounts import AccountLibrary
from whatnot_monitor.browser_pool import BrowserPool
from whatnot_monitor.whatnot import WhatnotPageStateUnknown, WhatnotVerificationRequired


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "ACCOUNT_PROFILES", tmp_path / "profiles")
    return AccountLibrary(tmp_path / "accounts.sqlite3")


def test_readiness_is_explicit_and_profiles_are_isolated(library):
    first = library.add("First")
    second = library.add("Second")
    assert first.profile != second.profile
    with pytest.raises(ValueError, match="先在该账号"):
        library.set_ready(first, True)
    first.profile.mkdir(parents=True)
    (first.profile / "Local State").touch()
    library.set_ready(first, True)
    assert [(row.name, row.ready) for row in library.list()] == [
        ("First", True), ("Second", False),
    ]
    library.set_ready(library.list()[0], False)
    assert not any(row.ready for row in library.list())


def test_recording_limits_migrate_old_accounts_and_save_independently(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, ready INTEGER DEFAULT 0)")
        db.execute("INSERT INTO accounts VALUES ('old', 'Existing', 1)")
    library = AccountLibrary(path)
    [first] = library.list()
    second = library.add("Second")
    assert first.ready and first.recording_limit == second.recording_limit == 4
    for value in (0, 101, True, 2.5):
        with pytest.raises(ValueError, match="整数"):
            library.set_recording_limit(first, value)
    library.set_recording_limit(first, 2)
    library.set_keyword(first, 17)
    assert [(account.ready, account.recording_limit) for account in AccountLibrary(path).list()] == [
        (True, 2), (False, 4),
    ]
    assert [account.keyword_id for account in AccountLibrary(path).list()] == [17, None]


def test_platform_accounts_keep_separate_settings_and_open_their_login_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "ACCOUNT_DB", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(accounts, "ACCOUNT_PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(accounts.sys, "platform", "darwin")
    is_dir = accounts.Path.is_dir
    monkeypatch.setattr(accounts.Path, "is_dir", lambda path: path == accounts.Path("/Applications/Google Chrome.app") or is_dir(path))
    launch = Mock()
    monkeypatch.setattr(accounts.subprocess, "run", launch)

    expected = [
        ("whatnot", "https://www.whatnot.com/login"),
        ("tiktok", "https://www.tiktok.com/login"),
        ("douyin", "https://www.douyin.com/"),
        ("xiaohongshu", "https://www.xiaohongshu.com/login"),
        ("bilibili", "https://passport.bilibili.com/login"),
    ]
    paths = set()
    for index, (platform, url) in enumerate(expected, 1):
        library = AccountLibrary(platform=platform)
        assert library.list() == []
        account = library.add("Shared nickname")
        library.set_keyword(account, index)
        library.set_recording_limit(account, index)
        accounts.open_manual_login(account)
        command = launch.call_args.args[0]
        assert command[-1] == url
        assert f"--user-data-dir={account.profile}" in command
        paths.add(account.profile)
    assert len(paths) == len(expected)
    for index, (platform, _) in enumerate(expected, 1):
        [account] = AccountLibrary(platform=platform).list()
        assert (account.keyword_id, account.recording_limit) == (index, index)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_manual_login_uses_installed_chrome_and_waits_for_its_exit(library, monkeypatch, platform):
    account = library.add("Recording account")
    executable = "installed Chrome"
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr(accounts.sys, "platform", platform)
    monkeypatch.setattr(accounts, "_manual_browsers", {})
    monkeypatch.setattr(accounts.WhatnotBrowser, "_find_installed_browser", lambda channel: executable)
    monkeypatch.setattr(accounts.subprocess, "Popen", launch)

    accounts.open_manual_login(account)
    command = launch.call_args.args[0]
    assert command[0] == executable
    assert f"--user-data-dir={account.profile}" in command
    assert command[-1] == accounts.PLATFORMS["whatnot"][1]
    (account.profile / "Local State").touch()
    with pytest.raises(RuntimeError, match="仍在运行"):
        library.set_ready(account, True)
    process.poll.return_value = 0
    library.set_ready(account, True)
    assert library.list()[0].ready


def test_disconnected_challenge_reports_account_without_reopening(library, monkeypatch):
    account = library.add("First")
    monkeypatch.setattr(browser_pool, "sync_playwright", lambda: Mock(start=Mock()))
    pool = BrowserPool([account], headless=False)
    member = Mock()
    member.is_usable.return_value = False
    member._uncertain_page = None
    member._blocked_pages = {object(): WhatnotVerificationRequired("challenge")}
    member._verification_page = None
    pool._members[account.id] = member
    try:
        with pytest.raises(WhatnotPageStateUnknown, match="账号「First」.*已关闭或断开") as error:
            pool.scan_keyword("test", keyword_id=1)
        from whatnot_monitor.runtime import _search_failure_detail
        assert "账号「First」" in _search_failure_detail(error.value)
        member.scan_keyword.assert_not_called()
    finally:
        pool.close()


def test_capacity_reuses_free_slots_and_preserves_account_ownership(library, monkeypatch):
    for name in ("First", "Second"):
        account = library.add(name)
        account.profile.mkdir(parents=True)
        (account.profile / "Local State").touch()
        library.set_ready(account, True)
    manager = Mock()
    monkeypatch.setattr(browser_pool, "sync_playwright", lambda: Mock(start=lambda: manager))
    members = [Mock(context=object()), Mock(context=object())]
    launch = Mock(side_effect=[(member, manager) for member in members])
    monkeypatch.setattr(browser_pool.WhatnotBrowser, "launch_persistent", launch)

    first, second = library.list()
    pool = BrowserPool([first, second], headless=False, load_accounts=library.list)
    try:
        assert pool.capacity == 8
        assert pool.room_browser({}) is members[0]
        pages = [SimpleNamespace(context=members[0].context) for _ in range(4)]
        active = {str(i): (object(), page) for i, page in enumerate(pages)}
        assert pool.room_browser(active) is members[1]
        assert pool.account_name(pages[0]) == "First"
        pool.inspect_page(pages[0])
        members[0].inspect_page.assert_called_once_with(pages[0])
        assert all(call.kwargs["manager"] is manager for call in launch.call_args_list)

        members[0]._check_pending_verification.side_effect = WhatnotVerificationRequired("challenge")
        with pytest.raises(WhatnotVerificationRequired):
            pool.room_browser(active)
        assert launch.call_count == 2

        members[0]._check_pending_verification.side_effect = None
        library.set_recording_limit(first, 1)
        library.set_recording_limit(second, 3)
        pool.refresh_account_settings()
        assert pool.capacity == 4
        assert pool.available_slots(active) == 3  # An over-limit account does not use another account's slots.
        assert not pool.has_slot(members[0], active)
        assert pool.room_browser(active) is members[1]
        library.set_recording_limit(first, 4)
        pool.refresh_account_settings()
        del active["0"]
        assert pool.room_browser(active) is members[0]

        library.set_keyword(first, 17)
        library.set_keyword(second, 23)
        pool.refresh_account_settings()
        assert pool.room_browser(active, keyword_ids={17}) is members[0]
        assert pool.room_browser(active, keyword_ids={23}) is members[1]
        assert pool.room_browser(active, keyword_ids={99}) is None
        assert pool.room_browser(active, keyword_ids=set()) is None
        assert not pool.has_slot(members[0], active, keyword_ids={23})
        pool.scan_keyword("second keyword", keyword_id=23)
        members[1].scan_keyword.assert_called_once_with("second keyword")
        members[0].scan_keyword.assert_not_called()
    finally:
        pool.close()
    assert all(member.close.call_count == 1 for member in members)
    manager.stop.assert_called_once()


def test_real_chrome_profiles_do_not_share_login_state(library):
    first = library.add("First")
    second = library.add("Second")
    with sync_playwright() as manager:
        members = []
        try:
            for account in (first, second):
                browser, owner = browser_pool.WhatnotBrowser.launch_persistent(
                    str(account.profile), "chrome", headless=True, manager=manager,
                    allow_fallback=False,
                )
                members.append(browser)
                assert owner is manager
                assert browser.context.cookies("https://www.whatnot.com") == []
                browser.context.add_cookies([{
                    "name": "test-isolated", "value": account.name,
                    "url": "https://www.whatnot.com",
                }])
            assert [member.context.cookies("https://www.whatnot.com")[0]["value"]
                    for member in members] == ["First", "Second"]
        finally:
            for member in reversed(members):
                member.close()
