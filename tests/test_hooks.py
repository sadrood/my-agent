"""
Hooks 机制测试（最小可用实现，不依赖网络）。

覆盖：
1. 钩子文件正常加载并生效（HookManager 与 Executor 集成两层）
2. 文件不存在时静默（不抛异常、不加载）
3. 回调抛异常时 fail-open 不阻断主流程
4. enabled=false 时不加载（即使文件存在）
"""
import json
import textwrap

from agent.executor import Executor
from agent.hooks import HookManager, get_hook_manager, reset_hook_manager
from tools.base import ToolResult


def _write_hooks(tmp_path, body, name="hooks.py"):
    """把钩子模块源码写入临时目录，返回文件路径。"""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def _log_entries(log_file):
    """读取回调日志文件，返回 [dict, ...]；文件不存在返回 []。"""
    if not log_file.exists():
        return []
    return [json.loads(line) for line in
            log_file.read_text(encoding="utf-8").strip().splitlines() if line.strip()]


def _hooks_logging_body(log_file):
    """生成把每次回调追加写入 log_file 的钩子模块源码。"""
    return f'''
import json

_LOG = {str(log_file)!r}

def _log(entry):
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\\n")

def on_pre_tool_use(tool_name, arguments):
    _log({{"event": "pre", "tool": tool_name, "args": arguments}})

def on_post_tool_use(tool_name, result):
    _log({{"event": "post", "tool": tool_name,
           "output": getattr(result, "output", "")}})
'''


class TestHookManager:
    def test_load_and_call_callbacks(self, tmp_path):
        log = tmp_path / "calls.jsonl"
        mgr = HookManager(enabled=True, hooks_file=_write_hooks(
            tmp_path, _hooks_logging_body(log)))

        assert mgr.pre_callback is not None
        assert mgr.post_callback is not None

        mgr.on_pre_tool_use("edit", {"file_path": "a.py"})
        mgr.on_post_tool_use("edit", ToolResult(success=True, output="ok"))

        entries = _log_entries(log)
        assert len(entries) == 2
        assert entries[0]["event"] == "pre"
        assert entries[0]["tool"] == "edit"
        assert entries[0]["args"] == {"file_path": "a.py"}
        assert entries[1]["event"] == "post"
        assert entries[1]["tool"] == "edit"
        assert entries[1]["output"] == "ok"

    def test_optional_callbacks_may_be_missing(self, tmp_path):
        """只定义 on_pre_tool_use 时，post 回调缺失不报错。"""
        log = tmp_path / "pre_only.jsonl"
        path = _write_hooks(tmp_path, f'''
def on_pre_tool_use(tool_name, arguments):
    with open({str(log)!r}, "w", encoding="utf-8") as f:
        f.write("pre:" + tool_name)
''')
        mgr = HookManager(enabled=True, hooks_file=path)
        assert mgr.pre_callback is not None
        assert mgr.post_callback is None      # 可选函数缺失静默
        mgr.on_pre_tool_use("edit", {})
        assert log.read_text(encoding="utf-8") == "pre:edit"
        mgr.on_post_tool_use("edit", object())  # 无 post 回调，静默跳过

    def test_missing_file_is_silent(self, tmp_path):
        """文件不存在：不抛异常、不加载回调。"""
        mgr = HookManager(enabled=True, hooks_file=str(tmp_path / "nope.py"))
        assert mgr.pre_callback is None
        assert mgr.post_callback is None
        # 调用同样不抛异常
        mgr.on_pre_tool_use("edit", {})
        mgr.on_post_tool_use("edit", ToolResult(success=True, output="x"))

    def test_callback_exception_is_fail_open(self, tmp_path):
        """回调抛异常：只警告，不阻断调用方。"""
        path = _write_hooks(tmp_path, '''
def on_pre_tool_use(tool_name, arguments):
    raise RuntimeError("pre boom")

def on_post_tool_use(tool_name, result):
    raise RuntimeError("post boom")
''')
        mgr = HookManager(enabled=True, hooks_file=path)
        mgr.on_pre_tool_use("edit", {"x": 1})     # 不抛
        mgr.on_post_tool_use("edit", ToolResult(success=True, output="ok"))  # 不抛
        assert mgr.pre_callback is not None
        assert mgr.post_callback is not None

    def test_disabled_does_not_load(self, tmp_path):
        """enabled=false：即使文件存在也不加载、不调用。"""
        log = tmp_path / "never.jsonl"
        path = _write_hooks(tmp_path, f'''
def on_pre_tool_use(tool_name, arguments):
    open({str(log)!r}, "w", encoding="utf-8").write("called")
''')
        mgr = HookManager(enabled=False, hooks_file=path)
        assert mgr.pre_callback is None
        mgr.on_pre_tool_use("edit", {})
        assert not log.exists()

    def test_singleton_and_reset(self, tmp_path):
        a = get_hook_manager()
        b = get_hook_manager()
        assert a is b
        c = reset_hook_manager(enabled=False, hooks_file=str(tmp_path / "x.py"))
        assert c is not a
        assert get_hook_manager() is c
        reset_hook_manager(enabled=False)   # 恢复默认配置，避免影响其它测试


class _Request:
    """审批请求替身：executor 只读 risk_level。"""
    risk_level = "high"


class _FakeToolManager:
    """极简 ToolManager 替身：_dispatch_tool_call 只用到这两个方法。"""

    def execute_json(self, name, args):
        return ToolResult(success=True, output="fake ok")

    def build_approval_request(self, name, args):
        return _Request()


class TestExecutorHooks:
    def test_dispatch_invokes_pre_and_post(self, tmp_path):
        log = tmp_path / "exec_calls.jsonl"
        reset_hook_manager(enabled=True, hooks_file=_write_hooks(
            tmp_path, _hooks_logging_body(log)))
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())
            result, blocked = ex._dispatch_tool_call(
                "edit", {"file_path": "a.py", "content": "x"}, "goal")
            assert result.success and blocked == ""

            entries = _log_entries(log)
            assert [e["event"] for e in entries] == ["pre", "post"]
            assert entries[0]["tool"] == "edit"
            assert entries[0]["args"] == {"file_path": "a.py", "content": "x"}
            assert entries[1]["tool"] == "edit"
            assert entries[1]["output"] == "fake ok"
        finally:
            reset_hook_manager(enabled=False)

    def test_blocked_call_triggers_no_hooks(self, tmp_path):
        """被审批拦截的调用不执行工具、也不触发任何钩子。"""
        log = tmp_path / "blocked.jsonl"
        reset_hook_manager(enabled=True, hooks_file=_write_hooks(
            tmp_path, _hooks_logging_body(log)))
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())

            class Deny:
                def decide(self, request):
                    return type("D", (), {"allowed": False, "reason": "denied"})()
            ex.approval = Deny()

            result, blocked = ex._dispatch_tool_call("edit", {}, "goal")
            assert blocked
            assert _log_entries(log) == []   # 钩子在审批/Guardian 通过之后才触发
        finally:
            reset_hook_manager(enabled=False)

    def test_dispatch_fail_open_when_hook_raises(self, tmp_path):
        """pre 钩子抛异常时工具照常执行，post 钩子照常触发。"""
        log = tmp_path / "exec_ok.jsonl"
        path = _write_hooks(tmp_path, f'''
def on_pre_tool_use(tool_name, arguments):
    raise RuntimeError("pre hook boom")

def on_post_tool_use(tool_name, result):
    with open({str(log)!r}, "a", encoding="utf-8") as f:
        f.write("post:" + tool_name + "\\n")
''')
        reset_hook_manager(enabled=True, hooks_file=path)
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())
            result, blocked = ex._dispatch_tool_call(
                "terminal", {"command": "echo hi"}, "goal")
            assert result.success and not blocked
            assert log.read_text(encoding="utf-8").strip() == "post:terminal"
        finally:
            reset_hook_manager(enabled=False)

    def test_disabled_manager_no_hooks_in_dispatch(self, tmp_path):
        log = tmp_path / "never.jsonl"
        path = _write_hooks(tmp_path, f'''
def on_pre_tool_use(tool_name, arguments):
    open({str(log)!r}, "w", encoding="utf-8").write("called")
''')
        reset_hook_manager(enabled=False, hooks_file=path)
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())
            result, blocked = ex._dispatch_tool_call("edit", {}, "goal")
            assert result.success and not blocked
            assert not log.exists()
        finally:
            reset_hook_manager(enabled=False)