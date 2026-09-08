"""
桌面操控工具（DesktopTool）测试。
不触发真实鼠标键盘：输入原语全部 monkeypatch，只验证 schema/分发/解析逻辑。
"""
import pytest

from tools.computer_use import DesktopTool, _VK_MAP, _os_key_combo


class _FakeVision:
    def analyze(self, b64, question, max_tokens=1500):
        return "【测试分析】屏幕上有一个按钮。"


@pytest.fixture
def tool():
    return DesktopTool(vision_model=_FakeVision())


def test_registered_in_manager():
    """computer 工具应被 ToolManager 注册，且审批元数据为高危。"""
    from tools.tool_manager import ToolManager
    tm = ToolManager()
    t = tm.get_tool("computer")
    assert t is not None
    assert t.risk_level == "high"
    assert t.approval == "on-request"
    assert t.min_sandbox_mode == "danger-full-access"


def test_schema_actions():
    tool = DesktopTool()
    s = tool.schema
    assert s["type"] == "object"
    assert set(s["properties"]["action"]["enum"]) == {
        "screenshot", "a11y", "click", "type", "key", "scroll", "window"}


def test_unknown_action(tool):
    r = tool.execute_json({"action": "fly"})
    assert not r.success
    assert "未知 action" in r.error


def test_click_requires_coords(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.computer_use._os_click",
        lambda x, y, button="left", double=False: calls.append((x, y, button, double)) or True)

    r = tool.execute_json({"action": "click"})
    assert not r.success and "坐标" in r.error
    assert calls == []

    r2 = tool.execute_json({"action": "click", "x": 100, "y": 200, "double": True})
    assert r2.success
    assert calls == [(100, 200, "left", True)]


def test_type_dispatch(tool, monkeypatch):
    typed = []
    monkeypatch.setattr("tools.computer_use._os_type_text", lambda t: typed.append(t) or True)

    r = tool.execute_json({"action": "type", "text": "你好世界"})
    assert r.success and typed == ["你好世界"]

    r2 = tool.execute_json({"action": "type", "text": ""})
    assert not r2.success


def test_key_dispatch(tool, monkeypatch):
    pressed = []
    monkeypatch.setattr("tools.computer_use._os_key_combo",
                        lambda c: pressed.append(c) or [0x11])
    r = tool.execute_json({"action": "key", "combo": "ctrl+s"})
    assert r.success and pressed == ["ctrl+s"]

    # 真实解析：非法键名必须在触碰硬件前就拒绝（先校验后按键）
    assert _os_key_combo("ctrl+不存在的键") is None
    assert _os_key_combo("") is None


def test_vk_map_core_keys():
    for k in ("ctrl", "shift", "alt", "enter", "esc", "delete", "f4", "a", "5", "win", "space"):
        assert k in _VK_MAP


def test_window_list_and_activate(tool, monkeypatch):
    monkeypatch.setattr("tools.computer_use._os_windows",
                        lambda: [(1, "记事本"), (2, "Edge")])
    monkeypatch.setattr("tools.computer_use._os_activate",
                        lambda t: "记事本 - 无标题" if "记事本" in t else None)

    r = tool.execute_json({"action": "window"})
    assert r.success and "记事本" in r.output

    r2 = tool.execute_json({"action": "window", "title": "记事本"})
    assert r2.success and "已激活" in r2.output

    r3 = tool.execute_json({"action": "window", "title": "不存在的窗口xyz"})
    assert not r3.success


def test_screenshot_with_vision(tool, monkeypatch, tmp_path):
    png = tmp_path / "s.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr("tools.computer_use._take_screenshot", lambda: str(png))
    monkeypatch.setattr("tools.computer_use._active_window_title", lambda: "测试窗口")

    r = tool.execute_json({"action": "screenshot"})
    assert r.success
    assert "【屏幕分析】" in r.output
    assert "测试窗口" in r.output
    assert str(png) in r.output
