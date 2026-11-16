from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright

from whatnot_monitor.live_identity import live_room_identity
from whatnot_monitor.platform_browser import BROWSER_TYPES
from whatnot_monitor.store import Store


@pytest.mark.parametrize(("platform", "url"), [
    ("tiktok", "https://www.tiktok.com/@seller/live"),
    ("douyin", "https://live.douyin.com/4545"),
    ("xiaohongshu", "https://www.xiaohongshu.com/live/room-533"),
    ("bilibili", "https://live.bilibili.com/7654"),
])
def test_additional_platform_routes_and_observes_only_its_live_room(platform, url):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context()
        adapter = BROWSER_TYPES[platform](context)

        def respond(route):
            if urlsplit(route.request.url).path == urlsplit(url).path:
                body = """
                    <h1>直播中</h1><span>125人正在观看</span><span>999 人气</span>
                    <video></video><script>
                    const player = document.querySelector('video');
                    for (const [key, value] of Object.entries({paused: false, readyState: 4}))
                        Object.defineProperty(player, key, {value});
                    </script>
                """
            else:
                body = f'<a href="{url}?from=search">直播间</a><a href="{url}">重复</a>'
                body += '<a href="https://live.douyin.com.evil.test/1234">其他站点</a>'
            route.fulfill(body=body, content_type="text/html; charset=utf-8")

        context.route("**/*", respond)
        try:
            [show] = adapter.scan_keyword("test & test")
            assert show.session_id == f"platform-live:{live_room_identity(url)}:candidate"
            assert show.url == url
            state = adapter.inspect_live(show.url)
            assert state["live"] and state["viewers"] == 125
            room = adapter._inspection_page
            room.locator("h1").evaluate("(element) => { element.innerText = '直播已结束' }")
            assert adapter.inspect_page(room)["ended_candidate"]
            assert adapter.inspect_page(room)["viewers"] is None
        finally:
            adapter.close()
            browser.close()


def test_room_identity_rejects_lookalike_urls():
    for url in (
        "https://www.tiktok.com.example.org/@seller/live",
        "https://www.tiktok.com/@seller/video/1234",
        "https://live.bilibili.com:9443/7654",
        "https://www.rednote.com/explore/abc",
    ):
        assert live_room_identity(url) is None


def test_platform_waiting_queues_are_isolated():
    with Store(":memory:") as store:
        for session_id in ("whatnot-show:old", "platform-live:tiktok:seller", "platform-live:bilibili:42"):
            store.create_session(
                session_id, session_id, session_id, "https://example.test/room",
                started_at=None, start_time_source="first_seen_estimate",
                first_seen_at="2026-11-15T20:00:00+00:00",
            )
        assert [row.session_id for row in store.waiting_sessions()] == ["whatnot-show:old"]
        assert [row.session_id for row in store.waiting_sessions(platform="tiktok")] == ["platform-live:tiktok:seller"]
        assert store.waiting_sessions(platform="douyin") == []


def test_room_broadcast_ids_survive_restart_and_reopen_only_on_new_evidence(tmp_path):
    path = tmp_path / "broadcasts.sqlite3"
    room = "bilibili:123"
    first_time = "2026-11-15T20:00:00+00:00"
    second_time = "2026-11-15T21:00:00+00:00"
    with Store(path) as store:
        first, _ = store.live_episode(room, first_time)
        assert store.live_episode(room, None)[0] == first
    with Store(path) as store:
        assert store.live_episode(room, first_time)[0] == first
        store.mark_room_ended(room, first)
        assert store.live_episode(room, first_time)[0] == first
        second, _ = store.live_episode(room, second_time)
        assert second != first
        store.mark_room_ended(room, first)  # A late end from the last broadcast.
        assert store.live_episode(room, None)[0] == second
        store.mark_room_ended(room, second)
        third, _ = store.live_episode(room, None)
        assert third not in {first, second}
        assert store.live_episode(room, None)[0] == third
