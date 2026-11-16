from types import SimpleNamespace

import pytest
from whatnot_monitor.app import MonitorApp


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("starting", "正在连接 Whatnot"),
        ("opening_login", "正在打开 Whatnot 页面"),
        ("login_ready", "等待手动登录或验证"),
        ("scanning", "扫描中"),
        ("verification_required", "Whatnot 安全验证待完成"),
        ("page_state_unknown", "页面状态未知；自动导航已暂停，请检查浏览器"),
        ("error", "连接或扫描失败；请查看事件日志"),
        ("partial_error", "部分关键词扫描失败；请查看事件日志"),
    ],
)
def test_runtime_status_exposes_startup_and_verification_states(state, expected):
    runtime = SimpleNamespace(
        running=True,
        paused=False,
        scan_status={"state": state},
    )

    assert MonitorApp._runtime_status_text(runtime) == expected


@pytest.mark.parametrize(("phase", "expected"), [
    ("loading", "安全验证加载中；尚未检测到可操作控件"),
    ("action_visible", "安全验证待手动处理；自动导航已暂停"),
    ("unknown", "Whatnot 安全验证待完成"),
])
def test_verification_status_does_not_instruct_clicking_an_absent_checkbox(phase, expected):
    runtime = SimpleNamespace(
        running=True, paused=True,
        scan_status={"state": "verification_required", "verification_phase": phase},
    )
    assert MonitorApp._runtime_status_text(runtime) == expected


def test_runtime_status_shows_pause_and_stopped_states():
    paused = SimpleNamespace(
        running=True,
        paused=True,
        scan_status={"state": "complete"},
    )
    stopped = SimpleNamespace(
        running=False,
        paused=False,
        scan_status={"state": "complete"},
    )

    assert MonitorApp._runtime_status_text(paused) == "已暂停；现有录制将继续"
    assert MonitorApp._runtime_status_text(stopped) == "已停止"
