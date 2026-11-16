"""Recognize only explicit browser live-room URLs on supported hosts."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

# Keep room identities identical in Python and in the capture document.
ROOM_ROUTES = (
    ("whatnot", r"(?:www\.)?whatnot\.com", r"/(?:show|shows|live)/([^/]+)/?"),
    ("tiktok", r"(?:www\.)?tiktok\.com", r"/@([a-z0-9._]+)/live/?"),
    ("douyin", r"live\.douyin\.com", r"/([0-9]+)/?"),
    ("xiaohongshu", r"(?:www\.)?(?:xiaohongshu|rednote)\.com", r"/(?:live|live-room)/([a-z0-9_-]+)/?"),
    ("bilibili", r"live\.bilibili\.com", r"/(?:blanc/)?([0-9]+)/?"),
)


def live_room_identity(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    for platform, host, path in ROOM_ROUTES:
        if re.fullmatch(host, parsed.hostname or "", re.I):
            match = re.fullmatch(path, parsed.path, re.I)
            if match:
                room = match[1].lower()
                return room if platform == "whatnot" else f"{platform}:{room}"
    return None


ROOM_IDENTITY_JS = """const showId = (url) => {
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
    for (const [platform, host, path] of """ + json.dumps(ROOM_ROUTES) + """) {
        if (!new RegExp('^' + host + '$', 'i').test(url.hostname)) continue;
        const match = new RegExp('^' + path + '$', 'i').exec(url.pathname);
        if (match) return platform === 'whatnot' ? match[1].toLowerCase()
            : platform + ':' + match[1].toLowerCase();
    }
    return null;
};"""
