"""Browser-side discovery and observation for additional livestream platforms."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError

from .live_identity import live_room_identity
from .whatnot import LiveShow, SearchDiagnosticError, WhatnotBrowser, WhatnotVerificationRequired

_SEARCH_LINKS = """() => Array.from(document.querySelectorAll('a[href]'), a => ({
    url: a.href, name: (a.getAttribute('aria-label') || a.innerText || a.title || '').trim()
})).slice(0, 1000)"""

_ROOM_DOCUMENT = r"""() => {
    const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    const player = Array.from(document.querySelectorAll('video')).filter(visible);
    const status = Array.from(document.querySelectorAll(
        'h1,h2,[role=status],[data-e2e=live-status],[data-testid=live-status],.live-status'
    )).filter(visible).map(text);
    const ended = status.some(s => /^(?:this )?(?:live|live stream|stream|livestream|broadcast) (?:has )?ended[.!]?$/i.test(s)
        || /^(?:直播已结束|直播结束|本场直播已结束|本场直播结束|主播已下播|主播不在直播)[。！]?$/.test(s));
    const playing = player.some(v => !v.ended && !v.paused && v.readyState >= 2);
    const live = !ended && playing && (player.some(v => !v.paused && v.duration === Infinity)
        || status.some(s => /^(?:live|live now|正在直播|直播中)$/i.test(s)));
    // Only explicitly labelled current audiences count. Heat, likes, cumulative
    // views, and rankings do not establish a viewer count.
    const candidates = [];
    if (live) for (const el of document.querySelectorAll('[aria-label],[title],button,span,[data-e2e=live-viewer-count]')) {
        if (el.closest('[role=log],[role=listitem],[data-e2e*=chat]') || !visible(el)) continue;
        for (const label of [el.getAttribute('aria-label'), el.getAttribute('title'), el.children.length ? '' : text(el)]) {
            if (label && label.length <= 64) {
                candidates.push(label.replace(/^(?:当前在线人数|当前观看人数|在线人数)/, '正在观看'));
            }
        }
    }
    return {
        title: document.title, body: document.body?.innerText || '',
        broadcastStart: (() => {
            const room = window.__NEPTUNE_IS_MY_WAIFU__?.roomInitRes?.data;
            const roomId = location.pathname.match(/^\/(?:blanc\/)?(\d+)\/?$/)?.[1];
            if (location.hostname === 'live.bilibili.com' && roomId &&
                [String(room?.room_id), String(room?.short_id)].includes(roomId) &&
                Number.isFinite(room?.live_time) && room.live_time > 0) {
                return new Date(room.live_time * 1000).toISOString();
            }
            return document.querySelector('meta[itemprop=startDate]')?.content || null;
        })(),
        live, ended, audienceCandidates: [...new Set(candidates)],
        verificationActionVisible: Array.from(document.querySelectorAll('button,[role=checkbox]'))
            .some(e => visible(e) && /^(?:verify|verify you are human|验证|进行验证)$/i.test(text(e))),
    };
}"""


class PlatformBrowser(WhatnotBrowser):
    document_script = _ROOM_DOCUMENT
    platform: str
    search_url: str

    def _read_page(self, page, script, **kwargs):
        result = super()._read_page(page, script, **kwargs)
        if script == self.document_script and result.get("title", "").strip() == "验证码中间页":
            error = WhatnotVerificationRequired("平台显示验证码，请在专用浏览器中完成后再启动。")
            self._remember_block(page, error)
            raise error
        return result

    def _inspect_document(self, page):
        result = super()._inspect_document(page)
        identity = live_room_identity(page.url)
        if not identity or not identity.startswith(f"{self.platform}:"):
            result.update(live=False, ended=False, viewers=None, viewers_raw=None)
        return result

    def scan_keyword(self, keyword: str) -> list[LiveShow]:
        self._check_pending_verification()
        page = self._search_page
        if page is None or page.is_closed():
            page = self._search_page = self.context.new_page()
        try:
            page.goto(self.search_url.format(keyword=quote(keyword, safe="")),
                      wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1500)
            self._inspect_document(page)
            if urlsplit(page.url).path.rstrip("/").endswith("/login"):
                raise SearchDiagnosticError("平台要求登录，请在账号库重新登录并确认。")
            search_host = urlsplit(self.search_url).hostname
            if urlsplit(page.url).hostname != search_host:
                raise SearchDiagnosticError("平台搜索页发生跳转，请检查专用浏览器中的登录状态。")
            links = self._read_page(page, _SEARCH_LINKS)
        except WhatnotVerificationRequired:
            raise
        except PlaywrightError:
            if not page.is_closed():
                self._inspect_document(page)
            raise SearchDiagnosticError("平台搜索页未能加载或读取，请检查专用浏览器。") from None

        now = datetime.now(UTC).isoformat()
        rooms = {}
        for link in links:
            identity = live_room_identity(link["url"])
            if not identity or not identity.startswith(f"{self.platform}:"):
                continue
            parts = urlsplit(link["url"])
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            rooms.setdefault(identity, LiveShow(
                session_id=f"platform-live:{identity}:candidate", room_id=identity,
                name=" ".join(link["name"].split())[:120] or identity,
                url=url, keyword=keyword, started_at=None, start_time_source="first_seen_estimate",
                first_seen_at=now, viewers=None, viewers_raw=None, identity_basis="platform_show_id",
            ))
        return list(rooms.values())


class TikTokBrowser(PlatformBrowser):
    platform = "tiktok"
    search_url = "https://www.tiktok.com/search/live?q={keyword}"


class DouyinBrowser(PlatformBrowser):
    platform = "douyin"
    search_url = "https://www.douyin.com/search/{keyword}?type=live"


class XiaohongshuBrowser(PlatformBrowser):
    platform = "xiaohongshu"
    search_url = "https://www.xiaohongshu.com/search_result?keyword={keyword}"


class BilibiliBrowser(PlatformBrowser):
    platform = "bilibili"
    search_url = "https://search.bilibili.com/live?keyword={keyword}"


BROWSER_TYPES = {
    "whatnot": WhatnotBrowser,
    "tiktok": TikTokBrowser,
    "douyin": DouyinBrowser,
    "xiaohongshu": XiaohongshuBrowser,
    "bilibili": BilibiliBrowser,
}
