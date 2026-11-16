from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .bounded_browser_read import BrowserReadTimeout, bounded_evaluate
from .live_identity import ROOM_IDENTITY_JS
from .recording.media_sources import ROOM_CAPTURE_INIT_SCRIPT


class SearchDiagnosticError(RuntimeError):
    """Search failure with a strictly sanitized diagnostic message."""


class WhatnotBrowserProfileInUse(RuntimeError):
    """Another browser process owns the Whatnot profile."""

    def __init__(self) -> None:
        super().__init__(
            "Whatnot 专用浏览器配置正在使用中。请退出占用该配置的浏览器"
            "（macOS 用 ⌘Q），再回到监控窗口点击启动。"
        )


class WhatnotPageChanged(RuntimeError):
    """Raised when the expected public Whatnot page structure is missing."""


class WhatnotVerificationRequired(WhatnotPageChanged):
    """A retained challenge, not permission to click or bypass its controls."""

    def __init__(self, message: str, *, phase: str = "unknown"):
        super().__init__(message)
        self.phase = phase


class WhatnotPageStateUnknown(WhatnotVerificationRequired):
    """A retained page timed out and has not been verified as safe to navigate."""


def _evaluate_page(page: Page, script: str, *, bounded: bool = False, **kwargs):
    """Read the current document despite a bounded navigation/context race.

    This never reloads or bypasses a verification page. Chrome may replace the
    document between DOMContentLoaded and evaluate, especially during a challenge.
    Only retry that transient read, not arbitrary script failures.
    """
    for attempt in range(4):
        try:
            return bounded_evaluate(page, script, **kwargs) if bounded else page.evaluate(script, **kwargs)
        except PlaywrightError as exc:
            navigation_race = any(
                marker in str(exc).casefold()
                for marker in (
                    "execution context was destroyed",
                    "cannot find context with specified id",
                )
            )
            if not navigation_race or attempt == 3 or page.is_closed():
                raise
            page.wait_for_timeout(250)


_VERIFICATION_MARKERS = (
    "captcha",
    "verify you are human",
    "verify you're human",
    "access denied",
    "security verification",
    "checking your browser",
    "just a moment",
    "please wait...",
    "security check",
    "正在进行安全验证",
    "安全验证",
    "恶意自动程序",
    "验证您不是自动程序",
    "验证您不是机器人",
    "验证您是真人",
)


def _raise_if_verification_page(title: str, body: str, *, action_visible: bool = False) -> None:
    page_text = f"{title}\n{body}".casefold()
    if any(marker.casefold() in page_text for marker in _VERIFICATION_MARKERS):
        loading = any(marker in page_text for marker in (
            "正在进行安全验证", "checking your browser", "just a moment", "please wait",
        ))
        phase = "action_visible" if action_visible else "loading" if loading else "unknown"
        raise WhatnotVerificationRequired("Whatnot 安全验证页面已拦截访问。", phase=phase)


@dataclass(frozen=True)
class LiveShow:
    session_id: str
    room_id: str
    name: str
    url: str
    keyword: str
    started_at: str | None
    start_time_source: str
    first_seen_at: str
    viewers: int | None
    viewers_raw: str | None
    identity_basis: str = "whatnot_show_link"


def parse_viewers(raw: str | None) -> int | None:
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw).strip().lower()
    match = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*(k|m|万)?", value)
    if not match:
        return None
    number_text = match.group(1)
    if "," in number_text and not re.fullmatch(
        r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", number_text
    ):
        return None
    suffix = match.group(2)
    if "." in number_text and suffix is None:
        return None  # An unscaled fractional count is not an integer audience.
    number = float(number_text.replace(",", ""))
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    elif suffix == "万":
        number *= 10_000
    return int(number)


def parse_platform_datetime(raw: str | None) -> str | None:
    """Normalize an explicit timezone-aware platform timestamp; never infer a date."""
    if not raw:
        return None
    candidate = raw.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]", candidate):
        return None
    try:
        normalized = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        value = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC).isoformat()


def parse_labeled_live_viewers(raw: str | None) -> int | None:
    """Parse only text whose full value identifies a current live audience."""
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    patterns = (
        r"^(?:live\s+)?(?:viewer|audience)\s+count\s*[:\-]?\s*([\d,.]+\s*[kKmM万]?)$",
        r"^(?:live\s+)?viewers?\s*[:\-]?\s*([\d,.]+\s*[kKmM万]?)$",
        r"^([\d,.]+\s*[kKmM万]?)\s+(?:live\s+)?viewers?$",
        r"^([\d,.]+\s*[kKmM万]?)\s+(?:people\s+)?watching(?:\s+now)?$",
        r"^watching(?:\s+now)?\s*[:\-]?\s*([\d,.]+\s*[kKmM万]?)$",
        r"^([\d,.]+\s*[kKmM万]?)\s*(?:人正在观看|人观看|正在观看)$",
        r"^(?:正在观看|观看人数)\s*[:：]?\s*([\d,.]+\s*[kKmM万]?)\s*人?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value, re.IGNORECASE)
        if match:
            return parse_viewers(match.group(1))
    return None


_AUDIENCE_PAGE_SCRIPT = """() => {
  const body = document.body.innerText || '';
  const visible = (element) => {
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      element.getClientRects().length > 0;
  };
  const candidates = [];
  const selector = 'span,button,[aria-label],[title],[data-testid],[data-test],[data-qa]';
  for (const element of document.querySelectorAll(selector)) {
    if (!visible(element)) continue;
    const text = (element.innerText || element.textContent || '')
      .replace(/\\s+/g, ' ').trim();
    if (text.length > 80) continue;
    const labels = ['aria-label', 'title', 'data-testid', 'data-test', 'data-qa']
      .map(name => element.getAttribute(name) || '')
      .filter(Boolean);
    const labelText = labels.join(' ');
    if (text) candidates.push(text);
    if (/viewer|watching|audience|正在观看|观看人数/i.test(labelText) && text) {
      candidates.push(`${labelText} ${text}`);
    }
    for (const label of labels) {
      if (label.length <= 80) candidates.push(label);
    }
  }
  const videos = [...document.querySelectorAll('video')];
  // The current site renders the live header counter without an accessible
  // label. Match only the observed counter control, never arbitrary numbers,
  // seller ratings, giveaway entries, bids, prices or the Watching tab itself.
  const counterPath = 'M7.8 10.6a1.4 1.4 0 1 0-2.8 0v7a1.4 1.4 0 1 0 2.8 0zM17.6 10.6A1.4 1.4 0 0 1 19 12v5.6a1.4 1.4 0 1 1-2.8 0V12a1.4 1.4 0 0 1 1.4-1.4M13.4 6.4a1.4 1.4 0 1 0-2.8 0v9.8a1.4 1.4 0 1 0 2.8 0z';
  const headerCounters = [];
  const watchingTab = document.querySelector('[data-testid="tab-watching"]');
  if (watchingTab && visible(watchingTab)) {
    for (const button of document.querySelectorAll('section > button')) {
      if (!visible(button) || button.querySelectorAll('strong').length !== 1) continue;
      const paths = [...button.querySelectorAll('svg[viewBox="0 0 24 24"] path')];
      if (paths.length !== 1 || paths[0].getAttribute('d') !== counterPath) continue;
      const value = (button.innerText || '').replace(/\\s+/g, ' ').trim();
      const strong = button.querySelector('strong');
      if (!visible(strong) || value !== strong.innerText.trim() ||
          !/^[0-9][0-9,.]*\\s*[kKmM]?$/.test(value)) continue;
      let container = button.parentElement;
      while (container && !container.querySelector('video')) container = container.parentElement;
      if (!container || container === document.body || container === document.documentElement) continue;
      const player = container.querySelector('video');
      const b = button.getBoundingClientRect(), v = player.getBoundingClientRect();
      if (!visible(player) || b.top < v.top - 8 || b.top > v.top + 120) continue;
      headerCounters.push(value);
    }
  }
  const ended = /\\bshow has ended\\b|\\bstream has ended\\b|\\breplay available\\b/i.test(body);
  const titleLive = /^\\s*\\S.*\\s+is live(?:\\s*[·|–—-]\\s*.*)?\\s*$/i.test(document.title);
  const live = !ended && (titleLive || /\\blive now\\b|\\bshow is live\\b|正在直播/i.test(body));
  const dateMatch = body.match(/(?:started|live since)\\s+([^\\n]+)/i);
  return {
    title: document.title,
    body,
    verificationActionVisible: [...document.querySelectorAll('button,input[type="checkbox"],[role="checkbox"]')]
      .some(e => visible(e) && /verify|human|验证|真人/i.test(
        `${e.getAttribute('aria-label') || ''} ${e.innerText || ''} ${e.closest('label')?.innerText || ''}`)),
    live,
    ended,
    startedText: dateMatch?.[1] || null,
    audienceCandidates: [...new Set(candidates)],
    liveHeaderCounters: headerCounters,
    media: videos.map(v => ({paused:v.paused, readyState:v.readyState,
      currentTime:v.currentTime,
      // Observation must not create a second capture stream on the player.
      // In Chrome this can stall the MediaRecorders already using its tracks.
      hasAudio: !!(v.srcObject?.getAudioTracks?.().some(t => t.readyState === 'live') ||
        v.mozHasAudio === true || v.webkitAudioDecodedByteCount > 0)}))
  };
}"""



_SEARCH_ERROR_DIAGNOSTICS = """() => {
  const url = new URL(location.href);
  const path = url.pathname.replace(/\\/$/, '');
  const pageKind = path === '' ? 'home' : path === '/search' ? 'search' : 'other';
  const searchMode = url.searchParams.get('searchVertical');
  const inputs = [...document.querySelectorAll(
    'input[type="search"],input[role="searchbox"],input[placeholder*="search" i],' +
    'input[aria-label*="search" i],input[name*="search" i]'
  )];
  const visible = element => {
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      element.getClientRects().length > 0;
  };
  return {
    page_kind: pageKind,
    platform_host: /^(?:www\\.)?whatnot\\.com$/i.test(url.hostname),
    search_mode: searchMode === 'LIVESTREAM' ? 'live' :
      searchMode === 'CATEGORY' ? 'category' : 'other_or_missing',
    inputs_total: inputs.length,
    inputs_visible: inputs.filter(visible).length,
    inputs_enabled: inputs.filter(e => !e.disabled).length,
    inputs_readonly: inputs.filter(e => e.readOnly).length,
    inputs_editable: inputs.filter(e => visible(e) && !e.disabled && !e.readOnly).length,
  };
}"""

_SEARCH_READY_SCRIPT = """() => {
  const body = document.body?.innerText || '';
  const hasShowLinks = [...document.querySelectorAll('a[href]')].some(a => {
    const path = new URL(a.href, location.href).pathname;
    return /\\/(show|shows|live)\\//i.test(path);
  });
  const empty = /no results|no live shows|try another search|there.s nothing here at the moment|没有结果|暂无直播/i.test(body);
  const verification = /captcha|verify you are human|security verification|安全验证|恶意自动程序/i
    .test(`${document.title}\\n${body}`);
  return hasShowLinks || empty || verification;
}"""


_SEARCH_CATEGORY_READY_SCRIPT = """() => {
  const body = document.body?.innerText || '';
  const hasCategory = /(^|\\n)\\s*(?:Live\\s+)?Shows?\\s*(?:\\n|$)/i.test(body);
  const hasShowAll = [...document.querySelectorAll('a,button,[role="button"]')]
    .some(el => /^show all(?:\\s*\\(\\d+\\))?$/i.test((el.innerText || '').trim()));
  const hasShowLinks = [...document.querySelectorAll('a[href]')].some(a => {
    const path = new URL(a.href, location.href).pathname;
    return /\\/(show|shows|live)\\//i.test(path);
  });
  const empty = /no results|no live shows|try another search|there.s nothing here at the moment|没有结果|暂无直播/i.test(body);
  const verification = /captcha|verify you are human|security verification|安全验证|恶意自动程序/i
    .test(`${document.title}\\n${body}`);
  return hasCategory || hasShowAll || hasShowLinks || empty || verification;
}"""


_SHOW_ALL_READY_SCRIPT = """() => {
  const body = document.body?.innerText || '';
  const hasShowAll = [...document.querySelectorAll('a,button,[role="button"]')]
    .some(el => /^show all(?:\\s*\\(\\d+\\))?$/i.test((el.innerText || '').trim()));
  const empty = /no results|no live shows|try another search|there.s nothing here at the moment|没有结果|暂无直播/i.test(body);
  const verification = /captcha|verify you are human|security verification|安全验证|恶意自动程序/i
    .test(`${document.title}\\n${body}`);
  return hasShowAll || empty || verification;
}"""


_ROOM_MEDIA_READY_SCRIPT = """({requireAudio, requireVideo}) => {
  const playing = (element) =>
    !element.paused && !element.ended && element.readyState >= 2;
  const audible = (element) => !element.muted && element.volume > 0;
  const videos = [...document.querySelectorAll('video')].filter(playing);
  const audioElements = [...document.querySelectorAll('audio')].filter(element =>
    element.readyState >= 2);
  const audios = audioElements.filter(element => playing(element) && audible(element));
  const liveAudio = (track) => track.kind === 'audio' &&
    track.readyState === 'live' && track.enabled && !track.muted;
  const audioVideos = [...document.querySelectorAll('video')].filter(video =>
    video.srcObject?.getTracks?.().some(liveAudio) ||
    video.mozHasAudio === true ||
    (typeof video.webkitAudioDecodedByteCount === 'number' &&
      video.webkitAudioDecodedByteCount > 0)
  );
  const embeddedAudio = audioVideos.some(video => playing(video) && audible(video));
  // Some WebRTC players render receiver audio through Web Audio, not an
  // <audio> element. These tracks belong only to this dedicated room page.
  // A live receiver and incoming packets do not prove that Chromium has
  // started decoding PCM. The page hook asynchronously observes getStats;
  // this polling predicate stays synchronous (a Promise is truthy too early).
  const remoteAudio = [...(window.__whatnotRoomRemoteTracks || [])].some(track =>
    liveAudio(track) && window.__whatnotRoomDecodedAudioTracks?.has(track));
  // A decoded receiver must not let a still-muted HTML player pass readiness.
  const webAudioOnly = remoteAudio && !audioElements.length && !audioVideos.length;
  return (!requireVideo || videos.length > 0) &&
    (!requireAudio || audios.length > 0 || embeddedAudio || webAudioOnly);
}"""


# Only explicit audio controls are eligible for a trusted click. Never search
# for generic text such as "Start", "Join", or commerce buttons.
_UNMUTE_CONTROL_RE = re.compile(
    r"^(?:unmute(?:\s+(?:audio|sound|video))?|(?:turn|switch)\s+on\s+(?:sound|audio)|"
    r"enable\s+(?:sound|audio)|(?:sound|audio)\s+(?:off|muted)|"
    r"(?:tap|click)\s+to\s+(?:unmute|enable sound)|"
    r"(?:点击|轻触)?(?:取消静音|开启声音|打开声音|开启音频|打开音频))(?:\s*\([^)]*\))?$", re.IGNORECASE
)
_SOUND_CONTROL_RE = re.compile(r"^(?:sound|audio|声音)$", re.IGNORECASE)

_ROOM_AUDIO_STATUS_SCRIPT = """() => {
  const media = [...document.querySelectorAll('audio,video')];
  return {title: document.title, body: (document.body?.innerText || '').slice(0, 2000),
    audibleElement: media.some(element =>
    !element.paused && !element.ended && element.readyState >= 2 &&
    !element.muted && element.volume > 0 && (
      element.tagName === 'AUDIO' || element.mozHasAudio === true ||
      element.webkitAudioDecodedByteCount > 0 ||
      element.srcObject?.getAudioTracks?.().some(track =>
        track.readyState === 'live' && track.enabled && !track.muted)))
  };
}"""

_ROOM_ENABLE_AUDIO_SCRIPT = """() => {
  for (const element of document.querySelectorAll('audio,video')) {
    if (element.muted) element.muted = false;
    if (element.defaultMuted) element.defaultMuted = false;
    if (element.volume === 0) element.volume = 1;
    for (const track of element.srcObject?.getAudioTracks?.() || []) {
      if (track.readyState === 'live') track.enabled = true;
    }
    if (element.paused && !element.ended && (element.currentSrc || element.srcObject)) {
      // play() can be blocked by autoplay policy. The bounded readiness check
      // below, not this fire-and-forget promise, decides whether startup succeeds.
      void element.play().catch(() => {});
    }
  }
}"""

_ROOM_KEEP_AUDIO_SCRIPT = "() => {" + ROOM_IDENTITY_JS + """
  if (window.__whatnotKeepRoomAudio) return;
  window.__whatnotKeepRoomAudio = true;
  let pending = false;
  const restore = () => {
    const capture = window.__whatnotBrowserAVCapture || window.__whatnotAudioCapture;
    if (!capture?.showId || capture.showId !== showId(new URL(location.href))) return;
    for (const media of document.querySelectorAll('video,audio')) {
      if (media.paused || media.ended || media.readyState < 2) continue;
      if (media.defaultMuted) media.defaultMuted = false;
      if (media.muted) media.muted = false;
      if (media.volume === 0) media.volume = 1;
    }
  };
  const schedule = () => {
    if (pending) return;
    pending = true;
    setTimeout(() => {
      pending = false;
      restore();
    }, 250);
  };
  document.addEventListener('volumechange', schedule, true);
  document.addEventListener('playing', schedule, true);
}"""


_ROOM_MEDIA_DIAGNOSTIC_SCRIPT = """() => ({
  title: document.title,
  body: (document.body?.innerText || '').slice(0, 500),
  media: [...document.querySelectorAll('video,audio')].map((element) => ({
    tag: element.tagName.toLowerCase(),
    paused: element.paused,
    muted: element.muted,
    defaultMuted: element.defaultMuted,
    volume: element.volume,
    ended: element.ended,
    readyState: element.readyState,
    currentTime: element.currentTime,
    hasSource: Boolean(element.currentSrc || element.src),
    audioDecodedBytes: element.webkitAudioDecodedByteCount ?? null,
    sourceTracks: element.srcObject?.getTracks?.().map(track => ({
      kind: track.kind, readyState: track.readyState,
      enabled: track.enabled, muted: track.muted
    })) || []
  })),
  remoteTracks: [...(window.__whatnotRoomRemoteTracks || [])].map(track => ({
    kind: track.kind, readyState: track.readyState,
    enabled: track.enabled, muted: track.muted
  }))
})"""


_LIVE_VIEWER_BADGE_RE = re.compile(
    r"^(?:live(?:\s*[·•.:\-]\s*[\d,.]+\s*[kKmM]?)?|"
    r"[\d,.]+\s*(?:live|viewers?|watching))$",
    re.IGNORECASE,
)


def is_live_viewer_badge(value: str | None) -> bool:
    """Identify card text that is a live/viewer badge, never a room name."""
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return bool(_LIVE_VIEWER_BADGE_RE.fullmatch(normalized))


def room_name_from_card(
    seller_name: str | None, seller_url: str | None, _show_title: str | None = None
) -> str:
    """Prefer the seller label beside the avatar and reject viewer-count badges."""
    seller_handle = None
    if seller_url:
        segments = [
            part for part in urlsplit(seller_url).path.rstrip("/").split("/") if part
        ]
        for index, part in enumerate(segments[:-1]):
            if part.casefold() in {"user", "users", "profile", "seller"}:
                seller_handle = unquote(segments[index + 1]).strip()
                break
    for candidate in (seller_name, seller_handle):
        normalized = re.sub(r"\s+", " ", candidate or "").strip()
        if normalized and not is_live_viewer_badge(normalized):
            return normalized[:180]
    return "未知直播间"


def _viewer_observation(
    candidates: list[str] | None, header_counters: list[str] | None = None,
) -> tuple[int | None, str | None]:
    observations = []
    for candidate in candidates or []:
        value = parse_labeled_live_viewers(candidate)
        if value is not None:
            observations.append((value, candidate))
    # These bare numbers are accepted only from the JS's exact, visible live
    # header control match. Arbitrary numeric page text remains inadmissible.
    counters = header_counters or []
    if len(counters) > 1:
        return None, None  # An ambiguous control layout must fail closed.
    if counters:
        value = parse_viewers(counters[0])
        if value is not None:
            observations.append((value, f"live_header_counter: {counters[0]}"))
    if len({value for value, _ in observations}) == 1:
        return observations[0]
    return None, None


def _show_identity(
    url: str, seller_url: str | None, first_seen_at: str
) -> tuple[str, str, str]:
    """Return distinct seller and show keys, with an explicit URL fallback."""
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.rstrip("/").split("/") if segment]
    show_segment = next(
        (
            index
            for index, segment in enumerate(segments[:-1])
            if segment.casefold() in {"show", "shows", "live"}
        ),
        None,
    )
    if show_segment is not None:
        show_key = segments[show_segment + 1]
        session_id = f"whatnot-show:{show_key.casefold()}"
        source = "whatnot_show_link"
    else:
        canonical = f"{parsed.netloc.casefold()}{parsed.path.rstrip('/').casefold()}"
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        session_id = f"url-fallback:{fingerprint}:{first_seen_at}"
        show_key = fingerprint
        source = "url_first_seen_fallback"

    seller_segments = []
    if seller_url:
        seller_parsed = urlsplit(seller_url)
        seller_segments = [
            segment for segment in seller_parsed.path.rstrip("/").split("/") if segment
        ]
    seller_key = None
    for index, segment in enumerate(seller_segments[:-1]):
        if segment.casefold() in {"user", "users", "profile", "seller"}:
            seller_key = seller_segments[index + 1].casefold()
            break
    if seller_key:
        room_id = f"whatnot-seller:{seller_key}"
    else:
        room_id = f"unresolved-seller:{show_key}"
        source += "+seller_url_unavailable"
    return room_id, session_id, source


class WhatnotBrowser:
    document_script = _AUDIENCE_PAGE_SCRIPT

    def __init__(
        self,
        context: BrowserContext,
        *,
        browser=None,
        process: subprocess.Popen | None = None,
    ):
        self.context = context
        self._browser = browser
        self._process = process
        self._search_page: Page | None = None
        self._inspection_page: Page | None = None
        self.reuse_inspection_for_recording = False
        self._verification_page: Page | None = None
        self._uncertain_page: Page | None = None
        self._blocked_pages: dict[Page, WhatnotVerificationRequired] = {}
        self._navigation_checks: set[Page] = set()
        self.verification_info: dict = {}
        # Event callbacks only enqueue work. Never re-enter sync Playwright
        # reads from a navigation callback while another command owns the loop.
        if callable(getattr(context, "on", None)):
            context.on("page", self._watch_page)
            for page in context.pages:
                self._watch_page(page)

    def _watch_page(self, page: Page) -> None:
        self._navigation_checks.add(page)
        page.on("framenavigated", lambda frame: self._navigation_checks.add(page)
                if frame is page.main_frame else None)

    def _remember_block(self, page: Page, error: WhatnotVerificationRequired) -> None:
        self._blocked_pages[page] = error
        if isinstance(error, WhatnotPageStateUnknown):
            self.mark_page_state_unknown(page)
        else:
            self._verification_page = page
            self.verification_info = {
                "verification_phase": error.phase,
                "verification_url": getattr(page, "url", ""),
                "verification_observed_at": datetime.now(UTC).isoformat(),
            }

    def _read_page(self, page: Page, script: str, **kwargs):
        try:
            return _evaluate_page(page, script, bounded=True, **kwargs)
        except BrowserReadTimeout as exc:
            error = WhatnotPageStateUnknown(f"页面读取超时，页面状态未知；已暂停导航：{exc}")
            self._remember_block(page, error)
            raise error from exc

    def pump_events(self, milliseconds: float) -> None:
        """Keep sync Playwright bindings flowing on the browser-owner thread."""
        page = next((page for page in self.context.pages if not page.is_closed()), None)
        if page is not None:
            page.wait_for_timeout(milliseconds)
        else:
            # No pages means there are no MediaRecorder bindings to drain.
            time.sleep(milliseconds / 1000)

    def is_usable(self) -> bool:
        """Whether the attached context can still accept browser commands."""
        try:
            if self.context.is_closed():
                return False
            return self._browser is None or self._browser.is_connected()
        except Exception:  # noqa: BLE001 - a broken CDP handle is unusable
            return False

    @staticmethod
    def _find_installed_browser(channel: str) -> str | None:
        if sys.platform.startswith("linux"):
            if channel.casefold() == "chrome":
                return shutil.which("google-chrome") or shutil.which("google-chrome-stable")
            return None

        executable = {
            "chrome": "chrome.exe",
            "msedge": "msedge.exe",
        }.get(channel.casefold())
        if executable is None:
            return None

        candidates = [shutil.which(executable)]
        if os.name == "nt":
            program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
            program_files_x86 = os.environ.get(
                "PROGRAMFILES(X86)", r"C:\Program Files (x86)"
            )
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            vendor = (
                r"Google\Chrome"
                if channel.casefold() == "chrome"
                else r"Microsoft\Edge"
            )
            for root in (program_files, program_files_x86, local_app_data):
                if root:
                    candidates.append(
                        str(Path(root) / vendor / "Application" / executable)
                    )

            try:
                import winreg

                key_path = (
                    "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\"
                    + executable
                )
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                        try:
                            with winreg.OpenKey(
                                hive, key_path, 0, winreg.KEY_READ | view
                            ) as key:
                                candidates.append(winreg.QueryValueEx(key, "")[0])
                        except OSError:
                            continue
            except ImportError:
                pass

        return next(
            (
                str(Path(candidate))
                for candidate in candidates
                if candidate and Path(candidate).is_file()
            ),
            None,
        )

    @staticmethod
    def _free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _wait_for_cdp(endpoint: str, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited before its local debugging endpoint was ready "
                    f"(exit code {process.returncode})."
                )
            try:
                with urllib.request.urlopen(
                    endpoint + "/json/version", timeout=0.5
                ) as response:
                    json.load(response)
                return
            except (OSError, ValueError):
                time.sleep(0.1)
        raise TimeoutError(
            "Chrome's local debugging endpoint did not start within 20 seconds."
        )

    @classmethod
    def _existing_profile_endpoint(cls, profile_dir: str, channel: str) -> str | None:
        """Find a live CDP endpoint for this app's already-open browser profile.

        Chrome is single-instance per user-data directory. If a previous app run
        left its visible browser open, launching Chrome again forwards the new
        request to that process and exits successfully without opening the new
        debugging port. Match both the browser executable and exact profile path
        before attaching to any loopback endpoint.
        """
        if os.name != "nt":
            return None

        profile = os.path.normcase(os.path.abspath(profile_dir))
        process_names = {
            "chrome": {"chrome.exe"},
            "msedge": {"msedge.exe"},
        }.get(channel.casefold())
        if not process_names:
            return None

        # A fixed remote-debugging port is present in the process command line;
        # with port 0 Chromium publishes the selected port in this file.
        script = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -in @('chrome.exe','msedge.exe') } | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=creationflags,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            processes = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

        if isinstance(processes, dict):
            processes = [processes]
        for process in processes:
            if not isinstance(process, dict):
                continue
            if str(process.get("Name", "")).casefold() not in process_names:
                continue
            command_line = process.get("CommandLine") or ""
            profile_match = re.search(
                r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s]+))',
                command_line,
                re.IGNORECASE,
            )
            if not profile_match:
                continue
            process_profile = os.path.normcase(
                os.path.abspath(profile_match.group(1) or profile_match.group(2))
            )
            if process_profile != profile:
                continue

            port_match = re.search(
                r"--remote-debugging-port(?:=|\s+)(\d+)",
                command_line,
                re.IGNORECASE,
            )
            port = int(port_match.group(1)) if port_match else None
            if port == 0 or port is None:
                port_file = Path(profile_dir) / "DevToolsActivePort"
                try:
                    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    continue
            if not 1 <= port <= 65535:
                continue

            endpoint = f"http://127.0.0.1:{port}"
            try:
                with urllib.request.urlopen(
                    endpoint + "/json/version", timeout=0.5
                ) as response:
                    metadata = json.load(response)
                if metadata.get("webSocketDebuggerUrl"):
                    return endpoint
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _connect_over_cdp(endpoint: str, manager):
        try:
            browser = manager.chromium.connect_over_cdp(endpoint, timeout=20_000)
        except PlaywrightTimeoutError:
            # CDP's browser endpoint can respond while an existing tab never
            # completes Playwright's initialization. No page handle is available
            # to classify it safely. Use the existing unknown-page pause gate,
            # rather than retrying every scan or replacing/closing retained tabs.
            # Never surface the raw exception: it includes protocol URLs/logs.
            raise WhatnotPageStateUnknown(
                "浏览器调试连接初始化超时，现有标签页状态尚未核实；"
                "已保留原页面并暂停自动连接、搜索和导航。"
                "请在程序专用 Chrome 中检查未响应页面，处理后点击启动重新检查。"
            ) from None
        if not browser.contexts:
            raise RuntimeError(
                "Chrome connected, but did not expose its default context."
            )
        return browser

    @classmethod
    def _launch_windows_chrome(cls, profile_dir: str, channel: str, manager, *, headless: bool = False):
        executable = cls._find_installed_browser(channel)
        if executable is None:
            raise FileNotFoundError(
                f"找不到已安装的 {channel} 浏览器，请检查 browser_channel 设置。"
            )

        profile = Path(profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        existing_endpoint = cls._existing_profile_endpoint(profile_dir, channel)
        if existing_endpoint:
            browser = cls._connect_over_cdp(existing_endpoint, manager)
            return cls(browser.contexts[0], browser=browser)

        port = cls._free_local_port()
        endpoint = f"http://127.0.0.1:{port}"
        command = [
            executable,
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            # Allow only Playwright's local origin for this exact loopback port.
            f"--remote-allow-origins=http://localhost:{port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
            "--autoplay-policy=no-user-gesture-required",
            "about:blank",
        ]
        if headless:
            command.insert(1, "--headless=new")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            try:
                cls._wait_for_cdp(endpoint, process)
            except RuntimeError as exc:
                # Another Chrome process may have claimed the profile between
                # discovery and launch; reuse it if it exposes the local CDP API.
                existing_endpoint = cls._existing_profile_endpoint(profile_dir, channel)
                if existing_endpoint:
                    browser = cls._connect_over_cdp(existing_endpoint, manager)
                    return cls(browser.contexts[0], browser=browser)
                if process.poll() == 0:
                    raise RuntimeError(
                        "The Whatnot Chrome profile is already open, but its process "
                        "does not expose a usable local debugging endpoint. Close that "
                        "profile window and retry."
                    ) from exc
                raise
            browser = cls._connect_over_cdp(endpoint, manager)
            return cls(browser.contexts[0], browser=browser, process=process)
        except WhatnotPageStateUnknown:
            # A browser with unclassified pages is not a failed disposable
            # process. Leave its window/profile intact for manual inspection.
            raise
        except Exception:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise

    @classmethod
    def launch_persistent(
        cls, profile_dir: str, channel: str = "msedge", headless: bool = False,
        *, manager=None, allow_fallback: bool = True,
    ):
        from playwright.sync_api import sync_playwright

        owns_manager = manager is None
        if owns_manager:
            manager = sync_playwright().start()
        browser_type = manager.chromium
        if os.name == "nt" and (channel or "").casefold() in {"chrome", "msedge"}:
            try:
                return cls._launch_windows_chrome(
                    profile_dir, channel, manager, headless=headless,
                ), manager
            except Exception:
                if owns_manager:
                    manager.stop()
                raise
        launch_options = {
            "user_data_dir": profile_dir,
            "headless": headless,
            "args": ["--autoplay-policy=no-user-gesture-required"],
        }
        if (sys.platform == "darwin" or sys.platform.startswith("linux")) and (channel or "").casefold() == "chrome":
            # Match standalone Chrome's cookie encryption for manual sign-in.
            launch_options["ignore_default_args"] = [
                "--password-store=basic",
                "--use-mock-keychain",
            ]
        try:
            try:
                context = browser_type.launch_persistent_context(
                    channel=channel or None, **launch_options
                )
            except PlaywrightError as exc:
                # Only fall back when the requested browser is absent. A profile
                # lock, permission error or security failure must be reported,
                # not masked by an attempt with another browser.
                missing = str(exc).lower()
                if "failed to create a processsingleton for your profile directory" in missing:
                    raise WhatnotBrowserProfileInUse() from None
                is_missing_browser = (
                    "executable doesn't exist" in missing
                    or "executable does not exist" in missing
                    or "distribution" in missing and "not found" in missing
                )
                if not allow_fallback or (channel or "").casefold() not in {"chrome", "msedge"} or not is_missing_browser:
                    raise
                launch_options.pop("ignore_default_args", None)
                context = browser_type.launch_persistent_context(**launch_options)
            return cls(context), manager
        except Exception:
            if owns_manager:
                manager.stop()
            raise

    def open_login(self) -> Page:
        self._check_pending_verification()
        page = self._search_page
        if page is None or page.is_closed() or not self._is_search_landing_page(page):
            page = next(
                (
                    candidate
                    for candidate in reversed(getattr(self.context, "pages", ()))
                    if not candidate.is_closed()
                    and self._is_search_landing_page(candidate)
                ),
                None,
            )
        if page is None:
            page = self.context.new_page()
        self._search_page = page
        if not self._is_search_landing_page(page):
            page.goto(
                "https://www.whatnot.com/", wait_until="domcontentloaded", timeout=60_000
            )
        page.wait_for_timeout(1800)
        page_info = self._read_page(page,
            """() => ({title: document.title, body: document.body?.innerText || ''})"""
        )
        try:
            _raise_if_verification_page(
                page_info.get("title", ""), page_info.get("body", "")
            )
        except WhatnotVerificationRequired as exc:
            self._remember_block(page, exc)
            raise
        return page

    @staticmethod
    def _is_search_landing_page(page: Page) -> bool:
        try:
            parsed = urlsplit(page.url)
        except PlaywrightError:
            return False
        return (
            parsed.netloc.casefold() == "www.whatnot.com"
            and parsed.path.rstrip("/") in {"", "/", "/search"}
        )

    @staticmethod
    def _first_visible_locator(locators):
        for locator in locators:
            try:
                for index in range(min(locator.count(), 8)):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except PlaywrightError:
                continue
        return None

    def _inspect_search_failure_safely(self, page):
        """Retain the page if a post-failure read cannot classify its state."""
        try:
            self._inspect_document(page)
        except WhatnotPageStateUnknown:
            self.mark_page_state_unknown(page)
            raise WhatnotPageStateUnknown("搜索页面状态未知；已保留原页面并暂停导航。") from None
        except WhatnotVerificationRequired as exc:
            self._remember_block(page, exc)
            raise WhatnotVerificationRequired("Whatnot 安全验证页面已拦截访问。", phase=exc.phase) from None
        except Exception:  # noqa: BLE001 - all unclassified reads retain the page
            self.mark_page_state_unknown(page)
            raise WhatnotPageStateUnknown(
                "搜索页面检查失败，页面状态未知；已暂停导航。"
            ) from None

    def _search_box(self, page):
        def find_box():
            return self._first_visible_locator(
                [
                    # Resolve visibility/editability on every locator action,
                    # not only at initial selection: Whatnot can replace its
                    # input or temporarily leave a disabled duplicate mounted.
                    page.locator('input[type="search"]:visible:enabled:not([readonly])'),
                    page.locator('input[role="searchbox"]:visible:enabled:not([readonly])'),
                    page.locator('input[placeholder*="search" i]:visible:enabled:not([readonly])'),
                    page.locator('input[aria-label*="search" i]:visible:enabled:not([readonly])'),
                    page.locator('input[name*="search" i]:visible:enabled:not([readonly])'),
                    page.locator('[role="searchbox"][contenteditable="true"]:visible'),
                ]
            )

        search_box = find_box()
        if search_box is not None:
            return search_box

        search_button = self._first_visible_locator(
            [
                page.get_by_role("button", name=re.compile(r"search", re.IGNORECASE)),
                page.get_by_role("link", name=re.compile(r"search", re.IGNORECASE)),
                page.locator(
                    'button[aria-label*="search" i], button[title*="search" i], '
                    'a[aria-label*="search" i]'
                ),
            ]
        )
        if search_button is not None:
            search_button.click()
            page.wait_for_timeout(250)
            search_box = find_box()
        return search_box

    def _search_control(self, page, name_pattern):
        pattern = re.compile(name_pattern, re.IGNORECASE)
        return self._first_visible_locator(
            [
                page.get_by_role("tab", name=pattern),
                page.get_by_role("link", name=pattern),
                page.get_by_role("button", name=pattern),
                page.get_by_text(pattern),
            ]
        )

    @staticmethod
    def _select_live_filter(page: Page) -> None:
        """Shows includes upcoming programs; select its real Live-only filter."""
        live = page.locator('main input#PLAYING[type="checkbox"]')
        if live.count() != 1:
            # Legacy layouts still use positive per-card Live classification.
            return
        # A deep result scroll can put the sticky sidebar checkbox underneath
        # its own accordion heading. Reset the document before native UI input.
        page.evaluate("() => window.scrollTo({top:0,behavior:'instant'})")
        collapsed = live.evaluate("""input => {
          for (let parent=input.parentElement; parent && parent.tagName !== 'MAIN'; parent=parent.parentElement) {
            if (parent.getBoundingClientRect().height <= 1 &&
              /hidden|clip/.test(getComputedStyle(parent).overflowY)) return true;
          }
          return false;
        }""")
        if collapsed:
            heading = WhatnotBrowser._first_visible_locator([
                page.get_by_text("Time of Show", exact=True)
            ])
            if heading is None:
                raise WhatnotPageChanged("Time of Show 筛选已折叠，但没有可用的展开控件。")
            heading.click(timeout=5000)
        upcoming = page.locator('main input#CREATED[type="checkbox"]')
        if upcoming.count() == 1 and upcoming.is_checked():
            upcoming.click(timeout=5000)
            page.wait_for_function(
                "() => !document.querySelector('main input#CREATED')?.checked",
                timeout=10_000,
            )
        if not live.is_checked():
            # React updates the checked prop after async router navigation.
            # check() tests it too early and can fail despite a successful click.
            live.click(timeout=5000)
        page.wait_for_function(
            """() => {
              const live = document.querySelector('main input#PLAYING');
              const upcoming = document.querySelector('main input#CREATED');
              if (!live?.checked || upcoming?.checked) return false;
              const cards = [...document.querySelectorAll('main section[data-testid="livestream-card"]')];
              const badge = /^(?:live(?:\\s*[·•.:\\-]\\s*[\\d,.]+\\s*[kKmM]?)?|live now|正在直播|直播中)$/i;
              if (cards.length) return cards.every(card => [
                ...card.innerText.split('\\n'),
                ...[...card.querySelectorAll('[aria-label],span,a')].flatMap(el =>
                  [el.getAttribute('aria-label') || '', el.innerText || ''])
              ].some(line => badge.test(line.trim())));
              return !document.querySelector('main [data-type="SkeletonItem"],main [aria-busy="true"]') &&
                /no results|no live shows|there.s nothing here at the moment|没有结果|暂无直播/i.test(document.body.innerText);
            }""",
            timeout=20_000,
        )

    @staticmethod
    def _collect_search_results(
        page: Page, script: str, before_step=None, *, timeout_seconds: float = 120
    ) -> dict:
        """Accumulate cards before scrolling; never finish while loading remains.

        Progress uses every candidate show ID, not the filtered Live count. A
        bounded quiet bottom without loading UI is an observed UI completion,
        not a promise of an atomic snapshot of an ever-changing remote service.
        """
        collected = {}
        stable_bottom = 0
        previous_signature = None
        filter_reselections = 0
        deadline = time.monotonic() + timeout_seconds
        for _ in range(400):
            if before_step is not None:
                before_step()
            result = _evaluate_page(page, script, bounded=True)
            _raise_if_verification_page(result.get("title", ""), result.get("body", ""))
            href = result.get("href", "")
            if href and "searchVertical=LIVESTREAM" not in href:
                raise WhatnotPageChanged("扫描期间页面离开 LIVESTREAM 结果，不能确认搜索完整。")
            for row in result["rows"]:
                key = row["href"].split("?")[0].split("#")[0].rstrip("/")
                collected[key] = row
            scroll = _evaluate_page(page, """() => {
              const showLink = 'a[href*="/live/"],a[href*="/show/"],a[href*="/shows/"]';
              const card = document.querySelector('main section[data-testid="livestream-card"]');
              const grid = card?.parentElement;
              const root = document.scrollingElement;
              let target = null;
              if (grid) {
                for (let ancestor=grid; ancestor; ancestor=ancestor.parentElement) {
                  if (ancestor === root || (ancestor.clientHeight > 0 &&
                    ancestor.scrollHeight > ancestor.clientHeight + 4 &&
                    /auto|scroll/.test(getComputedStyle(ancestor).overflowY))) {
                    target=ancestor; break;
                  }
                }
              } else {
                target = [...document.querySelectorAll('main,section,div'),root]
                  .find(el => el && el.clientHeight > 0 &&
                    el.scrollHeight > el.clientHeight + 4 &&
                    (el === root || /auto|scroll/.test(getComputedStyle(el).overflowY)) &&
                    el.querySelector(showLink));
              }
              const scope = grid || target || document.querySelector('main') || document;
              const ids = [...new Set([...scope.querySelectorAll(showLink)].map(a => {
                const url = new URL(a.href);return url.origin+url.pathname;
              }))];
              const pending = Boolean(scope.querySelector('[data-type="SkeletonItem"],[aria-busy="true"],[role="progressbar"]'));
              const live = document.querySelector('main input#PLAYING');
              const filterLost = Boolean(grid && live && !live.checked);
              const signature = JSON.stringify([ids,scope.scrollHeight]);
              if (!target) return {scrollable:false,pending,signature,filterLost};
              const before = target.scrollTop;
              const edge = target === root ? innerHeight : target.getBoundingClientRect().bottom;
              const atBottom = grid ? grid.getBoundingClientRect().bottom <= edge+2 :
                before+target.clientHeight >= target.scrollHeight-2;
              if (!atBottom) target.scrollTo({top:before+Math.max(100,target.clientHeight*.8),behavior:'instant'});
              return {scrollable:true,atBottom,pending,signature,filterLost};
            }""")
            if scroll.get("filterLost"):
                # The site may reset its checkbox while the result grid is
                # being re-rendered. Results accumulated before that point
                # cannot establish a complete live-only snapshot: discard them
                # and start again from the top after explicitly reselecting.
                if filter_reselections >= 2:
                    raise WhatnotPageChanged("扫描期间 Live 筛选反复被取消，本轮搜索未完成。")
                filter_reselections += 1
                collected.clear()
                stable_bottom = 0
                previous_signature = None
                WhatnotBrowser._select_live_filter(page)
                continue
            if not scroll.get("pending") and (
                scroll.get("atBottom") or not scroll.get("scrollable")
            ) and scroll.get("signature") == previous_signature:
                stable_bottom += 1
            else:
                stable_bottom = 0
            previous_signature = scroll.get("signature")
            # Fakes/legacy non-scrollable results have no progress metadata.
            if (not scroll.get("scrollable") and "signature" not in scroll) or stable_bottom >= 5:
                result["rows"] = list(collected.values())
                return result
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(600)
        raise WhatnotPageChanged(
            f"搜索结果加载达到安全预算，已看到 {len(collected)} 个直播间，"
            "无法确认已加载全部结果（可能仍有加载占位）；本轮不会将不完整结果标记为扫描成功。"
        )

    def _search_checkpoint(self) -> None:
        self._check_pending_verification()
        service = getattr(self, "service_recordings", None)
        if callable(service):
            service()
        self._check_pending_verification()

    def scan_keyword(self, keyword: str) -> list[LiveShow]:
        phase = "准备搜索页面"
        page = None
        try:
            self._check_pending_verification()
            page = self._search_page
            if page is None or page.is_closed():
                page = next(
                    (
                        candidate
                        for candidate in reversed(getattr(self.context, "pages", ()))
                        if not candidate.is_closed()
                        and urlsplit(candidate.url).netloc.casefold() == "www.whatnot.com"
                        and urlsplit(candidate.url).path.rstrip("/") in {"", "/", "/search"}
                    ),
                    None,
                )
                if page is None:
                    page = self.context.new_page()
                self._search_page = page
            # A challenge can occupy the previous /search URL. Inspect the
            # existing document before any home navigation can replace it.
            page_info = self._read_page(page,
                """() => ({title: document.title, body: document.body?.innerText || ''})"""
            )
            _raise_if_verification_page(
                page_info.get("title", ""), page_info.get("body", "")
            )
            if (
                urlsplit(page.url).netloc.casefold() != "www.whatnot.com"
                or urlsplit(page.url).path.rstrip("/") != ""
            ):
                # Submitting the same query from its previous result page can
                # satisfy URL waits before the new CATEGORY navigation commits.
                # Start from the normal home search UI, not stale result state.
                page.goto(
                    "https://www.whatnot.com/",
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
            page_info = self._read_page(page,
                """() => ({title: document.title, body: document.body?.innerText || ''})"""
            )
            _raise_if_verification_page(
                page_info.get("title", ""), page_info.get("body", "")
            )

            search_box = self._search_box(page)
            if search_box is None:
                # The Whatnot challenge is hydrated after DOMContentLoaded. Give
                # its page text a short chance to appear before reporting a
                # generic missing-search-control error.
                page.wait_for_timeout(1_200)
                page_info = self._read_page(page,
                    """() => ({title: document.title, body: document.body?.innerText || ''})"""
                )
                _raise_if_verification_page(
                    page_info.get("title", ""), page_info.get("body", "")
                )
                search_box = self._search_box(page)
            if search_box is None and urlsplit(page.url).path.rstrip("/") != "":
                page.goto(
                    "https://www.whatnot.com/",
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                page_info = self._read_page(page,
                    """() => ({title: document.title, body: document.body?.innerText || ''})"""
                )
                _raise_if_verification_page(
                    page_info.get("title", ""), page_info.get("body", "")
                )
                search_box = self._search_box(page)
                if search_box is None:
                    page.wait_for_timeout(1_200)
                    page_info = self._read_page(page,
                        """() => ({title: document.title, body: document.body?.innerText || ''})"""
                    )
                    _raise_if_verification_page(
                        page_info.get("title", ""), page_info.get("body", "")
                    )
                    search_box = self._search_box(page)
            if search_box is None:
                raise WhatnotPageChanged(
                    "Whatnot 页面没有可用的搜索框或搜索按钮，未提交关键词。"
                )

            self._check_pending_verification()
            phase = "填写关键词"
            # The page may re-render between selection and action. Recheck the
            # verification page before reselecting; never dismiss challenges.
            try:
                search_box.fill(keyword, timeout=8_000)
            except PlaywrightError:
                # A newly rendered challenge or unreadable page must remain
                # intact before any additional search interaction.
                self._inspect_search_failure_safely(page)
                search_box = self._search_box(page)
                if search_box is None:
                    raise WhatnotPageChanged("搜索框暂时不可编辑，未提交关键词；等待下次扫描。")
                search_box.fill(keyword, timeout=8_000)
            phase = "提交关键词"
            search_box.press("Enter")
            phase = "等待搜索 URL"
            page.wait_for_function(
                """keyword => {
                  const url = new URL(location.href);
                  if (url.pathname.replace(/\\/$/, '') !== '/search') return false;
                  return [...url.searchParams.values()].some(value =>
                    value.trim().toLocaleLowerCase() === keyword.trim().toLocaleLowerCase());
                }""",
                arg=keyword,
                timeout=20_000,
            )
            try:
                page.wait_for_function(
                    _SEARCH_CATEGORY_READY_SCRIPT, timeout=12_000
                )
            except PlaywrightError:
                page_info = self._read_page(page,
                    """() => ({title: document.title, body: document.body?.innerText || ''})"""
                )
                _raise_if_verification_page(
                    page_info.get("title", ""), page_info.get("body", "")
                )

            self._check_pending_verification()
            phase = "选择 Shows 分类"
            live_shows = self._search_control(
                page, r"^(?:live\s+)?shows?$"
            )
            if live_shows is not None:
                live_shows.click()
                # Do not click a stale CATEGORY 'Show all' during router commit.
                phase = "等待 Shows 跳转"
                page.wait_for_function(
                    "() => new URL(location.href).searchParams.get('searchVertical') === 'LIVESTREAM'",
                    timeout=20_000,
                )
                try:
                    page.wait_for_function(
                        _SHOW_ALL_READY_SCRIPT, timeout=12_000
                    )
                except PlaywrightError:
                    page_info = self._read_page(page,
                        """() => ({title: document.title, body: document.body?.innerText || ''})"""
                    )
                    _raise_if_verification_page(
                        page_info.get("title", ""), page_info.get("body", "")
                    )
            elif not self._read_page(page,
                """() => new URL(location.href).searchParams.get('searchVertical') === 'LIVESTREAM'"""
            ):
                page_info = self._read_page(page,
                    """() => ({title: document.title, body: document.body?.innerText || ''})"""
                )
                _raise_if_verification_page(
                    page_info.get("title", ""), page_info.get("body", "")
                )
                raise WhatnotPageChanged(
                    "关键词搜索已提交，但页面未显示 Shows/Live Shows 分类，"
                    "也未进入 LIVESTREAM 结果。"
                )

            self._check_pending_verification()
            phase = "打开全部直播结果"
            show_all = self._search_control(page, r"^show\s+all(?:\s*\(\d+\))?$")
            if show_all is not None:
                show_all.click()
            try:
                page.wait_for_function(_SEARCH_READY_SCRIPT, timeout=15_000)
            except PlaywrightError:
                pass
            self._check_pending_verification()
            phase = "筛选正在直播"
            self._select_live_filter(page)
            phase = "收集直播结果"
            shows = self._collect_search_results(page,
                """() => {
                  const resultCards = [...document.querySelectorAll('main section[data-testid="livestream-card"]')];
                  const anchors = resultCards.length ? resultCards.flatMap(card => [...card.querySelectorAll('a[href]')]) :
                    [...document.querySelectorAll('a[href]')];
                  const seen = new Set();
                  const rows = [];
                  for (const a of anchors) {
                    const href = a.href || '';
                    const path = new URL(href, location.href).pathname;
                    if (!href.startsWith(location.origin) || !/\\/(show|live|shows)\\//i.test(path)) continue;
                    const canonicalHref = new URL(href, location.href);
                    canonicalHref.search = '';
                    canonicalHref.hash = '';
                    const key = canonicalHref.toString().replace(/\\/$/, '');
                    let card = a.closest('section[data-testid="livestream-card"]') || a;
                    const profileLink = (link) =>
                      new URL(link.href, location.href).pathname.split('/')
                        .some(part => ['user', 'users', 'profile', 'seller'].includes(part.toLowerCase()));
                    let sellerLinks = [];
                    for (let ancestor = card, depth = 0;
                         ancestor && depth < 8;
                         ancestor = ancestor.parentElement, depth++) {
                      const links = [...ancestor.querySelectorAll('a[href]')];
                      const showKeys = new Set(links.filter(link =>
                        /\\/(show|live|shows)\\//i.test(new URL(link.href, location.href).pathname)
                      ).map(link => {
                        const value = new URL(link.href, location.href);
                        return value.origin + value.pathname.replace(/\\/$/, '');
                      }));
                      // Never borrow a neighboring card's seller or Live badge.
                      if (showKeys.size > 1) break;
                      card = ancestor;
                      const candidates = links.filter(profileLink);
                      if (candidates.length) {
                        sellerLinks = candidates;
                        break;
                      }
                    }
                    const text = (card?.innerText || a.innerText || '').trim();
                    const liveBadge = /^(?:live(?:\\s*[·•.:\\-]\\s*[\\d,.]+\\s*[kKmM]?)?|live now|正在直播|直播中)$/i;
                    const labels = [...(card?.querySelectorAll('[aria-label],span,a') || [])]
                      .flatMap(el => [el.getAttribute('aria-label') || '',
                        (el.innerText || '').trim()]);
                    const isLive = [...text.split('\\n'), ...labels]
                      .some(value => liveBadge.test(value.trim()));
                    if (!text || !isLive || seen.has(key)) continue;
                    seen.add(key);
                    const seller = sellerLinks[0];
                    const sellerName = (seller?.innerText || seller?.getAttribute('aria-label')
                      || seller?.getAttribute('title') || '').trim();
                    const sameShowLinks = [...(card?.querySelectorAll('a[href]') || [])]
                      .filter(link => {
                        const candidate = new URL(link.href, location.href);
                        candidate.search = '';
                        candidate.hash = '';
                        return candidate.toString().replace(/\\/$/, '') === key;
                      });
                    const isViewerBadge = value =>
                      /^(?:live(?:\\s*[·•.:-]\\s*[\\d,.]+\\s*[kKmM]?)?|[\\d,.]+\\s*(?:live|viewers?|watching))$/i
                        .test((value || '').replace(/\\s+/g, ' ').trim());
                    const showTitle = sameShowLinks.map(link => (link.innerText || '').trim())
                      .find(value => value && !isViewerBadge(value)) || '';
                    const startedAt = card?.querySelector('time[datetime]')?.getAttribute('datetime')
                      || a.querySelector('time[datetime]')?.getAttribute('datetime') || null;
                    rows.push({
                      href, sellerHref: seller?.href || null, sellerName,
                      text: text.slice(0, 1600),
                      title: showTitle || (a.innerText || '').trim(),
                      startedAt
                    });
                  }
                  const emptyState = /no results|no live shows|try another search|there.s nothing here at the moment|没有结果|暂无直播/i.test(document.body.innerText);
                  const candidateLinks = anchors.filter(a => /\\/(show|live|shows)\\//i.test(new URL(a.href, location.href).pathname)).length;
                  return {href: location.href, title: document.title, body: document.body.innerText.slice(0, 3000), rows, candidateLinks, emptyState};
                }""",
                self._search_checkpoint,
            )
            title = shows.get("title", "")
            _raise_if_verification_page(title, shows.get("body", ""))
            if (
                not shows["rows"]
                and not shows.get("emptyState")
                and not shows.get("candidateLinks")
            ):
                raise WhatnotPageChanged(
                    "Whatnot Live Shows/Show all 页面未出现直播卡片或明确空结果状态。"
                )
            found_at = datetime.now(UTC).isoformat()
            output = []
            for row in shows["rows"]:
                url = row["href"].split("?")[0]
                room_id, session_id, identity_source = _show_identity(
                    url, row.get("sellerHref"), found_at
                )
                normalized = room_name_from_card(
                    row.get("sellerName"),
                    row.get("sellerHref"),
                    row.get("title") or row.get("text"),
                )
                viewer_match = re.search(
                    r"(?:live\s+)?viewers?\s*[:\-]?\s*[\d,.]+\s*[kKmM万]?|"
                    r"[\d,.]+\s*[kKmM万]?\s+(?:live\s+)?viewers?|"
                    r"[\d,.]+\s*[kKmM万]?\s+(?:people\s+)?watching(?:\s+now)?|"
                    r"[\d,.]+\s*[kKmM万]?\s*(?:人正在观看|人观看)",
                    row["text"],
                    re.IGNORECASE,
                )
                raw_viewers = viewer_match.group(0) if viewer_match else None
                parsed_viewers = parse_labeled_live_viewers(raw_viewers)
                output.append(
                    LiveShow(
                        session_id=session_id,
                        room_id=room_id,
                        name=normalized[:180],
                        url=url,
                        keyword=keyword,
                        started_at=parse_platform_datetime(row.get("startedAt")),
                        start_time_source=(
                            "platform_started_at"
                            if parse_platform_datetime(row.get("startedAt"))
                            else "first_seen_estimate"
                        ),
                        first_seen_at=found_at,
                        viewers=parsed_viewers,
                        viewers_raw=raw_viewers,
                        identity_basis=identity_source,
                    )
                )
            return output
        except WhatnotPageStateUnknown:
            if page is not None:
                self.mark_page_state_unknown(page)
            raise WhatnotPageStateUnknown("搜索页面状态未知；已保留原页面并暂停导航。") from None
        except WhatnotVerificationRequired as exc:
            if page is not None and exc not in self._blocked_pages.values():
                # A preflight may have found the challenge in a different tab.
                self._remember_block(page, exc)
            raise WhatnotVerificationRequired("Whatnot 安全验证页面已拦截访问。", phase=exc.phase) from None
        except WhatnotPageChanged as exc:
            safe_errors = {
                "Time of Show 筛选已折叠，但没有可用的展开控件。",
                "扫描期间页面离开 LIVESTREAM 结果，不能确认搜索完整。",
                "扫描期间 Live 筛选反复被取消，本轮搜索未完成。",
                "Whatnot 页面没有可用的搜索框或搜索按钮，未提交关键词。",
                "搜索框暂时不可编辑，未提交关键词；等待下次扫描。",
                "关键词搜索已提交，但页面未显示 Shows/Live Shows 分类，也未进入 LIVESTREAM 结果。",
                "Whatnot Live Shows/Show all 页面未出现直播卡片或明确空结果状态。",
            }
            message = str(exc)
            if message not in safe_errors:
                message = f"搜索阶段 {phase} 未完成，结果状态未确认。"
            raise WhatnotPageChanged(message) from None
        except PlaywrightError:
            if page is None:
                raise WhatnotPageStateUnknown("搜索页面不可用，状态未知；已暂停导航。") from None
            try:
                page_closed = page.is_closed()
            except Exception:  # noqa: BLE001 - unknown page state must pause
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("无法检查搜索页面，状态未知；已暂停导航。") from None
            if page_closed:
                self._search_page = None
                raise SearchDiagnosticError(f"搜索阶段 {phase} 失败：页面已关闭。") from None
            # Never navigate while classifying a failure. A verification page
            # or unreadable inspection remains retained and pauses monitoring.
            self._inspect_search_failure_safely(page)
            try:
                diagnostic = bounded_evaluate(page, _SEARCH_ERROR_DIAGNOSTICS)
            except WhatnotPageStateUnknown:
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("搜索页面状态未知；已保留原页面并暂停导航。") from None
            except WhatnotVerificationRequired:
                self._verification_page = page
                raise WhatnotVerificationRequired("Whatnot 安全验证页面已拦截访问。") from None
            except Exception:  # noqa: BLE001 - unknown diagnostic must pause
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("搜索诊断读取失败，页面状态未知；已暂停导航。") from None
            if type(diagnostic) is not dict:
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("搜索诊断结果无效，页面状态未知；已暂停导航。") from None
            kind = diagnostic.get("page_kind")
            host = diagnostic.get("platform_host")
            if type(kind) is not str or kind not in {"home", "search"} or host is not True:
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("搜索页面路径或域名无法确认；已保留原页面并暂停导航。") from None
            mode = diagnostic.get("search_mode")
            safe = {
                "page_kind": kind, "platform_host": True,
                "search_mode": mode if type(mode) is str and mode in {"live", "category", "other_or_missing"} else None,
            }
            for key in ("inputs_total", "inputs_visible", "inputs_enabled", "inputs_readonly", "inputs_editable"):
                value = diagnostic.get(key)
                safe[key] = value if type(value) is int and 0 <= value <= 10000 else None
            raise SearchDiagnosticError(
                f"搜索阶段 {phase} 失败；去敏页面状态："
                f"{json.dumps(safe, ensure_ascii=False)}"
            ) from None
        except Exception:  # noqa: BLE001 - preserve an unclassified page
            if page is not None:
                self.mark_page_state_unknown(page)
            raise WhatnotPageStateUnknown(f"搜索阶段 {phase} 状态未知；已暂停导航。") from None

    def _inspect_document(self, page: Page) -> dict:
        result = self._read_page(page, self.document_script)
        self._navigation_checks.discard(page)
        try:
            _raise_if_verification_page(
                result.get("title", ""), result.pop("body", ""),
                action_visible=bool(result.pop("verificationActionVisible", False)),
            )
        except WhatnotVerificationRequired as exc:
            self._remember_block(page, exc)
            if self._uncertain_page is page:
                self._uncertain_page = None
            raise
        self._blocked_pages.pop(page, None)
        if self._uncertain_page is page:
            self._uncertain_page = None
        if self._verification_page is page:
            self._verification_page = None
        result["viewers"], result["viewers_raw"] = _viewer_observation(
            result.pop("audienceCandidates", None), result.pop("liveHeaderCounters", None)
        )
        return result

    def mark_page_state_unknown(self, page: Page) -> None:
        """Retain an unclassified page; never navigate it until rechecked."""
        if self._uncertain_page is None:
            self._uncertain_page = page
        self._blocked_pages.setdefault(page, WhatnotPageStateUnknown("页面状态未知；已暂停导航"))

    def _check_pending_verification(self, *, current_page: Page | None = None) -> None:
        # A navigation gate is separate from recorder observation. Check every
        # retained challenge, not just the last single-page compatibility pointer.
        pages = list(self._blocked_pages)
        for page in (self._uncertain_page, self._verification_page, *self._navigation_checks):
            if page is not None and page not in pages:
                pages.append(page)
        for page in pages:
            if page.is_closed():
                self._blocked_pages.pop(page, None)
                self._navigation_checks.discard(page)
                if self._uncertain_page is page:
                    self._uncertain_page = None
                if self._verification_page is page:
                    self._verification_page = None
                continue
            self._inspect_document(page)

    def inspect_live(self, url: str) -> dict:
        self._check_pending_verification()
        target = urlsplit(url)
        page = next(
            (
                candidate
                for candidate in getattr(self.context, "pages", ())
                if not candidate.is_closed()
                and urlsplit(candidate.url)._replace(query="", fragment="")
                == target._replace(query="", fragment="")
            ),
            None,
        )
        if page is None:
            page = self._inspection_page
            if page is None or page.is_closed():
                page = self.context.new_page()
                self._inspection_page = page
                if self.reuse_inspection_for_recording:
                    page.add_init_script(ROOM_CAPTURE_INIT_SCRIPT)
            else:
                # The previously normal page can become a challenge after the
                # last inspection returned. Never overwrite that unseen state.
                self._inspect_document(page)
            # Only this privately owned inspection tab may be navigated. Pages
            # borrowed from the context (including recordings) are read-only.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except PlaywrightError:
                # A challenge may have replaced the document before navigation
                # timed out. Classify it without retrying the navigation.
                try:
                    self._inspect_document(page)
                except WhatnotVerificationRequired:
                    raise
                except PlaywrightError:
                    pass
                raise
            page.wait_for_timeout(1200)
        return self._inspect_document(page)

    def recording_page_observation_allowed(self, page: Page) -> bool:
        """Local gate only: never revisit a retained challenge without Start."""
        return (not page.is_closed() and page not in self._blocked_pages
                and page is not self._uncertain_page
                and page is not self._verification_page
                and page not in self._navigation_checks)

    def inspect_page(self, page: Page) -> dict:
        # Another retained/unknown tab must not hide this recorder's document
        # replacement. Only operations that navigate use the global safety gate.
        result = self._inspect_document(page)
        ended = result.pop("ended", False)
        live = result.pop("live", False)
        result["ended_candidate"] = bool(ended and not live)
        return result

    def _enable_room_audio(self, page: Page, requirements: dict, deadline: float) -> None:
        """Enable only this room's player before capture; never toggle sound twice."""
        clicked = False
        while time.monotonic() < deadline:
            status = self._read_page(page, _ROOM_AUDIO_STATUS_SCRIPT)
            _raise_if_verification_page(status.get("title", ""), status.get("body", ""))
            if not clicked and not status.get("audibleElement"):
                for name in (_UNMUTE_CONTROL_RE, _SOUND_CONTROL_RE):
                    # A generic Sound button may toggle an already audible player.
                    if name is _SOUND_CONTROL_RE and status.get("audibleElement"):
                        continue
                    candidates = page.get_by_role("button", name=name)
                    visible = [
                        candidates.nth(index) for index in range(candidates.count())
                        if candidates.nth(index).is_visible()
                        and candidates.nth(index).is_enabled()
                    ]
                    if len(visible) == 1:
                        visible[0].click(timeout=max(1, int((deadline - time.monotonic()) * 1000)))
                        clicked = True
                        break
            # Idempotent fallback also handles players attached/replaced after the
            # sound click. No captureStream calls, extra tabs or system-volume edits.
            page.evaluate(_ROOM_ENABLE_AUDIO_SCRIPT)
            if self._read_page(page, _ROOM_MEDIA_READY_SCRIPT, arg=requirements):
                return
            page.wait_for_timeout(max(1, min(200, (deadline - time.monotonic()) * 1000)))
        raise PlaywrightError("Player sound could not be enabled before the media deadline")

    def new_room_page(
        self,
        url: str,
        *,
        require_audio: bool = True,
        require_video: bool = True,
        media_timeout_ms: int = 30_000,
    ) -> Page:
        self._check_pending_verification()
        page = self._inspection_page
        reuse = (
            self.reuse_inspection_for_recording and page is not None
            and not page.is_closed()
            and urlsplit(page.url)._replace(query="", fragment="")
            == urlsplit(url)._replace(query="", fragment="")
        )
        if reuse:
            self._inspection_page = None
        else:
            page = self.context.new_page()
        try:
            if not reuse:
                page.add_init_script(ROOM_CAPTURE_INIT_SCRIPT)
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(500)
            page_info = self._read_page(page, _ROOM_MEDIA_DIAGNOSTIC_SCRIPT)
            _raise_if_verification_page(
                page_info.get("title", ""), page_info.get("body", "")
            )
            requirements = {
                "requireAudio": require_audio,
                "requireVideo": require_video,
            }
            deadline = time.monotonic() + media_timeout_ms / 1000
            try:
                if require_audio:
                    self._enable_room_audio(page, requirements, deadline)
                page.wait_for_function(
                    _ROOM_MEDIA_READY_SCRIPT,
                    arg=requirements,
                    timeout=max(1, int((deadline - time.monotonic()) * 1000)),
                )
                if require_audio:
                    page.evaluate(_ROOM_KEEP_AUDIO_SCRIPT)
            except PlaywrightError as exc:
                diagnostic = self._read_page(page, _ROOM_MEDIA_DIAGNOSTIC_SCRIPT)
                _raise_if_verification_page(
                    diagnostic.get("title", ""), diagnostic.get("body", "")
                )
                raise WhatnotPageChanged(
                    f"直播播放器在 {media_timeout_ms // 1000} 秒内未提供所需的"
                    "可播放媒体轨道（音频需已开启、非静音且音量大于 0）；页面诊断："
                    f"{json.dumps(diagnostic, ensure_ascii=False)}"
                ) from exc
            return page
        except WhatnotVerificationRequired as exc:
            # Keep the visible challenge page open for the user to complete.
            self._remember_block(page, exc)
            raise
        except Exception:
            # A navigation timeout can itself be caused by a late challenge.
            # Classify the current document before deciding it is safe to close.
            try:
                self._inspect_document(page)
            except WhatnotVerificationRequired:
                raise
            except Exception as exc:  # Unclassified pages must be retained.
                self.mark_page_state_unknown(page)
                raise WhatnotPageStateUnknown("无法确认失败页面的状态；已保留页面并暂停导航。") from exc
            try:
                page.close()
            except PlaywrightError:
                pass
            raise

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            else:
                self.context.close()
        finally:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
