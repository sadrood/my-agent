"""
内置工具 v2 接口测试：schema、execute_json、审批元数据。
"""
import os
import json

from tools.terminal import TerminalTool
from tools.file import FileTool
from tools.python import PythonTool
from tools.browser import BrowserTool
from tools.tool_manager import ToolManager


class TestTerminalTool:
    def test_schema(self):
        s = TerminalTool().schema
        assert s["required"] == ["command"]

    def test_execute_json_echo(self):
        r = TerminalTool().execute_json({"command": "echo hello"})
        assert r.success is True
        assert "hello" in r.output

    def test_blocked_command_rejected_in_tool(self):
        r = TerminalTool().execute("rm -rf /")
        assert r.success is False
        assert "黑名单" in r.error

    def test_approval_request_risk(self):
        t = TerminalTool()
        r = t.build_approval_request({"command": "rm -rf build/"})
        assert r.risk_level == "high"
        r2 = t.build_approval_request({"command": "pip list"})
        assert r2.risk_level == "low"


class TestFileTool:
    def test_schema_enum(self):
        s = FileTool().schema
        assert s["properties"]["operation"]["enum"] == ["read", "write", "list", "exists", "info"]

    def test_write_read_roundtrip(self, tmp_path):
        t = FileTool()
        f = tmp_path / "a.txt"
        w = t.execute_json({"operation": "write", "path": str(f), "content": "你好"})
        assert w.success is True
        r = t.execute_json({"operation": "read", "path": str(f)})
        assert r.success is True
        assert "你好" in r.output

    def test_write_requires_workspace(self):
        t = FileTool()
        req = t.build_approval_request({"operation": "write", "path": "x", "content": "y"})
        assert req.min_sandbox_mode == "workspace-write"
        req2 = t.build_approval_request({"operation": "read", "path": "x"})
        assert req2.min_sandbox_mode == "read-only"
        assert req2.risk_level == "low"


class TestPythonTool:
    def test_execute_json(self):
        r = PythonTool().execute_json({"code": "print(1 + 2)"})
        assert r.success is True
        assert "3" in r.output

    def test_no_subprocess(self):
        """v2 安全收紧：受限命名空间不提供 subprocess。"""
        r = PythonTool().execute_json({"code": "import subprocess"})
        assert r.success is False
        assert "ModuleNotFoundError" in r.output or "subprocess" in r.output

    def test_os_available(self):
        r = PythonTool().execute_json({"code": "import os; print('makedirs' in dir(os))"})
        assert r.success is True
        assert "True" in r.output

    def test_high_risk_detection(self):
        req = PythonTool().build_approval_request({"code": "import subprocess; subprocess.run('dir')"})
        assert req.risk_level == "high"


class TestBrowserTool:
    def test_schema_has_commands(self):
        s = BrowserTool().schema
        enum = s["properties"]["command"]["enum"]
        assert "goto" in enum and "clickat" in enum and "launch" in enum
        assert "snapshot" in enum          # accessibility 结构快照（免截图看布局）

    def test_snapshot_is_readonly(self):
        b = BrowserTool()
        assert "snapshot" in b.READONLY_COMMANDS
        req = b.build_approval_request({"command": "snapshot"})
        assert req.min_sandbox_mode == "read-only"
        assert req.risk_level == "low"

    def test_snapshot_rendering(self, monkeypatch, tmp_path):
        # 用假 page 验证 accessibility 树渲染（无需真实浏览器）
        from tools.base import ToolResult

        class FakePage:
            url = "https://example.com"

            class _Acc:
                def snapshot(self):
                    return {
                        "role": "WebArea",
                        "name": "Example",
                        "children": [
                            {"role": "button", "name": "登录"},
                            {"role": "textbox", "name": "用户名", "required": True},
                            {"role": "heading", "name": "欢迎", "level": 1},
                            {"role": "checkbox", "name": "记住我", "checked": True},
                        ],
                    }

            accessibility = _Acc()

        b = BrowserTool(screenshot_dir=str(tmp_path))
        b._pages = [FakePage()]
        b._current_page_idx = 0
        monkeypatch.setattr(b, "_ensure_page",
                            lambda: ToolResult(success=True, output=""))

        r = b.execute("snapshot")
        assert r.success is True
        assert "[页面结构快照]" in r.output
        assert "WebArea" in r.output and "https://example.com" in r.output
        assert 'button "登录"' in r.output
        assert "required" in r.output
        assert "level=1" in r.output
        assert "checked=True" in r.output

    def test_snapshot_aria_primary_path(self, monkeypatch, tmp_path):
        # 新版 Playwright 走 locator.aria_snapshot（Playwright MCP 同款格式）
        from tools.base import ToolResult

        class FakeLocator:
            def aria_snapshot(self, depth=None):
                return ('- button "登录"\n'
                        '- textbox "用户名" [required]\n'
                        '- heading "欢迎" [level=1]')

        class FakePage:
            url = "https://example.com"

            def locator(self, sel):
                return FakeLocator()

        b = BrowserTool(screenshot_dir=str(tmp_path))
        b._pages = [FakePage()]
        b._current_page_idx = 0
        monkeypatch.setattr(b, "_ensure_page",
                            lambda: ToolResult(success=True, output=""))

        r = b.execute("snapshot")
        assert r.success is True
        assert "[页面结构快照]" in r.output
        assert 'button "登录"' in r.output
        assert "required" in r.output
        assert "level=1" in r.output

    def test_snapshot_empty_tree(self, monkeypatch, tmp_path):
        from tools.base import ToolResult

        class FakePage:
            url = ""

            class _Acc:
                def snapshot(self):
                    return None

            accessibility = _Acc()

        b = BrowserTool(screenshot_dir=str(tmp_path))
        b._pages = [FakePage()]
        b._current_page_idx = 0
        monkeypatch.setattr(b, "_ensure_page",
                            lambda: ToolResult(success=True, output=""))
        r = b.execute("snapshot")
        assert r.success is True
        assert "无可访问性树" in r.output

    def test_readonly_command_classification(self):
        b = BrowserTool()
        req = b.build_approval_request({"command": "title"})
        assert req.min_sandbox_mode == "read-only"
        assert req.risk_level == "low"
        req2 = b.build_approval_request({"command": "goto", "args": "https://example.com"})
        assert req2.min_sandbox_mode == "workspace-write"


class TestTerminalBackground:
    def _bg_code(self):
        return "import time; [print(i, flush=True) or time.sleep(0.2) for i in range(60)]"

    def _start_job(self, t):
        import sys
        r = t.execute_json({
            "command": f'{sys.executable} -c "{self._bg_code()}"',
            "background": True,
        })
        assert r.success, r.error
        import re
        m = re.search(r"job-\d+-\d+", r.output)
        assert m, f"输出中没有任务 ID: {r.output}"
        return m.group(0)

    def test_background_start_list_kill(self):
        import time
        t = TerminalTool()
        job_id = self._start_job(t)
        try:
            lst = t.execute("bg list")
            assert job_id in lst.output and "运行中" in lst.output

            killed = t.execute(f"bg kill {job_id}")
            assert killed.success and "已终止" in killed.output

            lst2 = t.execute("bg list")
            assert job_id in lst2.output and "已结束" in lst2.output
        finally:
            t.execute(f"bg kill {job_id}")

    def test_background_output_streams_and_readable_after_kill(self):
        import time
        t = TerminalTool()
        job_id = self._start_job(t)
        try:
            time.sleep(0.8)   # 等几行输出落地
            out = t.execute(f"bg output {job_id} 5")
            assert out.success
            assert "0" in out.output and "1" in out.output
            assert "仍在运行" in out.output

            t.execute(f"bg kill {job_id}")
            out2 = t.execute(f"bg output {job_id} 5")
            assert out2.success and "已结束" in out2.output
        finally:
            t.execute(f"bg kill {job_id}")

    def test_bg_unknown_job_errors(self):
        t = TerminalTool()
        r = t.execute("bg output nosuch-job")
        assert r.success is False and "不存在" in r.error
        r2 = t.execute("bg kill nosuch-job")
        assert r2.success is False and "不存在" in r2.error

    def test_bg_schema_has_background_flag(self):
        s = TerminalTool().schema
        assert "background" in s["properties"]
        assert s["required"] == ["command"]


class TestToolManager:
    def test_registered_tools(self):
        tm = ToolManager()
        names = tm.list_tools()
        for expected in ("terminal", "file", "python", "browser", "see"):
            assert expected in names

    def test_execute_json_unknown_tool(self):
        tm = ToolManager()
        r = tm.execute_json("no_such_tool", {})
        assert r.success is False

    def test_execute_json_truncation(self):
        tm = ToolManager(output_max_chars=200)
        r = tm.execute_json("python", {"code": "print('x' * 5000)"})
        assert r.truncated is True
        assert len(r.output) < 1000

    def test_list_openai_schemas(self):
        tm = ToolManager()
        schemas = tm.list_openai_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "terminal" in names
        assert "see" in names
        assert all(s["type"] == "function" for s in schemas)

    def test_reset_tool_calls_reset_method(self):
        # 优先调用实例的 reset()（同一实例，SeeTool 等持有引用的工具依然有效）
        tm = ToolManager()
        before = tm.get_tool("browser")
        tm.reset_tool("browser")
        after = tm.get_tool("browser")
        assert after is before          # 同一实例
        assert after._pages == []       # reset() 清空了页面状态
        assert after._playwright is None

    def test_reset_tool_reinstantiates_when_no_reset(self):
        # 没有 reset() 方法的工具退回重新实例化
        class _NoReset:
            def __init__(self):
                self.name = "x"

        tm = ToolManager()
        old = _NoReset()
        tm.register(old)
        tm.reset_tool("x")
        assert tm.get_tool("x") is not old


def test_terminal_output_stream_callback():
    """terminal 执行期间增量回调应收到输出行（实时流）。"""
    from tools.terminal import TerminalTool
    tool = TerminalTool()
    chunks = []
    tool.set_output_callback(chunks.append)
    try:
        r = tool.execute_json({"command": "echo hello-stream"})
        assert r.success
        assert any("hello-stream" in c for c in chunks), f"回调未收到输出: {chunks[:3]}"
    finally:
        tool.set_output_callback(None)
