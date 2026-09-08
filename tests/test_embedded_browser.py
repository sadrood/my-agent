"""
内嵌浏览器工具测试（桥接转发逻辑，不依赖 Electron / 网络）。
"""
import pytest

from tools.embedded_browser import EmbeddedBrowserTool


@pytest.fixture()
def tool(monkeypatch):
    """桥调用被拦截的 EmbeddedBrowserTool，并记录每次调用。"""
    t = EmbeddedBrowserTool(bridge_url="http://127.0.0.1:9999/browser")
    calls = []

    def _fake_call(action, **params):
        calls.append((action, params))
        return {"ok": True, "output": f"bridge:{action}"}

    monkeypatch.setattr(t, "_call_bridge", _fake_call)
    return t, calls


def test_goto_normalizes_and_routes(tool):
    t, calls = tool
    r = t.execute("goto example.com/path")
    assert r.success
    assert calls[0][0] == "navigate"
    assert calls[0][1]["url"] == "https://example.com/path"

    r2 = t.execute("goto https://a.b")
    assert r2.success
    assert calls[1][1]["url"] == "https://a.b"


def test_launch_and_readonly_passthrough(tool):
    t, calls = tool
    assert t.execute("launch").success
    assert calls[-1][0] == "open"
    assert t.execute("text").success
    assert calls[-1][0] == "text"
    assert t.execute("status").success
    assert calls[-1][0] == "status"


def test_type_parses_selector_and_text(tool):
    t, calls = tool
    r = t.execute("type #search 你好 世界")
    assert r.success
    assert calls[-1] == ("type", {"selector": "#search", "text": "你好 世界"})

    # 缺文本参数 → 报错且不调桥
    calls.clear()
    r2 = t.execute("type #search")
    assert not r2.success
    assert not calls


def test_scroll_directions(tool):
    t, calls = tool
    t.execute("scroll down 300")
    assert calls[-1] == ("scroll", {"dx": 0, "dy": 300})
    t.execute("scroll up")
    assert calls[-1] == ("scroll", {"dx": 0, "dy": -500})
    t.execute("scroll left 200")
    assert calls[-1] == ("scroll", {"dx": -200, "dy": 0})
    t.execute("scroll top")
    assert calls[-1][0] == "js"
    t.execute("scroll bad-dir")
    # 未知方向不调桥，返回错误
    assert not t.execute("scroll bad-dir").success


def test_screenshot_base64_wraps_tag(tool):
    t, calls = tool
    monkey_shot = {"ok": True, "base64": "QUJD", "output": ""}
    monkey_status = {"ok": True, "output": "浏览器状态: 运行中"}

    def _fake(action, **params):
        calls.append((action, params))
        return monkey_shot if action == "screenshot" else monkey_status

    monkeypatch_stub = _fake
    t._call_bridge = monkeypatch_stub
    r = t.execute("screenshot_base64")
    assert r.success
    assert "[FULL_BASE64]QUJD[/FULL_BASE64]" in r.output
    assert "[FULL_BASE64]" in t.execute("screenshot_base64").output


def test_unsupported_command_guides(tool):
    t, calls = tool
    for cmd in ("newtab", "switchtab 1", "closetab 2", "alert accept", "elementinfo #x"):
        r = t.execute(cmd)
        assert not r.success, f"{cmd} 应该不支持"
        assert "不支持" in r.error
    assert not calls  # 均不应触桥


def test_tabs_and_switch_route_to_bridge(tool):
    """多标签：tabs / switch / open 均路由到桥。"""
    t, calls = tool
    r = t.execute("tabs")
    assert r.success
    assert calls[-1][0] == "tabs"

    r2 = t.execute("switch 1")
    assert r2.success
    assert calls[-1][0] == "switch"
    assert calls[-1][1]["index"] == "1"

    r3 = t.execute("open https://example.com")
    assert r3.success
    assert calls[-1][0] == "open"
    assert calls[-1][1]["url"] == "https://example.com"

    r4 = t.execute("launch")
    assert r4.success
    assert calls[-1][0] == "open"


def test_bridge_unreachable_yields_clear_error(monkeypatch):
    import urllib.request
    t = EmbeddedBrowserTool(bridge_url="http://127.0.0.1:1/browser")

    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    r = t.execute("text")
    assert not r.success
    assert "桥不可达" in r.error


def test_vision_click_flow(monkeypatch, tool):
    """visionclick：桥截图 → 视觉定位 → 桥坐标点击。"""
    t, calls = tool
    b64 = "QUJD"

    def _fake(action, **params):
        calls.append((action, params))
        if action == "screenshot":
            return {"ok": True, "base64": b64}
        if action == "status":
            return {"ok": True, "output": "status-line"}
        if action == "clickat":
            return {"ok": True, "output": "clicked"}
        return {"ok": False, "error": "unexpected"}

    t._call_bridge = _fake

    class _FakeVM:
        def __init__(self, *a, **k):
            pass

        def locate_element(self, data, desc):
            assert data == b64
            return {"found": True, "x": 120.0, "y": 80.0, "width": 50, "height": 30}

    monkeypatch.setattr("models.vision.VisionModel", _FakeVM)
    r = t.execute("visionclick 登录按钮")
    assert r.success
    assert ("clickat", {"x": 120.0, "y": 80.0, "button": "left"}) in calls

    # 未找到元素 → 报错且不点击
    class _MissVM(_FakeVM):
        def locate_element(self, data, desc):
            return {"found": False, "selector_hint": ""}

    monkeypatch.setattr("models.vision.VisionModel", _MissVM)
    calls.clear()
    r2 = t.execute("visionclick 不存在的按钮")
    assert not r2.success
    assert not any(c[0] == "clickat" for c in calls)


def test_tool_manager_selects_embedded_when_bridge_env(monkeypatch):
    """注入桥地址环境变量 → ToolManager 注册 EmbeddedBrowserTool；未注入 → BrowserTool。"""
    from tools import tool_manager as tm_mod

    created = {}

    class _FakeEmbedded:
        def __init__(self, bridge_url=None):
            created["embedded"] = True

        name = "browser"

    monkeypatch.setattr("tools.embedded_browser.EmbeddedBrowserTool", _FakeEmbedded)
    monkeypatch.setenv("MY_AGENT_EMBEDDED_BROWSER_URL", "http://127.0.0.1:8091/browser")
    assert isinstance(tm_mod._create_browser_tool(), _FakeEmbedded)

    # 未注入环境变量且桥离线（显式置离线，避免开发机上桌面端恰好开着导致探测成功）
    monkeypatch.delenv("MY_AGENT_EMBEDDED_BROWSER_URL", raising=False)
    monkeypatch.setattr("tools.embedded_browser.probe_bridge", lambda timeout=0.8: False)
    created.clear()
    tool = tm_mod._create_browser_tool()
    assert "embedded" not in created
    assert type(tool).__name__ == "BrowserTool"


def test_tool_manager_autodetects_running_bridge(monkeypatch):
    """自动探测：桌面端桥在线 → 内嵌浏览器（禁外部）；离线 → 回退外部浏览器。"""
    from tools import tool_manager as tm_mod

    created = {}

    class _FakeEmbedded:
        def __init__(self, bridge_url=None):
            created["embedded"] = True

        name = "browser"

    monkeypatch.setattr("tools.embedded_browser.EmbeddedBrowserTool", _FakeEmbedded)
    monkeypatch.delenv("MY_AGENT_EMBEDDED_BROWSER_URL", raising=False)

    # 桥在线：探测到 → 内嵌
    monkeypatch.setattr("tools.embedded_browser.probe_bridge", lambda timeout=0.8: True)
    assert isinstance(tm_mod._create_browser_tool(), _FakeEmbedded)

    # 桥离线：回退外部 BrowserTool
    monkeypatch.setattr("tools.embedded_browser.probe_bridge", lambda timeout=0.8: False)
    created.clear()
    tool = tm_mod._create_browser_tool()
    assert "embedded" not in created
    assert type(tool).__name__ == "BrowserTool"


def test_parallel_safe_readonly_only(tool):
    t, _ = tool
    assert t.is_parallel_safe({"command": "text"})
    assert t.is_parallel_safe({"command": "screenshot_base64"})
    assert not t.is_parallel_safe({"command": "goto"})
    assert not t.is_parallel_safe({"command": "click"})
