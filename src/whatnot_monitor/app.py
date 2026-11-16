from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import tkinter as tk
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import ClassVar

from .config import APP_ROOT, load_settings
from .runtime import MonitorRuntime
from .store import Store


class MonitorApp:
    STATUS_LABELS: ClassVar[dict[str, str]] = {
        "WAITING": "等待录制",
        "CAPACITY_INSUFFICIENT": "容量不足",
        "RECORDING": "录制中",
        "AUDIENCE_PENDING": "等待观众数核验",
        "TRANSCRIBING": "转写中",
        "UPLOADING": "上传中",
        "COMPLETED": "已完成",
        "FAILED": "失败",
        "INTERRUPTED": "已中断",
        "RECOVERY_CHECK": "待恢复核验",
        "recording_failed": "录制失败",
        "cleanup_failed": "清理失败",
    }
    QUALIFICATION_LABELS: ClassVar[dict[str, str]] = {
        "qualified": "已达标",
        "rejected_low_viewers": "未达标（观众数不足）",
        "low_viewers_deferred": "低人数已核实（保护暂停，待复核清理）",
        "viewer_check_failed": "观众数核验失败",
        "pending": "待核验",
        "legacy": "历史状态",
    }
    TASK_STATE_LABELS: ClassVar[dict[str, str]] = {
        "COMPLETED": "已完成",
        "IN_PROGRESS": "进行中",
        "NOT_STARTED": "未开始",
        "FAILED": "失败",
        "SKIPPED": "已跳过",
        "NEVER": "尚未扫描",
        "SCANNING": "扫描中",
        "OK": "成功",
        "EMPTY": "无结果",
        "ERROR": "失败",
        "CANCELLED": "已取消",
        "PARTIAL_ERROR": "部分扫描失败",
        "VERIFICATION_REQUIRED": "安全验证拦截，扫描已暂停",
        "PAGE_STATE_UNKNOWN": "页面状态未知，扫描已暂停",
        "STARTING": "正在连接 Whatnot",
        "OPENING_LOGIN": "正在打开 Whatnot 页面",
        "LOGIN_READY": "等待手动登录或验证",
        "IDLE": "空闲",
        "RUNNING": "运行中",
    }
    STATUS_FILTER_VALUES: ClassVar[dict[str, str | None]] = {
        "全部": None,
        "录制中": "RECORDING",
        "容量不足": "CAPACITY_INSUFFICIENT",
        "等待录制": "WAITING",
        "转写中": "TRANSCRIBING",
        "上传中": "UPLOADING",
        "已完成": "COMPLETED",
        "失败": "FAILED",
        "已中断": "INTERRUPTED",
    }

    @classmethod
    def _display_state(cls, value: object) -> str:
        raw = str(value or "")
        return cls.STATUS_LABELS.get(
            raw,
            cls.QUALIFICATION_LABELS.get(
                raw,
                cls.TASK_STATE_LABELS.get(raw, cls.TASK_STATE_LABELS.get(raw.upper(), raw)),
            ),
        )

    @classmethod
    def _runtime_status_text(cls, runtime: MonitorRuntime) -> str:
        if not runtime.running:
            return "已停止"
        scan = runtime.scan_status
        state = str(scan.get("state", "idle")).casefold()
        if state == "verification_required":
            phase = scan.get("verification_phase", "unknown")
            if phase == "loading":
                return "安全验证加载中；尚未检测到可操作控件"
            if phase == "action_visible":
                return "安全验证待手动处理；自动导航已暂停"
            return "Whatnot 安全验证待完成"
        if state == "page_state_unknown":
            return "页面状态未知；自动导航已暂停，请检查浏览器"
        if state == "error":
            return "连接或扫描失败；请查看事件日志"
        if state == "partial_error":
            return "部分关键词扫描失败；请查看事件日志"
        if state in {"starting", "opening_login", "login_ready", "scanning"}:
            return cls._display_state(state)
        if runtime.paused:
            return "已暂停；现有录制将继续"
        return "运行中"

    @staticmethod
    def _display_scan_result(value: str | None) -> str:
        if not value:
            return ""
        match = re.fullmatch(r"(\d+) live shows", value)
        if match:
            return f"{match.group(1)} 个直播间"
        if value.startswith("Scan failed: "):
            return f"扫描失败：{value.removeprefix('Scan failed: ')}"
        return value

    @staticmethod
    def _display_event_level(level: str) -> str:
        return {
            "DEBUG": "调试",
            "INFO": "信息",
            "WARNING": "警告",
            "ERROR": "错误",
            "CRITICAL": "严重错误",
        }.get(level.upper(), level)

    @staticmethod
    def _display_event_component(component: str) -> str:
        return {
            "audience": "观众核验",
            "feishu": "飞书",
            "recovery": "恢复",
            "recording": "录制",
            "storage": "存储",
            "store": "数据库",
            "whatnot": "Whatnot 扫描",
        }.get(component.casefold(), component)

    @staticmethod
    def _display_feishu_message(message: str) -> str:
        if "尚未配置飞书凭据" in message:
            return "尚未配置飞书凭据。"
        if "飞书启动检查失败" in message:
            return message.replace("飞书启动检查失败", "飞书连接检查失败", 1)
        if "credentials are not configured" in message:
            return "尚未配置飞书凭据。"
        if "Feishu startup check failed" in message:
            detail = message.partition("Feishu startup check failed")[2].lstrip(": ")
            return f"飞书启动检查失败：{detail}"
        return message

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        Path(self.settings.recordings_dir).mkdir(parents=True, exist_ok=True)
        self.store = Store(APP_ROOT / "database" / "whatnot.sqlite3")
        self.runtime = MonitorRuntime(self.store, self.settings)
        self._refresh_after_id = None
        root.title("Whatnot 直播监控")
        root.geometry("1480x900")
        root.minsize(1120, 720)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh()

    def _build(self):
        style = ttk.Style()
        style.configure("Treeview", rowheight=28)
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))

        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Whatnot 直播监控", style="Title.TLabel").pack(
            side="left"
        )
        self.status_var = tk.StringVar(value="已停止")
        ttk.Label(header, textvariable=self.status_var).pack(side="right", padx=(12, 0))
        self.start_button = ttk.Button(header, text="启动", command=self.start)
        self.start_button.pack(side="right")
        ttk.Button(header, text="暂停", command=self.pause).pack(side="right", padx=6)
        from .accounts import PLATFORMS

        self.platform_var = tk.StringVar(master=self.root, value=PLATFORMS["whatnot"][0])
        self.platform_box = ttk.Combobox(
            header, textvariable=self.platform_var, state="readonly", width=12,
            values=[label for label, _ in PLATFORMS.values()],
        )
        self.platform_box.pack(side="right", padx=(4, 6))
        ttk.Label(header, text="监控平台").pack(side="right")

        self.stats = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        self.stats.pack(fill="x")
        self.stat_vars = {}
        for key, label in (
            ("keywords", "已启用关键词"),
            ("live", "活跃直播场次"),
            ("recording", "录制中"),
            ("transcribing", "转写中"),
            ("uploading", "待上传飞书"),
            ("completed", "今日已完成"),
            ("failed", "失败"),
        ):
            block = ttk.LabelFrame(self.stats, text=label, padding=(10, 6))
            block.pack(side="left", fill="both", expand=True, padx=(0, 8))
            value = tk.StringVar(value="0")
            ttk.Label(block, textvariable=value, font=("Segoe UI", 16, "bold")).pack(
                anchor="w"
            )
            self.stat_vars[key] = value

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        overview = ttk.Frame(tabs, padding=8)
        keywords = ttk.Frame(tabs, padding=8)
        recordings = ttk.Frame(tabs, padding=8)
        events = ttk.Frame(tabs, padding=8)
        feishu = ttk.Frame(tabs, padding=16)
        settings_page = ttk.Frame(tabs)
        tabs.add(overview, text="直播间总览")
        tabs.add(keywords, text="关键词管理")
        tabs.add(recordings, text="录音与转写")
        tabs.add(events, text="事件日志")
        tabs.add(feishu, text="飞书")
        from .accounts_ui import PlatformAccountTabs

        account_tabs = PlatformAccountTabs(
            tabs, list_keywords=self.store.list_keywords,
            monitor_running=lambda platform: self.runtime.running and self.runtime.platform == platform,
        )
        self.account_panes = account_tabs.panes
        tabs.add(account_tabs, text="账号库")
        tabs.add(settings_page, text="设置")

        settings_actions = ttk.Frame(settings_page, padding=(12, 6))
        settings_actions.pack(side="bottom", fill="x")
        ttk.Button(settings_actions, text="保存设置", command=self.save_settings).pack(
            side="right"
        )
        settings_canvas = tk.Canvas(settings_page, highlightthickness=0)
        settings_scrollbar = ttk.Scrollbar(
            settings_page, orient="vertical", command=settings_canvas.yview
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_canvas.pack(side="left", fill="both", expand=True)
        settings_scrollbar.pack(side="right", fill="y")
        settings_tab = ttk.Frame(settings_canvas, padding=12)
        settings_window = settings_canvas.create_window(
            (0, 0), window=settings_tab, anchor="nw"
        )
        settings_tab.bind(
            "<Configure>",
            lambda _event: settings_canvas.configure(
                scrollregion=settings_canvas.bbox("all")
            ),
        )
        settings_canvas.bind(
            "<Configure>",
            lambda event: settings_canvas.itemconfigure(
                settings_window, width=event.width
            ),
        )

        self.scan_status_var = tk.StringVar(value="扫描轮次：空闲")
        ttk.Label(overview, textvariable=self.scan_status_var).pack(
            anchor="w", pady=(0, 8)
        )
        self.room_filter_name = tk.StringVar()
        self.room_filter_keyword = tk.StringVar(value="全部")
        self.room_filter_status = tk.StringVar(value="全部")
        self.room_filter_after = tk.StringVar()
        self.room_filter_before = tk.StringVar()
        filters = ttk.Frame(overview)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="直播间名称").pack(side="left")
        ttk.Entry(filters, textvariable=self.room_filter_name, width=20).pack(
            side="left", padx=(4, 10)
        )
        ttk.Label(filters, text="关键词").pack(side="left")
        self.room_keyword_filter = ttk.Combobox(
            filters, textvariable=self.room_filter_keyword, width=20, state="readonly"
        )
        self.room_keyword_filter.pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="录制状态").pack(side="left")
        ttk.Combobox(
            filters,
            textvariable=self.room_filter_status,
            values=(
                "全部",
                "录制中",
                "容量不足",
                "等待录制",
                "转写中",
                "上传中",
                "已完成",
                "失败",
                "已中断",
            ),
            width=22,
            state="readonly",
        ).pack(side="left", padx=(4, 10))
        from .date_picker import DatePicker

        ttk.Label(filters, text="开始日期").pack(side="left")
        DatePicker(filters, variable=self.room_filter_after).pack(side="left", padx=(4, 10))
        ttk.Label(filters, text="结束日期").pack(side="left")
        DatePicker(filters, variable=self.room_filter_before).pack(side="left", padx=(4, 10))
        filter_actions = ttk.Frame(overview)
        filter_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(filter_actions, text="筛选", command=self.refresh).pack(side="left")
        ttk.Button(filter_actions, text="重置筛选", command=self.clear_room_filters).pack(
            side="left", padx=4
        )
        ttk.Button(filter_actions, text="清除", command=self.clear_room_history).pack(
            side="left", padx=4
        )
        ttk.Label(filter_actions, text="清除全部非录制中历史及对应本地文件，不受当前筛选影响；保留录制中任务。").pack(
            side="left", padx=8
        )

        self.monitor_summary_var = tk.StringVar(value="直播监控摘要")
        ttk.Label(
            overview,
            textvariable=self.monitor_summary_var,
            wraplength=1420,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 8))

        ttk.Label(
            overview,
            text="新录制均从本程序录制开始计时核验，采用该场开始时保存的间隔和门槛；实际采样时间另列。",
            wraplength=1400,
        ).pack(anchor="w", fill="x", pady=(0, 6))

        session_columns = (
            "name",
            "audience",
            "audience_captured",
            "room_id",
            "session_id",
            "url",
            "keyword",
            "started",
            "discovered",
            "recording_started",
            "viewers",
            "qualification",
            "status",
            "duration",
            "video_size",
            "audio_size",
            "video",
            "audio",
            "transcript_status",
            "upload_status",
            "ended",
        )
        session_body = ttk.Frame(overview)
        session_body.pack(fill="both", expand=True)
        self.session_tree = ttk.Treeview(
            session_body,
            columns=session_columns,
            show="headings",
            selectmode="extended",
        )
        for col, title, width in (
            ("name", "直播间名称", 170),
            ("room_id", "直播间 ID", 145),
            ("session_id", "直播场次 ID", 180),
            ("url", "直播间链接", 240),
            ("keyword", "来源关键词", 220),
            ("started", "开播时间", 160),
            ("discovered", "首次发现时间", 160),
            ("recording_started", "开始录制时间", 160),
            ("viewers", "当前观众数", 85),
            ("audience", self._audience_column_title(), 185),
            ("audience_captured", "人数实际采集时间", 205),
            ("qualification", "筛选结果", 165),
            ("status", "录制状态", 170),
            ("duration", "录制时长", 130),
            ("video_size", "视频大小", 90),
            ("audio_size", "音频大小", 90),
            ("video", "视频文件", 280),
            ("audio", "音频文件", 280),
            ("transcript_status", "转写状态", 120),
            ("upload_status", "飞书状态", 120),
            ("ended", "直播结束状态", 155),
        ):
            self.session_tree.heading(col, text=title)
            self.session_tree.column(
                col,
                width=width,
                anchor="w",
                stretch=(col in {"name", "url", "video", "audio"}),
            )
        session_y = ttk.Scrollbar(
            session_body, orient="vertical", command=self.session_tree.yview
        )
        session_x = ttk.Scrollbar(
            session_body, orient="horizontal", command=self.session_tree.xview
        )
        self.session_tree.configure(
            yscrollcommand=session_y.set, xscrollcommand=session_x.set
        )
        session_body.rowconfigure(0, weight=1)
        session_body.columnconfigure(0, weight=1)
        self.session_tree.grid(row=0, column=0, sticky="nsew")
        session_y.grid(row=0, column=1, sticky="ns")
        session_x.grid(row=1, column=0, sticky="ew")
        # Preserve the existing action handlers while making this the unified room pool.
        session_actions = ttk.Frame(overview)
        session_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            session_actions, text="打开直播间", command=self.open_selected_room
        ).pack(side="left")
        ttk.Button(
            session_actions, text="打开音频", command=self.open_selected_audio
        ).pack(side="left", padx=6)
        ttk.Button(
            session_actions, text="打开视频", command=self.open_selected_video
        ).pack(side="left")
        ttk.Button(
            session_actions, text="查看详情", command=self.show_selected_details
        ).pack(side="left")
        ttk.Button(
            session_actions, text="停止所选录制", command=self.stop_selected
        ).pack(side="left")
        ttk.Button(session_actions, text="刷新", command=self.refresh).pack(
            side="right"
        )

        self.single_keyword_var = tk.StringVar()
        single = ttk.Frame(keywords)
        single.pack(fill="x", pady=(0, 8))
        ttk.Label(single, text="添加单个关键词").pack(side="left")
        single_entry = ttk.Entry(single, textvariable=self.single_keyword_var, width=48)
        single_entry.pack(side="left", padx=6)
        single_entry.bind("<Return>", lambda _event: self.add_keyword())
        ttk.Button(single, text="添加", command=self.add_keyword).pack(side="left")

        keyword_columns = (
            "phrase",
            "enabled",
            "created",
            "last",
            "result",
            "discovered",
            "recording",
            "scan_status",
        )
        keyword_body = ttk.Frame(keywords)
        keyword_body.pack(fill="both", expand=True)
        self.keyword_tree = ttk.Treeview(
            keyword_body,
            columns=keyword_columns,
            show="headings",
            height=12,
            selectmode="extended",
        )
        for col, title, width in (
            ("phrase", "搜索关键词", 235),
            ("enabled", "已启用", 75),
            ("created", "创建时间", 155),
            ("last", "最近扫描时间", 155),
            ("result", "最近扫描结果", 220),
            ("discovered", "发现直播间数", 95),
            ("recording", "当前录制数", 110),
            ("scan_status", "扫描状态", 115),
        ):
            self.keyword_tree.heading(col, text=title)
            self.keyword_tree.column(
                col, width=width, anchor="w", stretch=(col == "result")
            )
        keyword_y = ttk.Scrollbar(
            keyword_body, orient="vertical", command=self.keyword_tree.yview
        )
        keyword_x = ttk.Scrollbar(
            keyword_body, orient="horizontal", command=self.keyword_tree.xview
        )
        self.keyword_tree.configure(
            yscrollcommand=keyword_y.set, xscrollcommand=keyword_x.set
        )
        self.keyword_tree.pack(side="left", fill="both", expand=True)
        keyword_y.pack(side="right", fill="y")
        keyword_x.pack(side="bottom", fill="x")
        actions = ttk.Frame(keywords)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="修改关键词", command=self.rename_keyword).pack(
            side="left"
        )
        ttk.Button(
            actions,
            text="启用所选",
            command=lambda: self.set_selected_keywords(True),
        ).pack(side="left", padx=4)
        ttk.Button(
            actions,
            text="停用所选",
            command=lambda: self.set_selected_keywords(False),
        ).pack(side="left")
        ttk.Button(actions, text="切换启停", command=self.toggle_keyword).pack(
            side="left", padx=4
        )
        ttk.Button(
            actions, text="删除所选", command=self.delete_selected_keywords
        ).pack(side="left")
        ttk.Button(
            actions, text="立即扫描所选", command=self.scan_selected_keyword
        ).pack(side="right", padx=(4, 0))
        ttk.Button(actions, text="扫描全部启用项", command=self.scan_now).pack(
            side="right"
        )

        batch = ttk.LabelFrame(keywords, text="批量添加", padding=8)
        batch.pack(fill="x", pady=(8, 0))
        self.batch_text = tk.Text(batch, height=4, wrap="word")
        self.batch_text.pack(side="left", fill="x", expand=True)
        ttk.Button(batch, text="批量添加", command=self.add_keywords_batch).pack(
            side="left", padx=(8, 0), anchor="n"
        )
        ttk.Label(
            batch,
            text="每行一个关键词，也可使用中英文逗号或分号分隔。",
        ).pack(side="left", padx=8, anchor="n")

        self.recording_tree = ttk.Treeview(
            recordings,
            columns=("room", "status", "audio", "transcript"),
            show="headings",
        )
        for col, title, width in (
            ("room", "直播间", 230),
            ("status", "状态", 140),
            ("audio", "音频文件", 350),
            ("transcript", "转写文本", 350),
        ):
            self.recording_tree.heading(col, text=title)
            self.recording_tree.column(col, width=width, anchor="w", stretch=True)
        self.recording_tree.pack(fill="both", expand=True)
        ttk.Button(
            recordings,
            text="打开所选转写文本",
            command=self.open_selected_transcript,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Button(
            recordings,
            text="重试所选任务",
            command=self.retry_selected,
        ).pack(anchor="w", pady=(8, 0))

        from .config import has_feishu_secret

        ttk.Label(feishu, text="多维表格", style="Title.TLabel").pack(anchor="w")
        feishu_form = ttk.Frame(feishu)
        feishu_form.pack(fill="x", anchor="w", pady=(12, 4))
        self.feishu_vars = {}
        for row, (key, label) in enumerate(
            (
                ("feishu_app_token", "应用 Token"),
                ("feishu_table_id", "数据表 ID"),
                ("feishu_app_id", "App ID"),
                ("secret", "App Secret"),
            )
        ):
            ttk.Label(feishu_form, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6
            )
            value = (
                ("***" if has_feishu_secret(self.settings) else "")
                if key == "secret"
                else getattr(self.settings, key, "")
            )
            variable = tk.StringVar(value=value)
            entry = ttk.Entry(
                feishu_form,
                textvariable=variable,
                show="*" if key == "secret" else "",
                width=72,
            )
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            if key == "secret":
                entry.bind(
                    "<FocusIn>",
                    lambda _event, var=variable: var.set("") if var.get() == "***" else None,
                )
                entry.bind(
                    "<FocusOut>",
                    lambda _event, var=variable: (
                        var.set("***")
                        if not var.get() and has_feishu_secret(self.settings)
                        else None
                    ),
                )
            self.feishu_vars[key] = variable
        feishu_form.columnconfigure(1, weight=1)
        ttk.Label(
            feishu,
            text="密钥留空或保持掩码时保留已保存的密钥；输入新值可更换。",
            wraplength=800,
        ).pack(anchor="w", pady=2)
        ttk.Button(feishu, text="保存飞书配置", command=self.save_feishu_settings).pack(
            anchor="w", pady=(8, 0)
        )
        self.feishu_status = tk.StringVar(value="尚未检查")
        ttk.Label(feishu, textvariable=self.feishu_status).pack(
            anchor="w", pady=(12, 8)
        )
        ttk.Button(
            feishu, text="测试连接和数据表结构", command=self.test_feishu
        ).pack(anchor="w")

        self.event_text = tk.Text(events, height=22, state="disabled", wrap="word")
        self.event_text.pack(fill="both", expand=True)
        ttk.Button(events, text="刷新事件", command=self.refresh).pack(
            anchor="e", pady=(8, 0)
        )

        self.setting_vars = {}
        setting_rows = (
            ("poll_interval_seconds", "扫描间隔（秒）", "int", 300),
            ("video_recording_enabled", "启用视频录制", "bool", True),
            (
                "audio_recording_enabled",
                "启用独立音频录制",
                "bool",
                True,
            ),
            ("qualification_minutes", "录制后核验观众数（分钟；超时保留文件）", "int", 30),
            ("minimum_viewers", "达标所需最低观众数", "int", 100),
            ("audience_retry_count", "观众数核验重试次数", "int", 3),
            (
                "audience_retry_interval_seconds",
                "观众数重试间隔（秒）",
                "int",
                30,
            ),
            (
                "max_concurrent_recordings",
                "最大并发录制数",
                "capacity",
                10,
            ),
            (
                "video_dir",
                "视频保存目录",
                "str",
                str(APP_ROOT / "data" / "recordings"),
            ),
            ("audio_dir", "音频保存目录", "str", str(APP_ROOT / "data" / "audio")),
            (
                "transcripts_dir",
                "转写文本目录",
                "str",
                str(APP_ROOT / "transcripts"),
            ),
            ("asr_model_path", "Whisper 模型名称或本地路径", "str", ""),
            ("asr_device", "语音识别设备（auto/cpu/cuda）", "str", "auto"),
            (
                "video_mime_type",
                "视频编码格式 / MIME 类型",
                "codec",
                "video/webm;codecs=vp8,opus",
            ),
            ("video_bitrate_bps", "视频质量（码率，bit/s）", "bitrate", 2500000),
            ("max_recording_size_gib", "单场录制最大文件大小（GiB）", "float", 20.0),
            ("disk_warning_gib", "磁盘空间不足预警阈值（GiB）", "float", 10.0),
            ("audio_sample_rate", "WAV 采样率（Hz）", "int", 16000),
            ("audio_channels", "WAV 声道数（1 或 2）", "int", 1),
        )
        self.setting_types = {}
        for row, (key, label, value_type, default) in enumerate(setting_rows):
            ttk.Label(settings_tab, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6
            )
            current = getattr(self.settings, key, default)
            if value_type == "bool":
                variable = tk.BooleanVar(value=bool(current))
                ttk.Checkbutton(settings_tab, variable=variable).grid(
                    row=row, column=1, sticky="w", pady=6
                )
            elif value_type == "capacity":
                variable = tk.StringVar(value=str(current))
                ttk.Combobox(
                    settings_tab,
                    textvariable=variable,
                    values=("2", "5", "10", "20"),
                    state="normal",
                    width=68,
                ).grid(row=row, column=1, sticky="ew", pady=6)
            elif value_type == "codec":
                variable = tk.StringVar(value=str(current))
                ttk.Combobox(
                    settings_tab,
                    textvariable=variable,
                    values=(
                        "video/webm;codecs=vp8,opus",
                        "video/webm;codecs=vp9,opus",
                        "video/webm",
                    ),
                    state="normal",
                    width=68,
                ).grid(row=row, column=1, sticky="ew", pady=6)
            elif value_type == "bitrate":
                variable = tk.StringVar(value=str(current))
                ttk.Combobox(
                    settings_tab,
                    textvariable=variable,
                    values=("1000000", "2500000", "4000000", "6000000"),
                    state="normal",
                    width=68,
                ).grid(row=row, column=1, sticky="ew", pady=6)
            else:
                variable = tk.StringVar(value=str(current))
                ttk.Entry(settings_tab, textvariable=variable, width=72).grid(
                    row=row, column=1, sticky="ew", pady=6
                )
            self.setting_vars[key] = variable
            self.setting_types[key] = value_type
        settings_tab.columnconfigure(1, weight=1)
        status_row = len(setting_rows)
        self.capacity_status_var = tk.StringVar(value="录制容量：0 / 10")
        ttk.Label(settings_tab, textvariable=self.capacity_status_var).grid(
            row=status_row, column=0, columnspan=2, sticky="w", pady=(14, 4)
        )
        self.audio_test_status_var = tk.StringVar(
            value="Chromium 合成音频隔离测试：2、5、10 路通过。真实 Whatnot 播放器尚未验证。"
        )
        ttk.Label(settings_tab, text="音频隔离测试状态").grid(
            row=status_row + 1, column=0, sticky="w", padx=(0, 12), pady=4
        )
        ttk.Label(settings_tab, textvariable=self.audio_test_status_var).grid(
            row=status_row + 1, column=1, sticky="w", pady=4
        )
        ttk.Label(
            settings_tab,
            text="飞书连接信息可在“飞书”页面配置并保存。",
        ).grid(row=status_row + 2, column=0, columnspan=2, sticky="w", pady=(10, 6))

    def _selected_keyword(self):
        selected = self.keyword_tree.selection()
        return int(selected[0]) if selected else None

    def _selected_keywords(self):
        return [int(item) for item in self.keyword_tree.selection()]

    def _selected_session(self):
        selected = self.session_tree.selection()
        return selected[0] if selected else None

    def _recording_status_display(self, session):
        status = str(session.status)
        if status == "RECORDING":
            if not self.runtime.running:
                status = "RECOVERY_CHECK"
            elif self.runtime.recording_health_warning(session.session_id):
                return "录制异常（文件无增长或无法检查），待核实"
        return self._display_state(status)

    def _audience_column_title(self):
        return f"核验在线人数（{int(self.settings.qualification_minutes)}分钟）"

    @staticmethod
    def _audience_display(session):
        if getattr(session, "audience_outcome", None) == "manual_confirmed_low":
            return "用户确认（具体人数未知）"
        if session.audience_value is None:
            return "—"
        if getattr(session, "audience_outcome", None) == "captured_at_end":
            return f"{session.audience_value}（结束前采样）"
        labels = []
        if session.audience_basis == "recording_start_estimate":
            labels.append("录制起算估算")
        elif session.audience_basis == "recording_start":
            labels.append("录制起算")
        if getattr(session, "audience_outcome", None) == "captured_late":
            labels.append("延迟采样补判，非30分钟人数")
        suffix = f"（{'；'.join(labels)}）" if labels else ""
        return f"{session.audience_value}{suffix}"

    @staticmethod
    def _session_error_display(session):
        if (getattr(session, "audience_outcome", None) == "manual_confirmed_low"
                and getattr(session, "qualification_status", None) == "rejected_low_viewers"):
            return "用户确认已清理，具体人数未知；此前中断情况保留在事件日志。"
        return getattr(session, "error", None) or "无"

    @staticmethod
    def _audience_evidence_display(session):
        if getattr(session, "audience_outcome", None) == "manual_confirmed_low":
            return "用户人工确认，未采集人数", "人工确认时间未在本字段记录"
        if getattr(session, "audience_outcome", None) == "captured_at_end":
            return "直播结束前最后一次有效采样", session.audience_captured_at
        basis = getattr(session, "audience_basis", None)
        label = ("录制开始起算（估算）" if basis == "recording_start_estimate"
                 else "录制开始起算" if basis == "recording_start"
                 else "平台开播时间" if basis == "platform_start" else "未知")
        return label, getattr(session, "audience_captured_at", None) or "尚未核验"

    def _qualification_status_display(self, session, *, now=None, scan_state=None):
        """Show blocked overdue checks without changing any persisted decision."""
        status = getattr(session, "qualification_status", None)
        outcome = getattr(session, "audience_outcome", None)
        if outcome == "manual_confirmed_low" and status == "rejected_low_viewers":
            return "用户确认已清理（人数未知）"
        if (str(getattr(session, "status", "")) != "RECORDING"
                or not getattr(self.runtime, "running", False)
                or not ((status == "pending" and outcome is None)
                        or (status == "viewer_check_failed" and outcome == "missed"))):
            return self._display_state(status)
        state = scan_state if scan_state is not None else self.runtime.scan_status.get("state")
        if state not in {"verification_required", "page_state_unknown"}:
            return self._display_state(status)
        try:
            basis = getattr(session, "audience_basis", None)
            source = getattr(session, "start_time_source", "") or ""
            platform_start = MonitorRuntime._normalized_platform_start(
                getattr(session, "started_at", None), source
            )
            if basis == "platform_start":
                anchor_text = platform_start
            elif basis in {"recording_start_estimate", "recording_start"}:
                anchor_text = getattr(session, "recording_started_at", None)
            else:
                # Old or unknown schedules may use first discovery. Recompute
                # only from a reliable platform start or actual recording start.
                anchor_text = platform_start or getattr(session, "recording_started_at", None)
            if not anchor_text:
                return self._display_state(status)
            anchor = datetime.fromisoformat(anchor_text)
            if anchor.tzinfo is None:
                return self._display_state(status)
            minutes = getattr(session, "qualification_minutes", None) or self.settings.qualification_minutes
            due = anchor + timedelta(minutes=max(1, int(minutes)))
            current = now or datetime.now(UTC)
            if current.tzinfo is None or current < due:
                return self._display_state(status)
        except (TypeError, ValueError, OverflowError):
            return self._display_state(status)
        return ("验证阻断，已逾期待补判" if state == "verification_required"
                else "页面状态未知，已逾期待补判")

    def clear_room_history(self):
        if not messagebox.askyesno(
            "清除非录制中历史",
            "将清除全部非录制中直播间的历史记录、关联事件及对应本地视频、音频、WAV和转写文件，"
            "包括已完成、失败、中断及等待录制的记录。\n\n"
            "不受当前筛选条件影响；保留录制中记录、文件和关键词配置。"
            "文件删除不可撤销，清理失败的记录将保留供重试。"
            "若仍有转写或上传任务，则本次不执行清理。\n\n是否继续？",
        ):
            return
        try:
            result = self.runtime.request_clear_history()
            self.clear_room_filters()
            if result is None:
                messagebox.showinfo("已排队", "将在当前浏览器操作完成后清除历史；录制继续。请查看事件日志中的清除结果。")
            elif result["failures"]:
                messagebox.showwarning("部分清理失败", f"已清除 {result['deleted']} 条，保留录制中 {result['retained']} 条；"
                                       f"{len(result['failures'])} 条清理失败并保留，请查看事件日志。")
            else:
                messagebox.showinfo("清除完成", f"已清除 {result['deleted']} 条历史及对应文件，保留 {result['retained']} 条录制中记录。")
        except Exception as exc:  # noqa: BLE001 - surface file/worker safety failures
            messagebox.showerror("清除未完成", str(exc))

    def clear_room_filters(self):
        self.room_filter_name.set("")
        self.room_filter_keyword.set("全部")
        self.room_filter_status.set("全部")
        self.room_filter_after.set("")
        self.room_filter_before.set("")
        self.refresh()

    def _load_browser_accounts(self):
        panes = getattr(self, "account_panes", None)
        if panes is None:
            return
        from .accounts import PLATFORMS

        platform = next(key for key, (label, _) in PLATFORMS.items() if label == self.platform_var.get())
        if self.runtime.running:
            if platform != self.runtime.platform:
                raise ValueError("请退出当前监控窗口后，再选择其他平台启动。")
            return
        library = panes[platform].library
        ready = [account for account in library.list() if account.ready]
        if platform != "whatnot" and not ready:
            raise ValueError("请先在该平台的账号库登录一个账号，并点「确认已登录」。")
        self.runtime.platform = platform
        self.runtime.load_browser_accounts = library.list
        self.runtime.browser_accounts = ready

    def start(self):
        try:
            self._load_browser_accounts()
            self.runtime.start()
            self.status_var.set(self._runtime_status_text(self.runtime))
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - surface GUI boundary errors
            messagebox.showerror("启动失败", str(exc))

    def pause(self):
        self.runtime.pause()
        self.status_var.set(self._runtime_status_text(self.runtime))

    def login(self):
        try:
            self._load_browser_accounts()
            self.runtime.open_login()
            self.status_var.set(self._runtime_status_text(self.runtime))
            # Opening is asynchronous. Do not claim success or steal focus with
            # a modal dialog before the browser-owner thread reports the result.
            self.refresh()
        except Exception as exc:  # noqa: BLE001 - surface GUI boundary errors
            messagebox.showerror("浏览器错误", str(exc))

    def add_keyword(self):
        phrase = self.single_keyword_var.get().strip()
        if not phrase:
            return
        if not self._valid_keyword(phrase):
            messagebox.showerror(
                "关键词无效", "关键词长度须为 1 至 120 个字符，且不能包含控制字符。"
            )
            return
        existing = {row.phrase.casefold() for row in self.store.list_keywords()}
        if phrase.casefold() in existing:
            messagebox.showinfo(
                "关键词已存在", "列表中已存在该关键词。"
            )
            return
        self.store.upsert_keyword(phrase, enabled=True)
        self.single_keyword_var.set("")
        self.refresh()

    @staticmethod
    def _valid_keyword(phrase: str) -> bool:
        return (
            bool(phrase)
            and len(phrase) <= 120
            and not any(ord(character) < 32 for character in phrase)
        )

    def add_keywords_batch(self):
        raw = self.batch_text.get("1.0", "end-1c")
        existing = {row.phrase.casefold() for row in self.store.list_keywords()}
        seen: set[str] = set()
        added = duplicates = invalid = 0
        for candidate in re.split(r"[\r\n,，;；]+", raw):
            phrase = candidate.strip()
            if not phrase:
                continue
            normalized = phrase.casefold()
            if not self._valid_keyword(phrase):
                invalid += 1
            elif normalized in seen or normalized in existing:
                duplicates += 1
            else:
                seen.add(normalized)
                try:
                    self.store.upsert_keyword(phrase, enabled=True)
                    added += 1
                except (ValueError, sqlite3.IntegrityError):
                    invalid += 1
        self.refresh()
        messagebox.showinfo(
            "批量添加完成",
            f"成功添加：{added}\n重复跳过：{duplicates}\n无效关键词：{invalid}",
        )

    def rename_keyword(self):
        key_id = self._selected_keyword()
        if key_id is None:
            return
        rows = self.store.list_keywords()
        row = next((row for row in rows if row.id == key_id), None)
        if not row:
            return
        phrase = simpledialog.askstring(
            "修改关键词",
            "搜索关键词：",
            initialvalue=row.phrase,
            parent=self.root,
        )
        if phrase and phrase.strip():
            try:
                normalized = phrase.strip()
                if not self._valid_keyword(normalized):
                    raise ValueError("关键词长度须为 1 至 120 个字符，且不能包含控制字符。")
                self.store.upsert_keyword(
                    normalized, keyword_id=key_id, enabled=row.enabled
                )
                self.refresh()
            except (ValueError, sqlite3.IntegrityError) as exc:
                messagebox.showerror("修改关键词失败", str(exc))

    def toggle_keyword(self):
        key_id = self._selected_keyword()
        if key_id is None:
            return
        row = next(
            (item for item in self.store.list_keywords() if item.id == key_id), None
        )
        if row:
            self.store.set_keyword_enabled(key_id, not row.enabled)
            self.refresh()

    def set_selected_keywords(self, enabled: bool):
        selected = self._selected_keywords()
        if not selected:
            return
        for keyword_id in selected:
            self.store.set_keyword_enabled(keyword_id, enabled)
        self.refresh()

    def delete_selected_keywords(self):
        selected = self._selected_keywords()
        if not selected:
            return
        if not messagebox.askyesno(
            "删除关键词",
            f"确定删除所选的 {len(selected)} 个关键词吗？已发现的直播间和录制任务会保留。",
        ):
            return
        for keyword_id in selected:
            self.store.delete_keyword(keyword_id)
        self.refresh()

    def delete_keyword(self):
        key_id = self._selected_keyword()
        if key_id is None:
            return
        if messagebox.askyesno(
            "删除关键词",
            "确定删除此关键词吗？已有直播场次和录制任务会保留。",
        ):
            self.store.delete_keyword(key_id)
            self.refresh()

    def scan_now(self):
        self.runtime.request_scan_now()

    def scan_selected_keyword(self):
        keyword_id = self._selected_keyword()
        if keyword_id is None:
            return
        request = getattr(self.runtime, "request_keyword_scan", None)
        if request is None:
            self.runtime.request_scan_now()
            self.events_notice("所选关键词扫描已加入队列；将扫描全部已启用关键词。")
        else:
            request(keyword_id)

    def events_notice(self, message: str):
        self.runtime.events.put(("info", message))

    def save_feishu_settings(self):
        from dataclasses import replace

        from .config import has_feishu_secret, save_settings, set_feishu_secret

        try:
            updated = replace(
                self.settings,
                feishu_app_token=self.feishu_vars["feishu_app_token"].get().strip(),
                feishu_table_id=self.feishu_vars["feishu_table_id"].get().strip(),
                feishu_app_id=self.feishu_vars["feishu_app_id"].get().strip(),
            )
            secret = self.feishu_vars["secret"].get()
            if secret and secret != "***":
                updated = set_feishu_secret(updated, secret)
            save_settings(updated)
            self.settings = updated
            self.runtime.settings = updated
            self.feishu_vars["secret"].set("***" if has_feishu_secret(updated) else "")
            resumed = self.runtime.resume_pending_feishu_uploads()
            self.feishu_status.set(f"配置已保存；已安排 {resumed} 项待上传任务重试。")
            messagebox.showinfo("飞书配置", "飞书配置已保存并应用。")
        except Exception as exc:  # noqa: BLE001 - surface GUI boundary errors
            messagebox.showerror("飞书配置保存失败", str(exc))

    def save_settings(self):
        from dataclasses import replace

        from .config import save_settings

        try:
            values = dict(self.setting_vars)
            parsed = {}
            for key, variable in values.items():
                value_type = self.setting_types[key]
                raw = variable.get()
                if value_type == "bool":
                    parsed[key] = bool(raw)
                elif (
                    value_type == "int"
                    or value_type == "capacity"
                    or value_type == "bitrate"
                ):
                    parsed[key] = int(raw)
                elif value_type == "float":
                    parsed[key] = float(raw)
                else:
                    parsed[key] = str(raw).strip()
            normalized = {
                key: parsed[key]
                for key in parsed
                if key in self.settings.__dataclass_fields__
            }
            normalized.update(
                poll_interval_seconds=max(15, parsed["poll_interval_seconds"]),
                max_concurrent_recordings=max(
                    1, min(100, parsed["max_concurrent_recordings"])
                ),
                qualification_minutes=max(1, parsed.get("qualification_minutes", 30)),
                minimum_viewers=max(0, parsed.get("minimum_viewers", 100)),
                audience_retry_count=max(
                    0, min(10, parsed.get("audience_retry_count", 3))
                ),
                audience_retry_interval_seconds=max(
                    1, parsed.get("audience_retry_interval_seconds", 30)
                ),
                video_bitrate_bps=max(
                    100_000, parsed.get("video_bitrate_bps", 2_500_000)
                ),
                max_recording_size_gib=max(
                    0.1, parsed.get("max_recording_size_gib", 20.0)
                ),
                audio_sample_rate=max(8000, parsed.get("audio_sample_rate", 16000)),
                audio_channels=min(2, max(1, parsed.get("audio_channels", 1))),
                disk_warning_gib=max(1, parsed.get("disk_warning_gib", 10)),
            )
            normalized = {
                key: value
                for key, value in normalized.items()
                if key in self.settings.__dataclass_fields__
            }
            updated = replace(
                self.settings,
                **normalized,
            )
            save_settings(updated)
            self.settings = updated
            self.runtime.settings = updated
            self._refresh()
            messagebox.showinfo(
                "设置",
                "设置已保存。新任务将使用更新后的录制容量，正在进行的录制会继续。",
            )
        except Exception as exc:  # noqa: BLE001 - surface GUI boundary errors
            messagebox.showerror("设置失败", str(exc))

    def _selected_session_row(self):
        session_id = self._selected_session()
        if session_id is None:
            return None
        return self.store.get_session(session_id)

    def open_selected_room(self):
        session = self._selected_session_row()
        if session:
            import webbrowser

            webbrowser.open(session.url)

    def open_selected_audio(self):
        session = self._selected_session_row()
        if session:
            audio_path = getattr(session, "audio_path", None) or session.recording_path
            if audio_path:
                self._open_path(audio_path)

    def open_selected_video(self):
        session = self._selected_session_row()
        if session:
            video_path = self._existing_path(session, "video_path")
            if video_path:
                self._open_path(video_path)
            else:
                messagebox.showinfo(
                    "视频不可用",
                    "此场直播没有可用的视频文件。",
                    parent=self.root,
                )

    @staticmethod
    def _existing_path(session, *names):
        for name in names:
            value = getattr(session, name, None)
            if not value:
                continue
            path = Path(value)
            if name == "video_path" and path.suffix.lower() == ".webm":
                mp4 = path.with_suffix(".mp4")
                if not mp4.is_symlink() and mp4.is_file() and mp4.stat().st_size:
                    return mp4
            if path.is_file():
                return path
        return None

    def show_selected_details(self):
        session = self._selected_session_row()
        if not session:
            return
        video = self._existing_path(session, "video_path")
        audio = self._existing_path(session, "audio_path", "recording_path")
        transcript = self._existing_path(session, "transcript_path")
        event_lines = [
            f"{event.created_at} {self._display_event_level(event.level)} {self._display_event_component(event.component)}: {event.message}"
            for event in self.store.recent_events(limit=250)
            if event.session_id == session.session_id
        ]
        segments = self.store.list_recording_segments(session.session_id)
        segment_lines = [f"{s['status']} {s['started_at'] or '开始时间未知'} 至 {s['ended_at'] or '结束时间未知'}；视频：{s['video_path'] or '无'}；音频：{s['audio_path'] or '无'}" for s in segments]
        manual_confirmed = getattr(session, "audience_outcome", None) == "manual_confirmed_low"
        current_viewer_detail = ("用户确认，具体人数未知" if manual_confirmed else
                                 f"{session.current_viewers}（{session.current_viewers_raw or '无原始值'}）")
        audience_detail = ("用户确认，具体人数未知" if manual_confirmed else
                           f"{session.audience_value}（{session.audience_raw or '无原始值'}）")
        audience_basis_detail, audience_time_detail = self._audience_evidence_display(session)
        details = (
            f"直播间：{session.name}\n"
            f"直播间 ID：{session.room_id}\n"
            f"直播场次 ID：{session.session_id}\n"
            f"录制任务 ID：{getattr(session, 'recording_task_id', '')}\n"
            f"链接：{session.url}\n"
            f"来源关键词：{', '.join(session.keyword_phrases)}\n"
            f"实际开播时间：{session.started_at or '未知'}（{session.start_time_source}）\n"
            f"首次发现时间：{session.first_seen_at}\n"
            f"开始录制时间：{session.recording_started_at or '尚未开始'}\n"
            f"结束录制时间：{session.ended_at or '尚未结束'}\n"
            f"当前观众数：{current_viewer_detail}\n"
            f"核验时观众数：{audience_detail}\n"
            f"人数核验依据：{audience_basis_detail}\n"
            f"观众数核验：{self._qualification_status_display(session)}，时间 {audience_time_detail}\n"
            f"视频文件：{video or '不存在'}\n"
            f"音频文件：{audio or '不存在'}\n"
            f"转写文件：{transcript or '不存在'}\n"
            f"飞书记录：{session.feishu_record_id or '尚未上传'}\n"
            f"状态：{self._recording_status_display(session)}\n"
            f"错误信息：{self._session_error_display(session)}\n\n"
            f"录制片段（中断期间内容缺失）：\n{chr(10).join(segment_lines) if segment_lines else '无片段记录'}\n\n"
            f"转写文本：\n{session.transcript_text or '暂无'}\n\n"
            f"最近任务事件：\n{'\n'.join(event_lines) if event_lines else '无'}"
        )
        messagebox.showinfo("直播场次详情", details, parent=self.root)

    def stop_selected(self):
        session_id = self._selected_session()
        if session_id is not None:
            self.runtime.stop_recording(session_id)

    def retry_selected(self):
        # This button lives on the recording page: never use the independently
        # selected overview row as the target for an external upload.
        selected = self.recording_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning("请选择任务", "请在录音与转写列表中选择一项任务。", parent=self.root)
            return
        session_id = selected[0]
        session = self.store.get_session(session_id)
        if session is None:
            messagebox.showwarning("任务不存在", "所选任务已不存在，请刷新列表后重试。", parent=self.root)
            return
        if session.status in {"RECORDING", "TRANSCRIBING", "UPLOADING"}:
            messagebox.showwarning("任务仍在运行", "录制或后处理尚未结束，不能提前重试转写或上传。", parent=self.root)
            return
        try:
            self.runtime.retry_session(session_id)
            self.refresh()
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror("重试失败", str(exc))

    def test_feishu(self):
        self.feishu_status.set("正在检查认证、数据表和字段结构……")
        self.runtime.test_feishu()

    def open_selected_transcript(self):
        selected = self.recording_tree.selection()
        if selected:
            session = self.store.get_session(selected[0])
            if session and session.transcript_path:
                self._open_path(session.transcript_path)

    @staticmethod
    def _open_path(path):
        import os
        import subprocess
        import sys

        p = Path(path)
        if not p.exists():
            messagebox.showerror("文件不存在", str(p))
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(p))
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, str(p)])
        except OSError as exc:
            messagebox.showerror("打开文件失败", f"无法打开文件：{exc}")

    def refresh(self):
        if self._refresh_after_id is not None:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        self._refresh()

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return ""
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _format_bytes(size: int | None) -> str:
        if not size:
            return "0 B"
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    @classmethod
    def _format_file_size(cls, path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return cls._format_bytes(path.stat().st_size)
        except OSError:
            return "不可用"

    @staticmethod
    def _restore_tree_selection(tree, selected_ids):
        retained = [item_id for item_id in selected_ids if tree.exists(item_id)]
        if retained:
            tree.selection_set(retained)

    def _refresh(self):
        try:
            sessions = self.store.list_sessions(limit=5000)
            keywords = self.store.list_keywords()
            active = [
                s
                for s in sessions
                if s.status
                in {"WAITING", "CAPACITY_INSUFFICIENT", "RECORDING", "AUDIENCE_PENDING"}
            ]
            recording = [
                s for s in sessions if s.status == "RECORDING" and self.runtime.running
            ]
            capacity_blocked = [
                s for s in sessions if s.status == "CAPACITY_INSUFFICIENT"
            ]
            waiting = [s for s in sessions if s.status == "WAITING"]
            transcribing = [s for s in sessions if s.status == "TRANSCRIBING"]
            uploading = [s for s in sessions if s.status == "UPLOADING"]
            completed = [s for s in sessions if s.status == "COMPLETED"]
            failed = [s for s in sessions if s.status in {"FAILED", "INTERRUPTED"}]
            for key, count in (
                ("keywords", sum(bool(k.enabled) for k in keywords)),
                ("live", len(active)),
                ("recording", len(recording)),
                ("transcribing", len(transcribing)),
                ("uploading", len(uploading)),
                ("completed", len(completed)),
                ("failed", len(failed)),
            ):
                self.stat_vars[key].set(str(count))
            threshold = int(getattr(self.settings, "qualification_minutes", 30))
            self.session_tree.heading("audience", text=self._audience_column_title())
            qualified = sum(
                1 for s in sessions if s.qualification_status == "qualified"
            )
            rejected = sum(
                1
                for s in sessions
                if s.qualification_status == "rejected_low_viewers"
            )
            audience_failed = sum(
                1
                for s in sessions
                if s.qualification_status == "viewer_check_failed"
            )
            recording_failed = sum(
                1
                for s in sessions
                if s.status
                in {"FAILED", "INTERRUPTED", "recording_failed", "cleanup_failed"}
            )
            uploading_failed = sum(
                1
                for s in sessions
                if s.upload_status == "FAILED"
            )
            scan = self.runtime.scan_status
            display_now = datetime.now(UTC)
            blocked_overdue = sum(
                1 for session in sessions
                if self._qualification_status_display(session, now=display_now, scan_state=scan.get("state"))
                in {"验证阻断，已逾期待补判", "页面状态未知，已逾期待补判"}
            )
            audience_failed = sum(
                1 for session in sessions
                if session.qualification_status == "viewer_check_failed"
                and self._qualification_status_display(session, now=display_now, scan_state=scan.get("state"))
                not in {"验证阻断，已逾期待补判", "页面状态未知，已逾期待补判"}
            )
            waiting_threshold = sum(
                1 for session in sessions
                if session.qualification_status == "pending"
                and session.audience_due_at and session.status == "RECORDING"
                and self._qualification_status_display(session, now=display_now, scan_state=scan.get("state"))
                not in {"验证阻断，已逾期待补判", "页面状态未知，已逾期待补判"}
            )
            try:
                disk_path = (
                    getattr(self.settings, "video_dir", None)
                    or self.settings.recordings_dir
                )
                disk_path = Path(disk_path)
                while not disk_path.exists() and disk_path != disk_path.parent:
                    disk_path = disk_path.parent
                import shutil

                disk_free = shutil.disk_usage(disk_path).free
                free_display = f"剩余 {disk_free / (1024**3):.1f} GiB"
            except (OSError, TypeError, ValueError):
                disk_free = 0
                free_display = "磁盘空间不可用"
            video_bytes_today = 0
            for session in sessions:
                video_path = self._existing_path(session, "video_path")
                if (
                    video_path
                    and session.recording_started_at
                    and session.recording_started_at[:10]
                    == datetime.now(UTC).date().isoformat()
                ):
                    try:
                        video_bytes_today += video_path.stat().st_size
                    except OSError:
                        pass
            self.monitor_summary_var.set(
                f"直播间：{len(sessions)} | 录制中：{len(recording)} | 等待开播 {threshold} 分钟核验：{waiting_threshold} | "
                f"阻断逾期待补判：{blocked_overdue} | 已达标：{qualified} | 未达标：{rejected} | 观众数核验失败：{audience_failed} | "
                f"录制错误：{recording_failed} | 转写中：{len(transcribing)} | 上传失败：{uploading_failed} | "
                f"磁盘空间：{free_display} | 今日视频大小：{self._format_bytes(video_bytes_today)}"
            )
            maximum = max(1, int(self.settings.max_concurrent_recordings))
            usage = min(100, round(len(recording) / maximum * 100))
            self.capacity_status_var.set(
                f"录制容量：{len(recording)} / {maximum}（{usage}%） | "
                f"等待启动：{len(waiting)} | 容量不足：{len(capacity_blocked)}"
            )
            scan = self.runtime.scan_status
            self.status_var.set(self._runtime_status_text(self.runtime))
            scan_state = self._display_state(scan.get("state", "idle"))
            self.scan_status_var.set(
                f"每 {self.settings.poll_interval_seconds} 秒扫描 | {scan_state} | "
                f"已扫描关键词 {scan.get('scanned_keywords', 0)}/{scan.get('total_keywords', 0)} | "
                f"失败 {scan.get('failed_keywords', 0)} | 新发现直播间 {scan.get('new_rooms', 0)} | "
                f"重复结果 {scan.get('duplicate_results', 0)} | "
                f"已启动录制 {scan.get('recordings_started', 0)}"
                + (
                    f" | 当前关键词：{scan.get('current_keyword')}"
                    if scan.get("current_keyword")
                    else ""
                )
                + (
                    f" | 阻塞原因：{scan.get('blocking_reason')}"
                    if scan.get("blocking_reason")
                    else ""
                )
            )
            self.room_keyword_filter.configure(
                values=("全部", *(keyword.phrase for keyword in keywords))
            )
            if self.room_filter_keyword.get() not in {
                "全部",
                *(keyword.phrase for keyword in keywords),
            }:
                self.room_filter_keyword.set("全部")
            selected_session_ids = self.session_tree.selection()
            session_x_fraction = self.session_tree.xview()[0]
            session_y_fraction = self.session_tree.yview()[0]
            selected_recording_ids = self.recording_tree.selection()
            self.session_tree.delete(*self.session_tree.get_children())
            self.recording_tree.delete(*self.recording_tree.get_children())
            session_sources = {}
            current_recording_by_keyword_id: dict[int, int] = {}
            for session in sessions:
                try:
                    source_rows = self.store.session_keyword_sources(session.session_id)
                    sources = [source.phrase_snapshot for source in source_rows]
                except (AttributeError, KeyError):
                    source_rows = []
                    sources = list(session.keyword_phrases)
                session_sources[session.session_id] = sources
                if session.status == "RECORDING" and self.runtime.running:
                    for source in source_rows:
                        if source.keyword_id is not None:
                            current_recording_by_keyword_id[source.keyword_id] = (
                                current_recording_by_keyword_id.get(
                                    source.keyword_id, 0
                                )
                                + 1
                            )
            name_filter = self.room_filter_name.get().strip().casefold()
            keyword_filter = self.room_filter_keyword.get()
            status_filter = self.STATUS_FILTER_VALUES.get(
                self.room_filter_status.get()
            )
            after_filter = self.room_filter_after.get().strip()
            before_filter = self.room_filter_before.get().strip()
            now = datetime.now(UTC)
            for session in sessions:
                source_phrases = session_sources[session.session_id]
                keyword = ", ".join(source_phrases)
                if name_filter and name_filter not in session.name.casefold():
                    continue
                if keyword_filter != "全部" and keyword_filter not in source_phrases:
                    continue
                if status_filter is not None and str(session.status) != status_filter:
                    continue
                started_text = session.started_at or session.first_seen_at
                started_day = started_text[:10]
                if after_filter and started_day < after_filter:
                    continue
                if before_filter and started_day > before_filter:
                    continue
                viewers = (
                    "未核实" if getattr(session, "audience_outcome", None) == "manual_confirmed_low"
                    else session.current_viewers if session.current_viewers is not None else ""
                )
                audience = self._audience_display(session)
                duration = session.duration_seconds
                if (
                    session.status == "RECORDING"
                    and self.runtime.running
                    and session.recording_started_at
                ):
                    try:
                        started_recording = datetime.fromisoformat(
                            session.recording_started_at
                        )
                        duration = max(0, (now - started_recording).total_seconds())
                    except ValueError:
                        pass
                show_ended = (
                    f"已结束（{session.ended_at}）"
                    if session.ended_at
                    else "未结束 / 未核实"
                )
                display_status = self._recording_status_display(session)
                video_path = self._existing_path(session, "video_path")
                audio_path = self._existing_path(
                    session, "audio_path", "recording_path"
                )
                video_size = self._format_file_size(video_path)
                audio_size = self._format_file_size(audio_path)
                transcript_path = self._existing_path(session, "transcript_path")
                transcript_status = (
                    "已完成"
                    if session.transcript_text or transcript_path
                    else "转写中"
                    if session.status == "TRANSCRIBING"
                    else self._display_state(
                        getattr(session, "transcription_status", None) or "NOT_STARTED"
                    )
                )
                upload_status = (
                    "已完成"
                    if session.feishu_record_id
                    else "上传中"
                    if session.status == "UPLOADING"
                    else self._display_state(
                        getattr(session, "upload_status", None) or "NOT_STARTED"
                    )
                )
                self.session_tree.insert(
                    "",
                    "end",
                    iid=str(session.id),
                    values=(
                        session.name,
                        audience,
                        session.audience_captured_at or "—",
                        session.room_id,
                        session.session_id,
                        session.url,
                        keyword,
                        started_text,
                        session.first_seen_at,
                        session.recording_started_at or "",
                        viewers,
                        self._qualification_status_display(session, now=now, scan_state=scan.get("state")),
                        display_status,
                        self._format_duration(duration),
                        video_size,
                        audio_size,
                        str(video_path) if video_path else "",
                        str(audio_path) if audio_path else "",
                        transcript_status,
                        upload_status,
                        show_ended,
                    ),
                )
                self.recording_tree.insert(
                    "",
                    "end",
                    iid=str(session.id),
                    values=(
                        session.name,
                        display_status,
                        str(audio_path) if audio_path else "",
                        session.transcript_path or "",
                    ),
                )
            # The 2.5-second refresh must not clear the selection between a
            # user's row click and their Stop/Open/Retry button click.
            self._restore_tree_selection(self.session_tree, selected_session_ids)
            self.session_tree.update_idletasks()
            self.session_tree.xview_moveto(session_x_fraction)
            self.session_tree.yview_moveto(session_y_fraction)
            self._restore_tree_selection(self.recording_tree, selected_recording_ids)
            selected_keyword_ids = set(self.keyword_tree.selection())
            self.keyword_tree.delete(*self.keyword_tree.get_children())
            for row in keywords:
                self.keyword_tree.insert(
                    "",
                    "end",
                    iid=str(row.id),
                    values=(
                        row.phrase,
                        "是" if row.enabled else "否",
                        row.created_at,
                        row.last_scanned_at or "",
                        self._display_scan_result(row.last_result),
                        row.discovered_count,
                        current_recording_by_keyword_id.get(row.id, 0),
                        self._display_state(row.scan_status),
                    ),
                )
            retained_selection = [
                item_id
                for item_id in selected_keyword_ids
                if self.keyword_tree.exists(item_id)
            ]
            if retained_selection:
                self.keyword_tree.selection_set(retained_selection)
            self.event_text.configure(state="normal")
            self.event_text.delete("1.0", "end")
            for event in self.store.recent_events(limit=250):
                self.event_text.insert(
                    "end",
                    f"{event.created_at}  {self._display_event_level(event.level)}  {self._display_event_component(event.component)}: {event.message}\n",
                )
            while True:
                try:
                    level, message = self.runtime.events.get_nowait()
                    self.event_text.insert(
                        "end", f"{self._display_event_level(level)}  {message}\n"
                    )
                    if (
                        "飞书认证、数据表和五个字段的结构检查已通过" in message
                        or "Feishu authentication, table, and five-field schema check passed"
                        in message
                    ):
                        self.feishu_status.set(
                            "已连接，数据表结构匹配；尚未测试写入权限。"
                        )
                    elif (
                        "飞书启动检查失败" in message
                        or "尚未配置飞书凭据" in message
                        or "Feishu startup check failed" in message
                        or "credentials are not configured" in message
                    ):
                        self.feishu_status.set(self._display_feishu_message(message))
                except queue.Empty:
                    break
            self.event_text.configure(state="disabled")
        except Exception:
            logging.getLogger(__name__).exception("Dashboard refresh failed")
        if self.root.winfo_exists():
            self._refresh_after_id = self.root.after(2500, self._refresh)

    def close(self):
        if getattr(self, "_shutdown_in_progress", False):
            return
        if self.runtime.running and not messagebox.askyesno(
            "退出", "是否正常停止录制并关闭程序？"
        ):
            return
        # Playwright belongs to the runtime's owner thread.  Never force-kill it
        # or close the database while it is flushing media or postprocessing.
        # Waiting on another thread keeps Tk's event loop and progress visible.
        self._shutdown_in_progress = True
        dialog = tk.Toplevel(self.root)
        dialog.title("正在安全关闭")
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(dialog, text="正在停止录制并保存媒体，请勿关闭浏览器或强制结束程序。\n直播间较多时可能需要数分钟。", padding=20).pack()
        dialog.grab_set()
        result: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                self.runtime.shutdown()
            except Exception as exc:
                logging.getLogger(__name__).exception("Shutdown failed")
                result.put(exc)
            else:
                result.put(None)

        threading.Thread(target=worker, name="graceful-shutdown", daemon=True).start()

        def poll():
            try:
                error = result.get_nowait()
            except queue.Empty:
                self.root.after(500, poll)
                return
            dialog.grab_release()
            dialog.destroy()
            if error is not None:
                self._shutdown_in_progress = False
                messagebox.showerror("关闭失败", f"程序未能安全关闭：{error}\n请查看日志。")
                return
            watchdog = getattr(self.runtime, "_file_watchdog", None)
            if watchdog is not None and watchdog.shutdown_loss:
                messagebox.showwarning(
                    "录制告警可能丢失",
                    "关闭时仍有未完成的文件增长告警；部分事件可能未写入数据库。",
                )
            self.store.close()
            self.root.destroy()

        self.root.after(100, poll)


def configure_logging():
    log_dir = APP_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "application.log", maxBytes=4_000_000, backupCount=5, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main():
    configure_logging()
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
