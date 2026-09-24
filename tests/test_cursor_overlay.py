"""光标可视化浮层测试（不真的开窗：只验证开关与安全空操作）。"""
import os
import sys
import time

import pytest

from config import COMPUTER_USE_CONFIG
from tools.cursor_overlay import CursorOverlay, get_overlay, overlay_enabled


@pytest.fixture
def overlay_off(monkeypatch):
    monkeypatch.setitem(COMPUTER_USE_CONFIG, "cursor_overlay", False)


@pytest.fixture
def overlay_on(monkeypatch):
    monkeypatch.setitem(COMPUTER_USE_CONFIG, "cursor_overlay", True)


def test_overlay_switch(overlay_off, monkeypatch):
    assert overlay_enabled() is False
    assert CursorOverlay.available() is False
    assert get_overlay() is None          # 关闭时调用方拿到 None，直接跳过

    monkeypatch.setitem(COMPUTER_USE_CONFIG, "cursor_overlay", True)
    assert overlay_enabled() is True
    if sys.platform == "win32":
        assert CursorOverlay.available() is True


def test_touch_ripple_are_safe_noops_when_disabled(overlay_off):
    ov = CursorOverlay(idle_seconds=1)
    ov.touch()          # 不应启动线程、不应抛
    ov.ripple(10, 20)
    ov.stop()
    assert ov._thread is None


def test_stop_hides_instead_of_destroying():
    """`stop()` 只隐藏（`_alive_until` 归零），不向线程投递销毁命令。

    回归：旧实现投 "quit" → 线程里 `root.destroy()` → 跨线程拆 Tcl，随后任意时刻
    整个进程无声消失（实测连 stdout 都不 flush）。
    """
    ov = CursorOverlay(idle_seconds=1)
    ov._alive_until = time.time() + 100
    ov.stop()
    assert ov._alive_until == 0.0     # 下一拍 tick 会清空画布 = 看不见
    assert list(ov._cmds) == []       # 没有"销毁"类命令在队列里


def test_computer_tool_uses_overlay_silently(overlay_off):
    """computer 工具入口在有/无浮层时都应正常返回（不因浮层缺失报错）。"""
    from tools.computer_use import DesktopTool
    t = DesktopTool()
    r = t.execute_json({"action": "不存在的动作"})
    assert r.success is False
    assert "未知 action" in (r.error or "")


def test_idle_seconds_come_from_config(overlay_on, monkeypatch):
    """空闲隐藏秒数必须真的读配置（此前 docstring 承诺的那个变量在代码里不存在）。"""
    monkeypatch.setitem(COMPUTER_USE_CONFIG, "cursor_idle_seconds", 3)
    import tools.cursor_overlay as mod
    monkeypatch.setattr(mod, "_overlay", None)      # 单例已缓存时也要重建
    assert get_overlay()._idle == 3


def test_default_is_off():
    """出厂默认必须是**关**（不是"用户碰巧没设"）：浮层穿透失效会吞鼠标事件。"""
    if os.getenv("COMPUTER_CURSOR_OVERLAY"):        # 本机 .env 显式开了 → 默认值不参与
        pytest.skip("本机显式设置了 COMPUTER_CURSOR_OVERLAY")
    assert COMPUTER_USE_CONFIG["cursor_overlay"] is False
