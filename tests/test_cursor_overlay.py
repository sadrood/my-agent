"""光标可视化浮层测试（不真的开窗：只验证开关与安全空操作）。"""
import os
import sys

import pytest

from tools import cursor_overlay
from tools.cursor_overlay import CursorOverlay, get_overlay, overlay_enabled


def test_overlay_env_switch(monkeypatch):
    monkeypatch.setenv("COMPUTER_CURSOR_OVERLAY", "0")
    assert overlay_enabled() is False
    assert CursorOverlay.available() is False
    assert get_overlay() is None          # 关闭时调用方拿到 None，直接跳过

    monkeypatch.setenv("COMPUTER_CURSOR_OVERLAY", "1")
    assert overlay_enabled() is True
    if sys.platform == "win32":
        assert CursorOverlay.available() is True


def test_touch_ripple_are_safe_noops_when_disabled(monkeypatch):
    monkeypatch.setenv("COMPUTER_CURSOR_OVERLAY", "0")
    ov = CursorOverlay(idle_seconds=1)
    ov.touch()          # 不应启动线程、不应抛
    ov.ripple(10, 20)
    ov.stop()
    assert ov._thread is None


def test_computer_tool_uses_overlay_silently(monkeypatch):
    """computer 工具入口在有/无浮层时都应正常返回（不因浮层缺失报错）。"""
    monkeypatch.setenv("COMPUTER_CURSOR_OVERLAY", "0")
    from tools.computer_use import DesktopTool
    t = DesktopTool()
    if not t.__class__.__module__:      # pragma: no cover - 防御
        pytest.skip("computer 工具不可用")
    r = t.execute_json({"action": "不存在的动作"})
    assert r.success is False
    assert "未知 action" in (r.error or "")

def test_overlay_default_off(monkeypatch):
    """默认必须关闭：全屏浮层穿透失效会吞鼠标事件，改为按需开启。"""
    monkeypatch.delenv("COMPUTER_CURSOR_OVERLAY", raising=False)
    assert overlay_enabled() is False
    assert get_overlay() is None
