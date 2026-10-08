"""ego 浏览器后端：脚本协议、命令映射、哨兵解析、后端选择（全部离线，不真的拉起浏览器）。"""
import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import tools.ego_browser as ego_mod
from tools.base import ToolResult
from tools.browser import BrowserTool
from tools.ego_browser import SENTINEL, EgoBrowserTool, ego_available


class _Proc:
    def __init__(self, stdout="", stderr="", code=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, code


@pytest.fixture
def captured(monkeypatch):
    """记录每次给 CLI 的脚本与环境，并按预设返回哨兵行。"""
    seen = {"scripts": [], "envs": [], "payloads": []}

    def _run(argv, input=None, env=None, **kw):
        seen["scripts"].append(input)
        seen["envs"].append(env or {})
        if seen["payloads"]:
            payload = seen["payloads"].pop(0)
            if callable(payload):
                return payload()
            return _Proc(stdout=f"noise line\n{SENTINEL}{json.dumps(payload)}\n")
        return _Proc(stdout=f"{SENTINEL}{json.dumps({'ok': True, '__out': {}})}")

    monkeypatch.setattr(ego_mod.subprocess, "run", _run)
    monkeypatch.setattr(ego_mod.shutil, "which", lambda name: "C:/node/node.exe")
    return seen


def _tool(monkeypatch, **kw):
    monkeypatch.setitem(ego_mod.__dict__, "resolve_chrome", lambda: r"C:\Edge\msedge.exe")
    tool = EgoBrowserTool(cli="C:/fake/ego.mjs", space="test-space", **kw)
    tool._chrome = r"C:\Edge\msedge.exe"
    return tool


class TestScriptProtocol:
    def test_top_level_await_with_space_and_sentinel(self, monkeypatch):
        script = _tool(monkeypatch)._script("  __out = { a: 1 }\n")
        assert 'await taskSpaces.useOrCreate("test-space")' in script
        assert "await browser.listTabs()" in script
        assert "await browser.switchTab(__real.targetId)" in script
        assert "let __out = null" in script
        assert SENTINEL in script
        # 包成 IIFE 会让 CLI 在 promise 未落地时退出（实测踩过）
        assert "(async () =>" not in script

    def test_chrome_env_is_exported(self, monkeypatch, captured):
        tool = _tool(monkeypatch)
        tool._run("  __out = {}\n")
        assert captured["envs"][0]["EGO_LINUX_CHROME"] == r"C:\Edge\msedge.exe"

    def test_headless_env_only_when_enabled(self, monkeypatch, captured):
        _tool(monkeypatch, headless=False)._run("  __out = {}\n")
        assert "EGO_LINUX_HEADLESS" not in captured["envs"][0]
        _tool(monkeypatch, headless=True)._run("  __out = {}\n")
        assert captured["envs"][1]["EGO_LINUX_HEADLESS"] == "1"


class TestRunParsing:
    def test_parses_sentinel_payload_with_noise(self, monkeypatch, captured):
        captured["payloads"].append({"ok": True, "__out": {"title": "ok"}})
        assert _tool(monkeypatch)._run("x")["__out"] == {"title": "ok"}

    def test_missing_sentinel_reports_stderr_tail(self, monkeypatch, captured):
        captured["payloads"].append(lambda: _Proc(stdout="", stderr="Error: no Chrome found"))
        result = _tool(monkeypatch)._run("x")
        assert result["ok"] is False
        assert "没有回传结果" in result["error"] and "no Chrome" in result["error"]

    def test_timeout_is_reported(self, monkeypatch, captured):
        def _boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="node", timeout=1)

        captured["payloads"].append(_boom)
        assert "超时" in _tool(monkeypatch)._run("x")["error"]

    def test_missing_cli_or_node(self, monkeypatch, captured):
        tool = _tool(monkeypatch)
        tool._cli = ""
        assert "CLI" in tool._run("x")["error"]
        tool2 = _tool(monkeypatch)
        monkeypatch.setattr(ego_mod.shutil, "which", lambda name: None)
        assert "node" in tool2._run("x")["error"]
        assert captured["scripts"] == [], "缺 CLI/node 时不该真的起进程"

    def test_script_error_is_surfaced(self, monkeypatch, captured):
        captured["payloads"].append({"ok": False, "error": "locator timeout"})
        result = _tool(monkeypatch).execute("click #nope")
        assert result.success is False and "locator timeout" in result.error


class TestCommandMapping:
    def _script_for(self, monkeypatch, captured, command):
        _tool(monkeypatch).execute(command)
        return captured["scripts"][-1]

    @pytest.mark.parametrize("command,fragment", [
        ("goto example.com", 'page.goto("https://example.com"'),
        ("goto data:text/html,<b>x</b>", 'page.goto("data:text/html,<b>x</b>"'),
        ("click #a", 'page.locator("#a").click()'),
        ("type #q hello", 'page.locator("#q").fill("hello")'),
        ("fill #q 你好", 'page.locator("#q").fill("你好")'),
        ("press Enter", 'page.keyboard.press("Enter")'),
        ("js document.title", 'page.evaluate("document.title")'),
        ("wait 1.5", "page.waitForTimeout(1500)"),
        ("scroll down", "window.scrollBy(0, 600)"),
        ("scroll top", "window.scrollTo(0, 0)"),
        ("snapshot", "await page.snapshot()"),
        ("text", "document.body.innerText"),
        ("screenshot", "await page.screenshot()"),
        ("url", "await page.info()"),
        ("title", "await page.info()"),
    ])
    def test_command_script(self, monkeypatch, captured, command, fragment):
        assert fragment in self._script_for(monkeypatch, captured, command)

    def test_missing_argument_is_rejected_without_running(self, monkeypatch, captured):
        tool = _tool(monkeypatch)
        for bad in ("click", "type #q", "press", "js"):
            result = tool.execute(bad)
            assert result.success is False, bad
        assert captured["scripts"] == [], "参数不全不该真的跑脚本"

    def test_unsupported_command_lists_support(self, monkeypatch):
        result = _tool(monkeypatch).execute("visionclick 登录按钮")
        assert result.success is False
        assert "不支持" in result.error and "snapshot" in result.error


class TestResults:
    def test_snapshot_text_is_returned(self, monkeypatch, captured):
        captured["payloads"].append({"ok": True, "__out": {"snapshot": "button [ref=1]"}})
        result = _tool(monkeypatch).execute("snapshot")
        assert result.success and result.output == "button [ref=1]"

    def test_human_check_hint_on_challenge_page(self, monkeypatch, captured):
        captured["payloads"].append({"ok": True, "__out": {
            "url": "https://x.com/captcha", "snapshot": "请完成安全验证"}})
        result = _tool(monkeypatch).execute("humancheck")
        assert result.success and "人工" in result.output
        assert result.metadata.get("human_check") is True

    def test_screenshot_base64_goes_to_metadata(self, monkeypatch, captured, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"\x89PNG-fake-bytes")
        captured["payloads"].append({"ok": True, "__out": {"path": str(png)}})
        result = _tool(monkeypatch).execute("screenshot_base64")
        assert result.success
        payload = result.metadata["screenshot_base64"]
        assert base64.b64decode(payload) == png.read_bytes()
        assert "FULL_BASE64" not in result.output, "负载不该回灌 output（会被截断成坏图）"

    def test_screenshot_reports_path(self, monkeypatch, captured, tmp_path):
        png = tmp_path / "s.png"
        png.write_bytes(b"x")
        captured["payloads"].append({"ok": True, "__out": {"path": str(png)}})
        result = _tool(monkeypatch).execute("screenshot")
        assert result.success and str(png) in result.output


class TestAvailability:
    def test_requires_cli_node_and_chrome(self, monkeypatch):
        monkeypatch.setattr(ego_mod, "resolve_cli", lambda: "C:/cli.mjs")
        monkeypatch.setattr(ego_mod, "resolve_chrome", lambda: r"C:\Edge\msedge.exe")
        monkeypatch.setattr(ego_mod.shutil, "which", lambda name: "C:/node.exe")
        assert ego_available() is True
        monkeypatch.setattr(ego_mod, "resolve_chrome", lambda: "")
        assert ego_available() is False

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("BROWSER_EGO_CLI", "D:/custom/ego.mjs")
        monkeypatch.setenv("EGO_LINUX_CHROME", "D:/custom/chrome.exe")
        assert ego_mod.resolve_cli() == "D:/custom/ego.mjs"
        assert ego_mod.resolve_chrome() == "D:/custom/chrome.exe"


class TestBackendSelection:
    """_create_browser_tool 的后端优先级。"""

    def _select(self, monkeypatch, backend, bridge_up, ego_ok):
        from config import BROWSER_CONFIG
        from tools.tool_manager import _create_browser_tool
        monkeypatch.setitem(BROWSER_CONFIG, "backend", backend)
        monkeypatch.setitem(BROWSER_CONFIG, "embedded_auto_detect", True)
        monkeypatch.setitem(BROWSER_CONFIG, "ego_enabled", True)
        monkeypatch.delenv("MY_AGENT_EMBEDDED_BROWSER_URL", raising=False)
        import tools.embedded_browser as eb
        monkeypatch.setattr(eb, "probe_bridge", lambda *a, **kw: bridge_up)
        monkeypatch.setattr(ego_mod, "ego_available", lambda: ego_ok)
        return type(_create_browser_tool()).__name__

    def test_forced_backends(self, monkeypatch):
        assert self._select(monkeypatch, "playwright", True, True) == "BrowserTool"
        assert self._select(monkeypatch, "ego", False, False) == "EgoBrowserTool"
        assert self._select(monkeypatch, "embedded", True, False) == "EmbeddedBrowserTool"

    def test_auto_prefers_bridge_then_ego_then_playwright(self, monkeypatch):
        assert self._select(monkeypatch, "auto", True, True) == "EmbeddedBrowserTool"
        assert self._select(monkeypatch, "auto", False, True) == "EgoBrowserTool"
        assert self._select(monkeypatch, "auto", False, False) == "BrowserTool"

    def test_ego_disabled_falls_back_to_playwright(self, monkeypatch):
        from config import BROWSER_CONFIG
        from tools.tool_manager import _create_browser_tool
        monkeypatch.setitem(BROWSER_CONFIG, "backend", "auto")
        monkeypatch.setitem(BROWSER_CONFIG, "embedded_auto_detect", True)
        monkeypatch.setitem(BROWSER_CONFIG, "ego_enabled", False)
        import tools.embedded_browser as eb
        monkeypatch.setattr(eb, "probe_bridge", lambda *a, **kw: False)
        assert type(_create_browser_tool()).__name__ == "BrowserTool"
