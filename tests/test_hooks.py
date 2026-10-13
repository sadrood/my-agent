"""Hooks 机制测试（最小可用实现，不依赖网络）。"""
import json
import sys
import textwrap

from agent.executor import Executor
from agent.hook_events import (
    EventHookRunner, HookDecision, matches_tool, normalize_event, parse_hooks,
)
from agent.hooks import HookManager, get_hook_manager, reset_hook_manager, result_payload
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


# ======================================================================
# 事件化 Hook 协议：hooks.json 外部命令 + exit code / stdout JSON 决策
# ======================================================================

def _write_script(tmp_path, body, name="hook_script.py"):
    """把 hook 脚本体写入临时文件，返回可直接执行的命令行。"""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return f'"{sys.executable}" "{p}"'


def _write_events(tmp_path, events, name="hooks.json"):
    """写入 hooks.json，返回路径。"""
    p = tmp_path / name
    p.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    return str(p)


class TestHookEventProtocol:
    def test_parse_wrapped_and_direct_forms(self, tmp_path):
        wrapped = {"hooks": {"PreToolUse": [
            {"matcher": "edit|write",
             "hooks": [{"type": "command", "command": "a.py", "timeout": 3}]}]}}
        direct = {"Stop": [{"command": "b.py"}]}
        h1 = parse_hooks(wrapped, default_timeout=10)
        h2 = parse_hooks(direct)
        assert len(h1) == 1 and h1[0].event == "PreToolUse"
        assert h1[0].matcher == "edit|write" and h1[0].command == "a.py"
        assert h1[0].timeout == 3
        assert len(h2) == 1 and h2[0].event == "Stop" and h2[0].timeout == 10

    def test_non_command_type_is_skipped(self):
        hooks = {"PreToolUse": [{"type": "prompt", "prompt": "judge it"}]}
        assert parse_hooks(hooks) == []

    def test_event_and_matcher_normalization(self):
        assert normalize_event("pre_tool_use") == "PreToolUse"
        assert normalize_event("user-prompt-submit") == "UserPromptSubmit"
        assert normalize_event("my-custom-event") == "my-custom-event"
        assert matches_tool("", "edit") and matches_tool("*", "edit")
        assert matches_tool("edit|write", "Write")
        assert not matches_tool("edit|write", "terminal")
        assert matches_tool("term*", "terminal")

    def test_exit_code_2_blocks_with_stderr_reason(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print("dangerous command", file=sys.stderr)
sys.exit(2)
''')
        runner = EventHookRunner(
            events_file=_write_events(tmp_path, {"PreToolUse": [
                {"matcher": "terminal", "hooks": [{"command": cmd}]}]}),
            cwd=str(tmp_path))
        decision = runner.run("PreToolUse", {"tool_name": "terminal"})
        assert decision.blocked is True
        assert "dangerous" in decision.reason

    def test_exit_code_1_is_notice_not_block(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print("remember to run tests", file=sys.stderr)
sys.exit(1)
''')
        runner = EventHookRunner(
            events_file=_write_events(tmp_path, {"Stop": [{"command": cmd}]}),
            cwd=str(tmp_path))
        decision = runner.run("Stop", {})
        assert decision.blocked is False
        assert "run tests" in " ".join(decision.messages)

    def test_stdout_json_block(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"decision": "block", "reason": "no tests"}))
''')
        runner = EventHookRunner(events_file=_write_events(
            tmp_path, {"Stop": [{"command": cmd}]}), cwd=str(tmp_path))
        d = runner.run("Stop", {})
        assert d.blocked and d.reason == "no tests"

    def test_permission_decision_deny_and_updated_input(self, tmp_path):
        deny = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}}))
''', name="deny.py")
        allow = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow",
                                         "updatedInput": {"command": "echo safe"}},
                  "systemMessage": "rewritten"}))
''', name="allow.py")
        r_deny = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": deny}]}, name="d.json"), cwd=str(tmp_path))
        r_allow = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": allow}]}, name="a.json"), cwd=str(tmp_path))
        d1 = r_deny.run("PreToolUse", {"tool_name": "terminal"})
        d2 = r_allow.run("PreToolUse", {"tool_name": "terminal"})
        assert d1.blocked and d1.permission == "deny"
        assert not d2.blocked and d2.permission == "allow"
        assert d2.updated_input == {"command": "echo safe"}
        assert "rewritten" in d2.messages

    def test_plain_text_stdout_becomes_message(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print("just a reminder")
''')
        runner = EventHookRunner(events_file=_write_events(
            tmp_path, {"PostToolUse": [{"command": cmd}]}), cwd=str(tmp_path))
        d = runner.run("PostToolUse", {"tool_name": "edit"})
        assert not d.blocked and "just a reminder" in d.messages

    def test_hook_receives_stdin_payload(self, tmp_path):
        seen_file = tmp_path / "seen.json"
        cmd = _write_script(tmp_path, f'''
import json, sys
data = json.load(sys.stdin)
open({str(seen_file)!r}, "w", encoding="utf-8").write(json.dumps(data))
''')
        runner = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": cmd}]}), cwd=str(tmp_path))
        runner.run("PreToolUse", {"tool_name": "edit", "tool_input": {"file_path": "a.py"}})
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
        assert seen["hook_event_name"] == "PreToolUse"
        assert seen["tool_name"] == "edit"
        assert seen["tool_input"] == {"file_path": "a.py"}
        assert "cwd" in seen

    def test_timeout_and_broken_command_are_fail_open(self, tmp_path):
        slow = _write_script(tmp_path, '''
import json, sys, time
json.load(sys.stdin)
time.sleep(3)
''')
        runner = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": slow, "timeout": 0.4}]}), cwd=str(tmp_path))
        d = runner.run("PreToolUse", {"tool_name": "edit"})
        assert d.blocked is False and d.ran == 1

        broken = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": "definitely-not-a-command-xyz"}]},
            name="broken.json"), cwd=str(tmp_path))
        assert broken.run("PreToolUse", {"tool_name": "edit"}).blocked is False

    def test_missing_or_invalid_file_is_silent(self, tmp_path):
        missing = EventHookRunner(events_file=str(tmp_path / "nope.json"), cwd=str(tmp_path))
        assert missing.hooks == [] and missing.load_error is None
        assert missing.run("Stop", {}).blocked is False

        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        invalid = EventHookRunner(events_file=str(bad), cwd=str(tmp_path))
        assert invalid.hooks == [] and invalid.load_error
        assert invalid.run("Stop", {}).blocked is False

    def test_disabled_runner_never_executes(self, tmp_path):
        marker = tmp_path / "ran.txt"
        cmd = _write_script(tmp_path, f'''
open({str(marker)!r}, "w", encoding="utf-8").write("ran")
''')
        runner = EventHookRunner(events_file=_write_events(
            tmp_path, {"PreToolUse": [{"command": cmd}]}), enabled=False, cwd=str(tmp_path))
        assert runner.run("PreToolUse", {"tool_name": "edit"}).ran == 0
        assert not marker.exists()


class TestHookManagerEvents:
    def test_manager_blocks_tool_via_events_file(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print("no rm -rf", file=sys.stderr)
sys.exit(2)
''')
        mgr = HookManager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                          events_file=_write_events(tmp_path, {"PreToolUse": [
                              {"matcher": "terminal",
                               "hooks": [{"command": cmd}]}]}),
                          project_dir=str(tmp_path))
        d = mgr.on_pre_tool_use("terminal", {"command": "rm -rf /"})
        assert d.blocked and "rm -rf" in d.reason
        assert mgr.on_pre_tool_use("edit", {}).blocked is False     # matcher 不命中

    def test_manager_stop_and_user_prompt_events(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
data = json.load(sys.stdin)
if data["hook_event_name"] == "Stop":
    print(json.dumps({"decision": "block", "reason": "tests not run"}))
else:
    print(json.dumps({"systemMessage": "prompt seen"}))
''')
        mgr = HookManager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                          events_file=_write_events(tmp_path, {
                              "Stop": [{"command": cmd}],
                              "UserPromptSubmit": [{"command": cmd}]}),
                          project_dir=str(tmp_path))
        assert mgr.on_stop({"goal": "x"}).blocked is True
        assert "prompt seen" in mgr.on_user_prompt("hi").messages

    def test_python_callback_may_return_decision_dict(self, tmp_path):
        path = _write_hooks(tmp_path, '''
def on_pre_tool_use(tool_name, arguments):
    return {"decision": "block", "reason": "callback veto"}
''')
        mgr = HookManager(enabled=True, hooks_file=path,
                          events_file=str(tmp_path / "none.json"),
                          project_dir=str(tmp_path))
        d = mgr.on_pre_tool_use("edit", {})
        assert d.blocked and d.reason == "callback veto"

    def test_disabled_manager_returns_empty_decision(self, tmp_path):
        mgr = HookManager(enabled=False, hooks_file=None, events_file=None)
        assert mgr.on_pre_tool_use("edit", {}).acts is False
        assert mgr.on_stop({}).blocked is False
        assert mgr.on_user_prompt("hi").messages == ()
        assert mgr.dispatch("Anything", {}).acts is False


class _CountingToolManager(_FakeToolManager):
    """记录 execute_json 调用次数，用于断言"工具确实没被执行"。"""

    def __init__(self):
        self.calls = 0

    def execute_json(self, name, args):
        self.calls += 1
        return super().execute_json(name, args)


class TestExecutorEventHooks:
    def test_event_hook_blocks_execution(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print("blocked by policy", file=sys.stderr)
sys.exit(2)
''')
        reset_hook_manager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                           events_file=_write_events(tmp_path, {"PreToolUse": [
                               {"matcher": "terminal",
                                "hooks": [{"command": cmd}]}]}),
                           project_dir=str(tmp_path))
        try:
            tm = _CountingToolManager()
            ex = Executor(tool_manager=tm, llm=object())
            result, blocked = ex._dispatch_tool_call(
                "terminal", {"command": "echo hi"}, "goal")
            assert result.success is False and blocked
            assert "钩子拦截" in result.error
            assert tm.calls == 0                # 工具没有被执行
        finally:
            reset_hook_manager(enabled=False)

    def test_post_hook_message_prefixed_to_output(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"systemMessage": "记得跑测试"}))
''')
        reset_hook_manager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                           events_file=_write_events(tmp_path, {"PostToolUse": [
                               {"matcher": "terminal",
                                "hooks": [{"command": cmd}]}]}),
                           project_dir=str(tmp_path))
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())
            result, blocked = ex._dispatch_tool_call(
                "terminal", {"command": "echo hi"}, "goal")
            assert result.success and not blocked
            assert "记得跑测试" in result.output
        finally:
            reset_hook_manager(enabled=False)

    def test_event_hook_timeout_is_fail_open(self, tmp_path):
        slow = _write_script(tmp_path, '''
import json, sys, time
json.load(sys.stdin)
time.sleep(3)
''')
        reset_hook_manager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                           events_file=_write_events(tmp_path, {"PreToolUse": [
                               {"command": slow, "timeout": 0.4}]}),
                           project_dir=str(tmp_path))
        try:
            ex = Executor(tool_manager=_FakeToolManager(), llm=object())
            result, blocked = ex._dispatch_tool_call("edit", {}, "goal")
            assert result.success and not blocked
        finally:
            reset_hook_manager(enabled=False)


class _RecordingToolManager(_FakeToolManager):
    """记录 execute_json 收到的参数（验证 updatedInput 真的改写了参数）。"""

    def __init__(self):
        self.seen = []

    def execute_json(self, name, args):
        self.seen.append((name, args))
        return ToolResult(success=True, output="fake ok")


class _FailingToolManager(_FakeToolManager):
    def execute_json(self, name, args):
        return ToolResult(success=False, output="", error="工具失败了")


class TestHookDecisionWiring:
    """决策字段是否**真的**接到执行路径上 —— 不是只解析不消费。"""

    def test_updated_input_rewrites_arguments(self, tmp_path):
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {"updatedInput": {"command": "echo 被改写"}}}))
''')
        reset_hook_manager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                           events_file=_write_events(tmp_path, {"PreToolUse": [
                               {"matcher": "terminal", "hooks": [{"command": cmd}]}]}),
                           project_dir=str(tmp_path))
        try:
            tm = _RecordingToolManager()
            ex = Executor(tool_manager=tm, llm=object())
            result, blocked = ex._dispatch_tool_call(
                "terminal", {"command": "rm -rf /"}, "goal")
            assert result.success and not blocked
            assert tm.seen == [("terminal", {"command": "echo 被改写"})], \
                "updatedInput 没生效：工具拿到的还是原参数"
        finally:
            reset_hook_manager(enabled=False)

    def test_post_hook_message_not_prepended_on_failure(self, tmp_path):
        """失败结果不拼"钩子提示"—— 否则真正的错误信息会被提示语淹没。"""
        cmd = _write_script(tmp_path, '''
import json, sys
json.load(sys.stdin)
print(json.dumps({"systemMessage": "记得跑测试"}))
''')
        reset_hook_manager(enabled=True, hooks_file=str(tmp_path / "none.py"),
                           events_file=_write_events(tmp_path, {"PostToolUse": [
                               {"matcher": "terminal", "hooks": [{"command": cmd}]}]}),
                           project_dir=str(tmp_path))
        try:
            ex = Executor(tool_manager=_FailingToolManager(), llm=object())
            result, _ = ex._dispatch_tool_call("terminal", {"command": "echo hi"}, "goal")
            assert result.success is False
            assert "钩子提示" not in (result.output or "")
        finally:
            reset_hook_manager(enabled=False)

    def test_callback_may_return_decision_object(self, tmp_path):
        """Python 回调返回 HookDecision 对象（不只是 dict）也要被认。"""
        module = _write_hooks(tmp_path, '''
from agent.hook_events import HookDecision


def on_pre_tool_use(tool_name, arguments):
    return HookDecision(blocked=True, reason="模块回调拦截")
''')
        reset_hook_manager(enabled=True, hooks_file=module)
        try:
            tm = _CountingToolManager()
            ex = Executor(tool_manager=tm, llm=object())
            result, blocked = ex._dispatch_tool_call("edit", {}, "goal")
            assert result.success is False and "模块回调拦截" in (result.error or "")
            assert tm.calls == 0
        finally:
            reset_hook_manager(enabled=False)


class TestDecisionMergingAndPayload:
    """纯逻辑：决策合并与结果摘要（此前没有直接测试）。"""

    def test_merge_blocked_wins_and_messages_concat(self):
        a = HookDecision(messages=("一",))
        b = HookDecision(blocked=True, reason="不行", messages=("二",), permission="deny")
        merged = a.merge(b)
        assert merged.blocked is True and merged.reason == "不行"
        assert merged.messages == ("一", "二") and merged.permission == "deny"

    def test_merge_keeps_first_nonempty_reason(self):
        assert HookDecision(reason="先说的").merge(
            HookDecision(blocked=True, reason="后说的")).reason == "先说的"

    def test_result_payload_truncates_and_extracts(self):
        class R:
            success = True
            output = "x" * 50
            error = "boom"
            truncated = False
        p = result_payload(R(), limit=10)
        assert p["output"] == "x" * 10 and p["output_truncated"] is True
        assert p["error"] == "boom" and p["success"] is True
