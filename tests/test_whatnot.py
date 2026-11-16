import json
import os.path
import subprocess
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError
from whatnot_monitor.whatnot import (
    WhatnotBrowser,
    WhatnotPageChanged,
    WhatnotPageStateUnknown,
    WhatnotVerificationRequired,
    _show_identity,
    is_live_viewer_badge,
    parse_labeled_live_viewers,
    parse_platform_datetime,
    parse_viewers,
    room_name_from_card,
)


class FakeLocator:
    def __init__(self, page, kind, visible):
        self.page = page
        self.kind = kind
        self.visible = visible

    def count(self):
        return int(self.visible)

    def nth(self, _index):
        return self

    def is_visible(self):
        return self.visible

    def fill(self, value, **_kwargs):
        self.page.actions.append(("fill", value))
        self.page.keyword = value

    def press(self, key):
        self.page.actions.append(("press", key))
        self.page.submitted = True
        self.page.url = (
            "https://www.whatnot.com/search?query=" + self.page.keyword
        )

    def click(self):
        self.page.actions.append(("click", self.kind))
        if self.kind == "shows":
            self.page.selected_shows = True
            self.page.url += "&searchVertical=LIVESTREAM"
        elif self.kind == "show_all":
            self.page.selected_all = True


class VerificationPage:
    def __init__(self, body="", *, result=None, searchbox_visible=True):
        self.body = body
        self.result = result or {
            "title": "Live Shows · Whatnot",
            "body": body,
            "rows": [],
            "candidateLinks": 0,
            "emptyState": False,
        }
        self.searchbox_visible = searchbox_visible
        self.closed = False
        self.url = "about:blank"
        self.actions = []
        self.keyword = ""
        self.submitted = False
        self.selected_shows = False
        self.selected_all = False

    def goto(self, url, **_kwargs):
        self.url = url
        self.actions.append(("goto", url))

    def wait_for_timeout(self, _milliseconds):
        pass

    def wait_for_function(self, script, *, timeout, arg=None):
        if "keyword =>" in script:
            assert arg == self.keyword
            assert self.submitted
            return
        if "new URL(location.href).searchParams.get('searchVertical')" in script:
            assert self.selected_shows
            return
        if "const anchors" in script:
            assert self.selected_all or self.result.get("emptyState")
        self.wait_timeout = timeout

    def get_by_role(self, role, *, name=None):
        if role == "searchbox":
            return FakeLocator(self, "searchbox", self.searchbox_visible)
        if role == "tab" and self.submitted and name and name.fullmatch("Shows"):
            return FakeLocator(self, "shows", not self.selected_shows)
        if (
            role in {"link", "button"}
            and self.selected_shows
            and name
            and name.fullmatch("Show all")
        ):
            return FakeLocator(self, "show_all", not self.selected_all)
        return FakeLocator(self, "missing", False)

    def get_by_text(self, name):
        if self.submitted and name.fullmatch("Shows"):
            return FakeLocator(self, "shows", not self.selected_shows)
        if self.selected_shows and name.fullmatch("Show all"):
            return FakeLocator(self, "show_all", not self.selected_all)
        return FakeLocator(self, "missing", False)

    def locator(self, selector):
        if selector.startswith("main input#"):
            return FakeLocator(self, "missing", False)
        return FakeLocator(self, "searchbox", self.searchbox_visible)

    def evaluate(self, script):
        if "document.title" in script and "const anchors" not in script:
            if self.submitted:
                body = "Top\nProducts\nShows\nUsers"
                if self.selected_shows:
                    body = "Live Shows\nShow all"
            else:
                body = self.body
            return {"title": "Whatnot", "body": body}
        if "searchVertical" in script:
            return self.selected_shows
        return {**self.result, "href": self.url}

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class Context:
    def __init__(self, page):
        self.page = page
        self.pages = []

    def new_page(self):
        self.pages.append(self.page)
        return self.page


class RoomPage:
    def __init__(self, *, body="", ready=True):
        self.body = body
        self.ready = ready
        self.url = "about:blank"
        self.closed = False
        self.wait_args = None
        self.init_script = None

    def add_init_script(self, script):
        self.init_script = script

    def goto(self, url, **_kwargs):
        self.url = url

    def wait_for_timeout(self, _milliseconds):
        pass

    def get_by_role(self, _role, *, name=None):
        return FakeLocator(self, "missing", False)

    def evaluate(self, _script, _arg=None):
        if "audibleElement:" in _script:
            return {"audibleElement": True}
        if "({requireAudio, requireVideo})" in _script:
            return self.ready
        if "element.defaultMuted = false" in _script:
            return None
        return {
            "title": "Whatnot live show",
            "body": self.body,
            "media": [
                {
                    "tag": "video",
                    "paused": False,
                    "ended": False,
                    "readyState": 4,
                    "currentTime": 1,
                }
            ],
        }

    def wait_for_function(self, _script, *, arg, timeout):
        self.wait_args = (arg, timeout)
        if not self.ready:
            raise PlaywrightError("media readiness timeout")

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "body",
    [
        "正在进行安全验证。本网站使用安全服务防护恶意自动程序。",
        "Please verify you are human before continuing.",
    ],
)
def test_verification_page_raises_specific_error_and_keeps_page_open(body):
    page = VerificationPage(body)
    with pytest.raises(WhatnotVerificationRequired):
        WhatnotBrowser(Context(page)).scan_keyword("Pokemon cards")

    assert not page.closed


def test_login_page_reports_security_verification_without_closing_page():
    page = VerificationPage("正在进行安全验证。本网站使用安全服务防护恶意自动程序。")
    with pytest.raises(WhatnotVerificationRequired, match="安全验证"):
        WhatnotBrowser(Context(page)).open_login()

    assert not page.closed


def test_login_reuses_existing_whatnot_page_instead_of_opening_duplicate_tab():
    page = VerificationPage("正在进行安全验证。本网站使用安全服务防护恶意自动程序。")
    page.url = "https://www.whatnot.com/"
    context = Context(page)
    context.pages.append(page)
    browser = WhatnotBrowser(context)

    with pytest.raises(WhatnotVerificationRequired):
        browser.open_login()

    assert len(context.pages) == 1
    assert page.actions == []
    assert not page.closed


def test_delayed_challenge_text_is_classified_before_missing_search_box():
    class DelayedChallengePage(VerificationPage):
        def __init__(self):
            super().__init__("Loading…", searchbox_visible=False)

        def wait_for_timeout(self, milliseconds):
            if milliseconds >= 1_000:
                self.body = "正在进行安全验证。本网站使用安全服务防护恶意自动程序。"

    page = DelayedChallengePage()

    with pytest.raises(WhatnotVerificationRequired, match="安全验证"):
        WhatnotBrowser(Context(page)).scan_keyword("needoh")

    assert not page.submitted
    assert not page.closed


def test_search_url_challenge_is_checked_before_navigation_and_can_resume():
    page = VerificationPage(
        "请验证您是真人",
        result={
            "title": "No live shows · Whatnot",
            "body": "No live shows",
            "rows": [],
            "candidateLinks": 0,
            "emptyState": True,
        },
    )
    page.url = "https://www.whatnot.com/search?query=previous"
    context = Context(page)
    context.pages.append(page)
    browser = WhatnotBrowser(context)

    for _ in range(2):
        with pytest.raises(WhatnotVerificationRequired):
            browser.scan_keyword("Pokemon cards")

    assert page.actions == []
    assert len(context.pages) == 1
    assert not page.closed

    page.body = "Whatnot search"
    assert browser.scan_keyword("Pokemon cards") == []
    assert page.actions[0] == ("goto", "https://www.whatnot.com/")
    assert len(context.pages) == 1


def test_pending_inspection_challenge_blocks_search_before_opening_or_navigation():
    challenge = InspectionPage(
        "https://www.whatnot.com/live/room", body="请验证您是真人"
    )
    context = InspectionContext(pages=[challenge])
    browser = WhatnotBrowser(context)

    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_page(challenge)
    with pytest.raises(WhatnotVerificationRequired):
        browser.scan_keyword("Pokemon cards")

    assert context.created == 0
    assert challenge.navigations == []
    assert not challenge.closed


def test_search_enters_keyword_selects_live_shows_and_show_all():
    page = VerificationPage(
        result={
            "title": "Live Shows · Whatnot",
            "body": "Live Shows",
            "rows": [
                {
                    "href": "https://www.whatnot.com/show/show-1",
                    "sellerHref": "https://www.whatnot.com/user/seller-1",
                    "sellerName": "seller display",
                    "text": "Seller one\nPokemon cards",
                    "title": "Live · 79",
                    "startedAt": None,
                }
            ],
            "candidateLinks": 1,
            "emptyState": False,
        }
    )

    shows = WhatnotBrowser(Context(page)).scan_keyword("Pokemon cards")

    assert [action[0] for action in page.actions] == ["goto", "fill", "press", "click", "click"]
    assert page.actions[-2:] == [("click", "shows"), ("click", "show_all")]
    assert shows[0].name == "seller display"
    assert shows[0].url == "https://www.whatnot.com/show/show-1"
    assert "searchVertical=LIVESTREAM" in page.url
    assert not page.closed


def test_live_viewer_badge_is_never_used_as_room_name():
    assert is_live_viewer_badge("Live · 124")
    assert is_live_viewer_badge("LIVE. 1.2K")
    assert not is_live_viewer_badge("houseofjunk")

    assert (
        room_name_from_card(
            "socalluv2",
            "https://www.whatnot.com/user/socalluv2",
            "Live · 124",
        )
        == "socalluv2"
    )
    assert (
        room_name_from_card(
            None,
            "https://www.whatnot.com/user/houseofjunk",
            "Live · 79",
        )
        == "houseofjunk"
    )
    assert room_name_from_card(None, None, "Live · 79") == "未知直播间"


class InspectionPage:
    def __init__(self, url="about:blank", *, body="Live now", navigation_error=False):
        self.url = url
        self.body = body
        self.navigation_error = navigation_error
        self.closed = False
        self.navigations = []
        self.reads = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def goto(self, url, **_kwargs):
        self.navigations.append(url)
        self.url = url
        if self.navigation_error:
            raise PlaywrightError("navigation timeout")

    def wait_for_timeout(self, _milliseconds):
        pass

    def evaluate(self, _script):
        self.reads += 1
        return {
            "title": "Whatnot",
            "body": self.body,
            "live": "Live now" in self.body,
            "ended": "Show has ended" in self.body,
            "audienceCandidates": ["123 viewers"],
            "media": [],
        }


class InspectionContext:
    def __init__(self, *, pages=(), new_page=None):
        self.pages = list(pages)
        self.created = 0
        self.next_page = new_page

    def new_page(self):
        self.created += 1
        page = self.next_page or InspectionPage()
        self.pages.append(page)
        return page


@pytest.mark.parametrize("method", ["inspect_page", "inspect_live"])
def test_inspection_classifies_challenge_before_live_or_ended(method):
    url = "https://www.whatnot.com/live/show-1"
    page = InspectionPage(url, body="请验证您是真人。Show has ended")
    context = InspectionContext(pages=[page])
    browser = WhatnotBrowser(context)

    with pytest.raises(WhatnotVerificationRequired):
        getattr(browser, method)(page if method == "inspect_page" else url)

    assert not page.closed
    assert page.navigations == []
    assert context.created == 0


def test_inspect_live_reuses_recording_page_without_navigation_or_close():
    url = "https://www.whatnot.com/live/show-1"
    page = InspectionPage(url + "?source=room#player")
    context = InspectionContext(pages=[page])
    browser = WhatnotBrowser(context)

    for _ in range(2):
        state = browser.inspect_live(url)
        assert state["live"] is True
        assert state["ended"] is False
        assert state["viewers"] == 123
        assert "body" not in state

    assert context.created == 0
    assert page.navigations == []
    assert not page.closed


def test_inspect_live_reuses_owned_page_without_touching_other_recordings():
    active = InspectionPage("https://www.whatnot.com/live/recording")
    context = InspectionContext(pages=[active])
    browser = WhatnotBrowser(context)
    first_url = "https://www.whatnot.com/live/show-1"
    second_url = "https://www.whatnot.com/live/show-2"

    browser.inspect_live(first_url)
    browser.inspect_live(first_url)
    browser.inspect_live(second_url)

    assert context.created == 1
    assert context.pages[-1].navigations == [first_url, second_url]
    assert active.navigations == []
    assert active.reads == 0
    assert not active.closed


def test_inspect_live_retains_challenge_and_resumes_same_page_after_manual_completion():
    first_url = "https://www.whatnot.com/live/show-1"
    second_url = "https://www.whatnot.com/live/show-2"
    page = InspectionPage(body="请验证您是真人")
    context = InspectionContext(new_page=page)
    browser = WhatnotBrowser(context)

    for url in (first_url, first_url, second_url):
        with pytest.raises(WhatnotVerificationRequired):
            browser.inspect_live(url)

    assert context.created == 1
    assert page.navigations == [first_url]
    assert not page.closed

    page.body = "Live now"
    assert browser.inspect_live(first_url)["live"] is True
    assert page.navigations == [first_url]
    assert context.created == 1


def test_inspection_timeout_keeps_unknown_page_and_blocks_next_navigation(monkeypatch):
    import whatnot_monitor.whatnot as module
    from whatnot_monitor.bounded_browser_read import BrowserReadTimeout
    first_url = "https://www.whatnot.com/live/show-1"
    second_url = "https://www.whatnot.com/live/show-2"
    page = InspectionPage(body="Live now")
    context = InspectionContext(new_page=page)
    browser = WhatnotBrowser(context)
    original = module.bounded_evaluate
    blocked = [True]
    def read(candidate, script):
        if candidate is page and blocked[0]:
            raise BrowserReadTimeout("synthetic timeout")
        return original(candidate, script)
    monkeypatch.setattr(module, "bounded_evaluate", read)
    with pytest.raises(WhatnotPageStateUnknown):
        browser.inspect_live(first_url)
    assert page.navigations == [first_url] and not page.closed
    for operation in (lambda: browser.inspect_live(second_url), lambda: browser.scan_keyword("needoh"),
                      lambda: browser.new_room_page(second_url), lambda: browser.open_login()):
        with pytest.raises(WhatnotPageStateUnknown):
            operation()
        assert page.navigations == [first_url] and context.created == 1 and not page.closed
    # A successful read may reveal a challenge; retain that same page until the
    # person has completed the real verification.
    blocked[0] = False
    page.body = "请验证您是真人"
    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_live(second_url)
    assert page.navigations == [first_url] and not page.closed
    page.body = "Live now"
    assert browser.inspect_live(second_url)["live"] is True
    assert page.navigations == [first_url, second_url]


def test_challenge_blocks_navigation_but_not_reading_another_recording():
    active = InspectionPage("https://www.whatnot.com/live/recording", body="请验证您是真人")
    other = InspectionPage("https://www.whatnot.com/live/other")
    context = InspectionContext(pages=[active, other])
    browser = WhatnotBrowser(context)

    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_page(active)
    assert browser.inspect_page(other)["ended_candidate"] is False
    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_live("https://www.whatnot.com/live/new")

    assert other.reads == 1
    assert context.created == 0
    assert not active.closed
    assert active.navigations == []

    active.body = "Live now"
    assert browser.inspect_page(other)["ended_candidate"] is False
    assert other.reads == 2


def test_reused_inspection_page_cannot_overwrite_an_asynchronous_challenge():
    page = InspectionPage()
    browser = WhatnotBrowser(InspectionContext(new_page=page))
    first = "https://www.whatnot.com/live/first"
    browser.inspect_live(first)
    page.body = "正在进行安全验证"
    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_live("https://www.whatnot.com/live/second")
    assert page.navigations == [first]
    assert not page.closed
    assert browser.verification_info["verification_phase"] == "loading"


def test_main_frame_navigation_event_is_deferred_and_blocks_new_rooms():
    class WatchedPage(InspectionPage):
        def __init__(self):
            super().__init__()
            self.main_frame = object()
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

    class WatchedContext(InspectionContext):
        def on(self, event, callback):
            self.page_callback = callback

    page = WatchedPage()
    context = WatchedContext(pages=[page])
    browser = WhatnotBrowser(context)
    browser._check_pending_verification()
    before = page.reads
    page.body = "正在进行安全验证"
    page.handlers["framenavigated"](page.main_frame)
    assert page.reads == before  # No nested evaluate inside an event callback.
    with pytest.raises(WhatnotVerificationRequired):
        browser.new_room_page("https://www.whatnot.com/live/new")
    assert context.created == 0 and not page.closed


@pytest.mark.parametrize("action_visible,expected", [(False, "loading"), (True, "action_visible")])
def test_browser_classifies_loading_separately_from_visible_action(action_visible, expected):
    page = InspectionPage(body="正在进行安全验证")
    original = page.evaluate
    def read(script):
        return {**original(script), "verificationActionVisible": action_visible}
    page.evaluate = read
    browser = WhatnotBrowser(InspectionContext(pages=[page]))
    with pytest.raises(WhatnotVerificationRequired) as error:
        browser.inspect_page(page)
    assert error.value.phase == expected
    assert browser.verification_info["verification_phase"] == expected


def test_all_retained_challenges_must_clear_before_new_navigation():
    pages = [InspectionPage(body="请验证您是真人") for _ in range(2)]
    browser = WhatnotBrowser(InspectionContext(pages=pages))
    for page in pages:
        with pytest.raises(WhatnotVerificationRequired):
            browser.inspect_page(page)
    pages[1].body = "Live now"
    browser.inspect_page(pages[1])
    with pytest.raises(WhatnotVerificationRequired):
        browser.new_room_page("https://www.whatnot.com/live/new")
    assert all(not page.closed and not page.navigations for page in pages)


def test_unknown_tab_does_not_hide_another_recording_challenge():
    unknown = InspectionPage()
    challenge = InspectionPage(body="请验证您是真人")
    browser = WhatnotBrowser(InspectionContext(pages=[unknown, challenge]))
    browser.mark_page_state_unknown(unknown)
    with pytest.raises(WhatnotVerificationRequired):
        browser.inspect_page(challenge)
    assert challenge.reads == 1
    assert unknown.reads == 0


def test_inspect_page_preserves_explicit_ended_candidate_contract():
    page = InspectionPage(body="Show has ended")
    browser = WhatnotBrowser(InspectionContext(pages=[page]))

    state = browser.inspect_page(page)

    assert state["ended_candidate"] is True
    assert "ended" not in state
    assert "live" not in state
    assert "body" not in state
    assert state["viewers"] == 123


def test_inspect_live_navigation_timeout_keeps_and_classifies_challenge():
    page = InspectionPage(body="请验证您是真人", navigation_error=True)
    context = InspectionContext(new_page=page)
    browser = WhatnotBrowser(context)
    url = "https://www.whatnot.com/live/show-1"

    for _ in range(2):
        with pytest.raises(WhatnotVerificationRequired):
            browser.inspect_live(url)

    assert page.navigations == [url]
    assert context.created == 1
    assert not page.closed


def test_room_navigation_waits_for_required_media_with_a_bound():
    page = RoomPage()
    result = WhatnotBrowser(Context(page)).new_room_page(
        "https://www.whatnot.com/live/show-1",
        require_audio=True,
        require_video=True,
        media_timeout_ms=25_000,
    )

    assert result is page
    assert page.wait_args[0] == {"requireAudio": True, "requireVideo": True}
    assert 0 < page.wait_args[1] <= 25_000
    assert "RTCPeerConnection" in page.init_script
    assert not page.closed


def test_room_media_timeout_reports_diagnostics_and_closes_failed_page():
    page = RoomPage(ready=False)
    with pytest.raises(WhatnotPageChanged, match="未提供所需的可播放媒体轨道"):
        WhatnotBrowser(Context(page)).new_room_page(
            "https://www.whatnot.com/live/show-1",
            media_timeout_ms=100,
        )

    assert page.closed


def test_room_verification_page_is_left_open_for_manual_completion():
    page = RoomPage(body="正在进行安全验证。")
    with pytest.raises(WhatnotVerificationRequired):
        WhatnotBrowser(Context(page)).new_room_page(
            "https://www.whatnot.com/live/show-1"
        )

    assert not page.closed


def test_room_challenge_blocks_reopening_room_until_manually_completed():
    class ChallengeRoom(RoomPage):
        def is_closed(self):
            return self.closed

    page = ChallengeRoom(body="请验证您是真人")
    context = Context(page)
    browser = WhatnotBrowser(context)
    with pytest.raises(WhatnotVerificationRequired):
        browser.new_room_page("https://www.whatnot.com/live/show-1")
    # No second page is created: the same pending document is checked first.
    context.new_page = lambda: pytest.fail("opened another challenge tab")
    with pytest.raises(WhatnotVerificationRequired):
        browser.new_room_page("https://www.whatnot.com/live/show-2")
    with pytest.raises(WhatnotVerificationRequired):
        browser.open_login()
    assert not page.closed


def test_room_navigation_timeout_detects_late_challenge_without_closing_page():
    class TimedOutRoom(InspectionPage):
        def add_init_script(self, _script):
            pass

    page = TimedOutRoom(body="x" * 600 + "请验证您是真人", navigation_error=True)
    context = InspectionContext(new_page=page)
    browser = WhatnotBrowser(context)
    for _ in range(2):
        with pytest.raises(WhatnotVerificationRequired):
            browser.new_room_page("https://www.whatnot.com/live/show-1")
    assert not page.closed
    assert context.created == 1
    assert len(page.navigations) == 1


def test_empty_live_shows_page_is_reported_as_empty_not_navigation_failure():
    page = VerificationPage(
        result={
            "title": "No live shows · Whatnot",
            "body": "There's nothing here at the moment.",
            "rows": [],
            "candidateLinks": 0,
            "emptyState": True,
        }
    )

    assert WhatnotBrowser(Context(page)).scan_keyword("needoh") == []
    assert page.selected_all
    assert "query=needoh" in page.url
    assert not page.closed


def test_search_page_without_search_box_is_not_recorded_as_empty():
    page = VerificationPage(searchbox_visible=False)
    with pytest.raises(WhatnotPageChanged, match="没有可用的搜索框"):
        WhatnotBrowser(Context(page)).scan_keyword("needoh")


def test_parse_viewer_formats():
    assert parse_viewers("1,250") == 1250
    assert parse_viewers("1.2K") == 1200
    assert parse_viewers("1.5万") == 15000
    assert parse_viewers(None) is None
    assert parse_viewers("unavailable") is None
    assert parse_viewers("1,25 viewers") is None


def test_labeled_live_audience_parser_accepts_explicit_viewer_labels_only():
    assert parse_labeled_live_viewers("1,250 viewers") == 1250
    assert parse_labeled_live_viewers("Live viewers: 100") == 100
    assert parse_labeled_live_viewers("101 people watching now") == 101
    assert parse_labeled_live_viewers("正在观看 99 人") == 99
    assert parse_labeled_live_viewers("观看人数：1.2K") == 1200
    assert parse_labeled_live_viewers("100 items sold") is None
    assert parse_labeled_live_viewers("2,500 total views") is None
    assert parse_labeled_live_viewers("currently popular: 101") is None


def test_platform_datetime_requires_explicit_timezone():
    assert (
        parse_platform_datetime("2026-11-11T10:00:00Z") == "2026-11-11T10:00:00+00:00"
    )
    assert parse_platform_datetime("2026-11-11 10:00:00") is None
    assert parse_platform_datetime("not a timestamp") is None


def test_show_and_seller_identity_are_distinct_and_source_is_recorded():
    room_id, session_id, source = _show_identity(
        "https://www.whatnot.com/show/show-123",
        "https://www.whatnot.com/user/seller-456",
        "2026-11-11T10:00:00+00:00",
    )

    assert room_id == "whatnot-seller:seller-456"
    assert session_id == "whatnot-show:show-123"
    assert source == "whatnot_show_link"


def test_show_identity_fallback_is_explicit_and_first_seen_scoped():
    first = _show_identity(
        "https://www.whatnot.com/seller/seller-456",
        None,
        "2026-11-11T10:00:00+00:00",
    )
    second = _show_identity(
        "https://www.whatnot.com/seller/seller-456",
        None,
        "2026-11-11T10:05:00+00:00",
    )

    assert first[0].startswith("unresolved-seller:")
    assert first[1].startswith("url-fallback:")
    assert first[1] != second[1]
    assert "url_first_seen_fallback+seller_url_unavailable" == first[2]


def test_existing_profile_endpoint_uses_only_matching_debuggable_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("whatnot_monitor.whatnot.os", SimpleNamespace(name="nt", path=os.path))
    profile = tmp_path / "Whatnot Browser"
    command_line = (
        f'"chrome.exe" --user-data-dir="{profile}" '
        "--remote-debugging-address=127.0.0.1 --remote-debugging-port=63438"
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {"webSocketDebuggerUrl": "ws://127.0.0.1:63438/devtools/browser/id"}
            ).encode()

    monkeypatch.setattr(
        "whatnot_monitor.whatnot.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"Name": "chrome.exe", "CommandLine": command_line}),
        ),
    )
    monkeypatch.setattr(
        "whatnot_monitor.whatnot.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    assert WhatnotBrowser._existing_profile_endpoint(str(profile), "chrome") == (
        "http://127.0.0.1:63438"
    )


def test_windows_launcher_attaches_to_existing_profile_without_relaunch(
    tmp_path, monkeypatch
):
    endpoint = "http://127.0.0.1:63438"
    context = object()

    class Browser:
        def __init__(self):
            self.contexts = [context]

    class Chromium:
        def connect_over_cdp(self, actual_endpoint, *, timeout):
            assert actual_endpoint == endpoint
            assert timeout == 20_000
            return Browser()

    class Manager:
        chromium = Chromium()

    monkeypatch.setattr(
        WhatnotBrowser,
        "_existing_profile_endpoint",
        classmethod(lambda cls, _profile, _channel: endpoint),
    )
    monkeypatch.setattr(
        WhatnotBrowser,
        "_find_installed_browser",
        staticmethod(lambda _channel: "chrome.exe"),
    )
    monkeypatch.setattr(
        "whatnot_monitor.whatnot.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("must reuse existing Chrome"),
    )

    attached = WhatnotBrowser._launch_windows_chrome(
        str(tmp_path / "profile"), "chrome", Manager()
    )

    assert attached.context is context
    assert attached._process is None


def test_search_fill_timeout_checks_for_new_security_challenge():
    class ChallengeOnFill(FakeLocator):
        def fill(self, value, **_kwargs):
            self.page.body = "正在进行安全验证。本网站使用安全服务防护恶意自动程序。"
            raise PlaywrightError("input became unavailable")

    class Page(VerificationPage):
        def locator(self, selector):
            return ChallengeOnFill(self, "searchbox", self.searchbox_visible)

    page = Page("Whatnot search")
    with pytest.raises(WhatnotVerificationRequired):
        WhatnotBrowser(Context(page)).scan_keyword("needoh")
    assert not page.submitted
    assert not page.closed
