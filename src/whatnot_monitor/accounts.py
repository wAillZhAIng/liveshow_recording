from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .config import APP_ROOT
from .whatnot import WhatnotBrowser

ACCOUNT_DB = APP_ROOT / "config" / "browser-accounts.sqlite3"
ACCOUNT_PROFILES = APP_ROOT / "browser-account-profiles"
DEFAULT_RECORDING_LIMIT = 4
MAX_RECORDING_LIMIT = 100
_manual_browsers: dict[Path, subprocess.Popen] = {}
PLATFORMS = {
    "whatnot": ("Whatnot", "https://www.whatnot.com/login"),
    "tiktok": ("TikTok", "https://www.tiktok.com/login"),
    "douyin": ("抖音", "https://www.douyin.com/"),
    "xiaohongshu": ("小红书", "https://www.xiaohongshu.com/login"),
    "bilibili": ("B站", "https://passport.bilibili.com/login"),
}


@dataclass(frozen=True)
class BrowserAccount:
    id: str
    name: str
    ready: bool
    recording_limit: int = DEFAULT_RECORDING_LIMIT
    keyword_id: int | None = None
    platform: str = "whatnot"

    @property
    def profile(self) -> Path:
        root = ACCOUNT_PROFILES if self.platform == "whatnot" else ACCOUNT_PROFILES / self.platform
        return root / self.id


class AccountLibrary:
    def __init__(self, path: Path | None = None, *, platform: str = "whatnot") -> None:
        if platform not in PLATFORMS:
            raise ValueError("不支持的账号平台。")
        self.platform = platform
        if path is None:
            path = ACCOUNT_DB if platform == "whatnot" else ACCOUNT_DB.with_name(f"browser-accounts-{platform}.sqlite3")
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
                    recording_limit INTEGER NOT NULL DEFAULT {DEFAULT_RECORDING_LIMIT}
                        CHECK (recording_limit BETWEEN 1 AND {MAX_RECORDING_LIMIT}),
                    keyword_id INTEGER
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            if "recording_limit" not in columns:
                db.execute(f"""ALTER TABLE accounts ADD COLUMN recording_limit INTEGER NOT NULL
                            DEFAULT {DEFAULT_RECORDING_LIMIT}
                            CHECK (recording_limit BETWEEN 1 AND {MAX_RECORDING_LIMIT})""")
            if "keyword_id" not in columns:
                db.execute("ALTER TABLE accounts ADD COLUMN keyword_id INTEGER")

    def list(self) -> list[BrowserAccount]:
        with closing(sqlite3.connect(self.path)) as db:
            return [
                BrowserAccount(identifier, name, bool(ready), limit, keyword_id, self.platform)
                for identifier, name, ready, limit, keyword_id in db.execute(
                    "SELECT id, name, ready, recording_limit, keyword_id FROM accounts ORDER BY rowid"
                )
            ]

    def add(self, name: str) -> BrowserAccount:
        name = name.strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("账号备注须为 1 至 60 个字符。")
        account = BrowserAccount(uuid.uuid4().hex, name, False, platform=self.platform)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM accounts").fetchone()[0] >= 10:
                raise ValueError("账号库最多添加 10 个账号。")
            try:
                db.execute("INSERT INTO accounts (id, name) VALUES (?, ?)", (account.id, account.name))
            except sqlite3.IntegrityError:
                raise ValueError("这个账号备注已经存在。") from None
        return account

    def set_ready(self, account: BrowserAccount, ready: bool) -> None:
        if ready:
            require_closed_profile(account.profile)
            if not (account.profile / "Local State").is_file():
                raise ValueError("请先在该账号的专用 Chrome 中完成登录。")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE accounts SET ready = ? WHERE id = ?", (ready, account.id))

    def set_recording_limit(self, account: BrowserAccount, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= MAX_RECORDING_LIMIT:
            raise ValueError(f"单账号录制上限须为 1 至 {MAX_RECORDING_LIMIT} 的整数。")
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute(
                "UPDATE accounts SET recording_limit = ? WHERE id = ?", (limit, account.id)
            )
            if not result.rowcount:
                raise ValueError("该账号已不存在。")

    def set_keyword(self, account: BrowserAccount, keyword_id: int | None) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("UPDATE accounts SET keyword_id = ? WHERE id = ?", (keyword_id, account.id))
            if not result.rowcount:
                raise ValueError("该账号已不存在。")


def require_closed_profile(profile: Path) -> None:
    process = _manual_browsers.get(profile)
    if process is not None:
        if process.poll() is None:
            raise RuntimeError("该账号的专用 Chrome 仍在运行。请退出该浏览器后再试。")
        _manual_browsers.pop(profile, None)
    if sys.platform == "win32":
        # Windows Chrome uses a native mutex instead of a SingletonLock symlink.
        return
    try:
        lock = os.readlink(profile / "SingletonLock")
    except FileNotFoundError:
        return
    try:
        pid = int(lock.rsplit("-", 1)[-1])
        if pid <= 0:
            raise ValueError
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except (PermissionError, ValueError):
        raise RuntimeError("无法确认专用 Chrome 已退出，请关闭对应窗口后再试。") from None
    raise RuntimeError("该账号的专用 Chrome 仍在运行。请退出该浏览器后再试。")


def open_manual_login(account: BrowserAccount) -> None:
    require_closed_profile(account.profile)
    arguments = [
        f"--user-data-dir={account.profile}", "--no-first-run",
        "--no-default-browser-check", PLATFORMS[account.platform][1],
    ]
    if sys.platform == "darwin":
        browser_app = Path("/Applications/Google Chrome.app")
        if not browser_app.is_dir():
            raise RuntimeError("请先将 Google Chrome 安装到「应用程序」。")
        account.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["/usr/bin/open", "-n", "-a", str(browser_app), "--args", *arguments], check=True)
        return

    executable = WhatnotBrowser._find_installed_browser("chrome")
    if executable is None:
        raise RuntimeError("找不到 Google Chrome，请先安装浏览器。")
    account.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    _manual_browsers[account.profile] = subprocess.Popen(
        [executable, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
