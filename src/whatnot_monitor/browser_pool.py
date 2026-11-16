from __future__ import annotations

import logging
import sqlite3
import time

from playwright.sync_api import sync_playwright

from .accounts import BrowserAccount
from .platform_browser import BROWSER_TYPES
from .whatnot import WhatnotBrowser, WhatnotPageStateUnknown

log = logging.getLogger(__name__)


class AccountBrowserDisconnected(WhatnotPageStateUnknown):
    """A retained account page cannot be inspected because its browser closed."""


class BrowserPool:
    """One Playwright owner and one isolated Chrome context per ready account."""

    def __init__(self, accounts: list[BrowserAccount], *, headless: bool, load_accounts=None):
        self.accounts = accounts
        self._load_accounts = load_accounts
        self._limits = {account.id: account.recording_limit for account in accounts}
        self._keywords = {account.id: account.keyword_id for account in accounts}
        self.headless = headless
        self.service_recordings = None
        self._members: dict[str, WhatnotBrowser] = {}
        self._manager = sync_playwright().start()

    @property
    def capacity(self) -> int:
        return sum(self._limits.values())

    def refresh_account_settings(self) -> None:
        if self._load_accounts is None:
            return
        try:
            saved = {account.id: account for account in self._load_accounts()}
        except (OSError, sqlite3.Error):
            log.warning("Could not read account recording limits; new allocations are paused", exc_info=True)
            saved = {}
        self._limits = {account.id: saved[account.id].recording_limit if account.id in saved else 0
                        for account in self.accounts}
        self._keywords = {account.id: saved[account.id].keyword_id if account.id in saved else None
                          for account in self.accounts}

    def _matching_accounts(self, keyword_ids):
        if keyword_ids is None:
            return self.accounts
        if not keyword_ids:
            return []
        exact = [account for account in self.accounts if self._keywords[account.id] in keyword_ids]
        return exact + [account for account in self.accounts if self._keywords[account.id] is None]

    def can_scan(self, keyword_id: int) -> bool:
        return any(self._limits[account.id] for account in self._matching_accounts({keyword_id}))

    def _free_slots(self, account: BrowserAccount, recorders) -> int:
        member = self._members.get(account.id)
        used = sum(page.context is member.context for _, page in recorders.values()) if member else 0
        return max(0, self._limits[account.id] - used)

    def available_slots(self, recorders) -> int:
        return sum(self._free_slots(account, recorders) for account in self.accounts)

    def has_slot(self, browser: WhatnotBrowser, recorders, *, keyword_ids=None) -> bool:
        return any(
            self._members.get(account.id) is browser and self._free_slots(account, recorders)
            for account in self._matching_accounts(keyword_ids)
        )

    def _open(self, account: BrowserAccount) -> WhatnotBrowser:
        member = self._members.get(account.id)
        if member is not None and member.is_usable():
            return member
        if member is not None and member._uncertain_page is not None:
            raise WhatnotPageStateUnknown("账号的浏览器页面状态未知；请检查后再启动。")
        if not (account.profile / "Local State").is_file():
            raise RuntimeError(f"账号「{account.name}」需要在账号库重新登录。")
        member, _ = BROWSER_TYPES[account.platform].launch_persistent(
            str(account.profile), "chrome", headless=self.headless,
            manager=self._manager, allow_fallback=False,
        )
        member.reuse_inspection_for_recording = True
        member.service_recordings = self.service_recordings
        self._members[account.id] = member
        return member

    def _check_pending_verification(self):
        for account in self.accounts:
            member = self._members.get(account.id)
            if member is None:
                continue
            if member.is_usable():
                member._check_pending_verification()
            elif member._uncertain_page is not None or member._blocked_pages or member._verification_page is not None:
                raise AccountBrowserDisconnected(
                    f"账号「{account.name}」的专用浏览器已关闭或断开，无法复核原页面；"
                    "请安全退出监控程序，在账号库打开该账号并手动处理网站提示后，再重新启动监控。"
                )

    def room_browser(self, recorders, *, keyword_ids=None) -> WhatnotBrowser | None:
        for account in self._matching_accounts(keyword_ids):
            if self._free_slots(account, recorders):
                self._check_pending_verification()
                return self._open(account)
        return None

    def scan_keyword(self, phrase, *, keyword_id: int):
        self.refresh_account_settings()
        account = next((account for account in self._matching_accounts({keyword_id})
                        if self._limits[account.id]), None)
        if account is None:
            return []
        self._check_pending_verification()
        return self._open(account).scan_keyword(phrase)

    def open_login(self):
        self._check_pending_verification()
        return self._open(self.accounts[0]).open_login()

    def _owner(self, page) -> WhatnotBrowser:
        for member in self._members.values():
            if page.context is member.context:
                return member
        raise WhatnotPageStateUnknown("无法确认录制页所属账号；已暂停导航。")

    def account_name(self, page) -> str:
        owner = self._owner(page)
        return next(account.name for account in self.accounts
                    if self._members.get(account.id) is owner)

    def inspect_page(self, page):
        return self._owner(page).inspect_page(page)

    def mark_page_state_unknown(self, page):
        self._owner(page).mark_page_state_unknown(page)

    def recording_page_observation_allowed(self, page):
        return self._owner(page).recording_page_observation_allowed(page)

    @property
    def verification_info(self):
        pending = (
            {**member.verification_info, "verification_account": account.name}
            for account in self.accounts
            if (member := self._members.get(account.id)) and member.verification_info
        )
        return max(
            pending,
            key=lambda info: info.get("verification_observed_at", ""), default={},
        )

    def pump_events(self, milliseconds):
        # All profiles share one driver, so any live context pumps bindings for all.
        for member in self._members.values():
            if member.is_usable() and member.context.pages:
                member.pump_events(milliseconds)
                return
        time.sleep(milliseconds / 1000)

    def is_usable(self):
        # A single disconnected profile must not destroy another account's captures.
        # Its next use is rechecked through the normal ownership and safety gates.
        return True

    def close(self):
        try:
            for member in self._members.values():
                try:
                    member.close()
                except Exception:
                    log.exception("Could not close a Whatnot account browser")
        finally:
            self._manager.stop()
