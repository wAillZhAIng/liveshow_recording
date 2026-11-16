from __future__ import annotations

import sqlite3
import subprocess
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .accounts import AccountLibrary, MAX_RECORDING_LIMIT, PLATFORMS, open_manual_login
from .config import APP_ROOT
from .store import Store


class AccountPane(ttk.Frame):
    def __init__(self, parent, *, list_keywords, platform="whatnot", monitor_running=lambda: False):
        super().__init__(parent, padding=16)
        self.library = AccountLibrary(platform=platform)
        self.list_keywords = list_keywords
        self.monitor_running = monitor_running
        ttk.Label(
            self, text=f"在专用 Chrome 完成 {PLATFORMS[platform][0]} 登录，退出浏览器后点「确认已登录」。"
        ).pack(anchor="w", pady=(0, 8))
        recording_help = (
            "录制上限保存后生效，所有账号的总并发仍受「设置」限制。"
            if platform == "whatnot" else "在主窗口选择此平台后启动，使用本页已确认登录的账号。"
        )
        ttk.Label(self, text=recording_help).pack(anchor="w", pady=(0, 8))
        self.rows = ttk.Treeview(self, columns=("name", "state", "limit", "keyword"), show="headings", selectmode="browse")
        self.rows.heading("name", text="账号备注")
        self.rows.heading("state", text="状态")
        self.rows.heading("limit", text="录制上限")
        self.rows.column("limit", width=100, stretch=False)
        self.rows.heading("keyword", text="录制关键词")
        self.rows.pack(fill="both", expand=True)
        limits = ttk.Frame(self)
        limits.pack(anchor="w", pady=(8, 0))
        ttk.Label(limits, text="单账号录制上限").pack(side="left")
        self.recording_limit = tk.StringVar(master=self)
        ttk.Entry(limits, textvariable=self.recording_limit, width=6).pack(side="left", padx=6)
        ttk.Button(limits, text="保存上限", command=self.save_recording_limit).pack(side="left")
        keywords = ttk.Frame(self)
        keywords.pack(anchor="w", pady=(8, 0))
        ttk.Label(keywords, text="录制关键词").pack(side="left")
        self.keyword_box = ttk.Combobox(keywords, state="readonly", postcommand=self.refresh_keyword_choices)
        self.keyword_box.pack(side="left", padx=6)
        ttk.Button(keywords, text="保存关键词", command=self.save_keyword).pack(side="left")
        self.rows.bind("<<TreeviewSelect>>", self.show_account_settings)
        buttons = ttk.Frame(self)
        buttons.pack(anchor="w", pady=(8, 0))
        for text, command in (
            ("新增并登录", self.add),
            ("打开登录", self.login),
            ("确认已登录", lambda: self.set_ready(True)),
            ("停用", lambda: self.set_ready(False)),
            ("刷新", self.refresh),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))
        self.refresh()

    def refresh(self):
        selected = self.rows.selection()
        self.rows.delete(*self.rows.get_children())
        self.accounts = {account.id: account for account in self.library.list()}
        self.keyword_labels = {keyword.id: keyword.phrase for keyword in self.list_keywords()}
        self.refresh_keyword_choices()
        for account in self.accounts.values():
            self.rows.insert("", "end", iid=account.id, values=(
                account.name, "已确认登录" if account.ready else "待登录或已停用", account.recording_limit,
                self.keyword_labels.get(account.keyword_id, "全部启用关键词" if account.keyword_id is None else "关键词已删除"),
            ))
        if selected and selected[0] in self.accounts:
            self.rows.selection_set(selected[0])
        self.show_account_settings()

    def refresh_keyword_choices(self):
        self.keyword_choices = [None]
        labels = ["全部启用关键词"]
        for keyword in self.list_keywords():
            if keyword.enabled:
                self.keyword_choices.append(keyword.id)
                labels.append(f"关键词：{keyword.phrase}")
        self.keyword_box.configure(values=labels)

    def show_account_settings(self, _event=None):
        selected = self.rows.selection()
        account = self.accounts.get(selected[0]) if selected else None
        self.recording_limit.set(str(account.recording_limit) if account else "")
        if account is None:
            self.keyword_box.set("")
        elif account.keyword_id in self.keyword_choices:
            self.keyword_box.current(self.keyword_choices.index(account.keyword_id))
        else:
            self.keyword_box.set("关键词已停用或删除")

    def save_keyword(self):
        def action():
            index = self.keyword_box.current()
            if index < 0:
                raise ValueError("请选择要录制的关键词。")
            keyword_id = self.keyword_choices[index]
            if keyword_id is not None and not any(
                keyword.id == keyword_id and keyword.enabled for keyword in self.list_keywords()
            ):
                raise ValueError("该关键词已停用或删除，请重新选择。")
            self.library.set_keyword(self.selected(), keyword_id)
        self.perform(action)

    def save_recording_limit(self):
        try:
            account = self.selected()
            try:
                limit = int(self.recording_limit.get().strip())
            except ValueError:
                raise ValueError(f"单账号录制上限须为 1 至 {MAX_RECORDING_LIMIT} 的整数。") from None
            self.library.set_recording_limit(account, limit)
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror("账号库", str(exc), parent=self)
            return
        self.refresh()

    def perform(self, action):
        try:
            action()
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            messagebox.showerror("账号库", str(exc), parent=self)
        finally:
            self.refresh()

    def selected(self):
        selected = self.rows.selection()
        if not selected or selected[0] not in self.accounts:
            raise ValueError("请先选择一个账号。")
        return self.accounts[selected[0]]

    def add(self):
        name = simpledialog.askstring("新增账号", "输入用于区分账号的备注", parent=self)
        if name is not None:
            self.perform(lambda: open_manual_login(self.library.add(name)))

    def login(self):
        def action():
            account = self.selected()
            if account.ready and self.monitor_running():
                raise RuntimeError("请先退出监控窗口，再重新登录已启用的账号。")
            open_manual_login(account)
        self.perform(action)

    def set_ready(self, ready):
        self.perform(lambda: self.library.set_ready(self.selected(), ready))


class PlatformAccountTabs(ttk.Notebook):
    def __init__(self, parent, *, list_keywords, monitor_running=lambda _platform: False):
        super().__init__(parent)
        self.panes = {}
        for platform, (label, _) in PLATFORMS.items():
            pane = AccountPane(
                self, list_keywords=list_keywords, platform=platform,
                monitor_running=lambda platform=platform: monitor_running(platform),
            )
            self.panes[platform] = pane
            self.add(pane, text=label)


def main():
    root = tk.Tk()
    root.title("Whatnot 账号库")
    root.geometry("680x420")
    with Store(APP_ROOT / "database" / "whatnot.sqlite3") as store:
        PlatformAccountTabs(root, list_keywords=store.list_keywords).pack(fill="both", expand=True)
        root.mainloop()


if __name__ == "__main__":
    main()
