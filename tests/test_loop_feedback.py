# -*- coding: utf-8 -*-
"""单循环的"反馈给模型"一侧：失败保留输出、打转提醒、批内去重、进展判定。"""
from agent.approval import ApprovalPolicy
from agent.executor import Executor
from agent.loop_budget import LoopBudget, StallWatch, TurnOutcome
from models.llm import LLMToolResponse, ToolCall
from tools.base import BaseTool, ToolResult
from tools.tool_manager import ToolManager


class FakeLLM:
    """脚本化假 LLM，并记录每次调用时的 messages 快照。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        return self.script.pop(0)


class ProbeTool(BaseTool):
    """只读测试工具：记录被执行的参数，返回可注入的结果。"""

    name = "probe"
    description = "测试用只读工具"
    risk_level = "low"
    parallel_safe = True

    def __init__(self, result=None):
        self.runs = []
        self._result = result or ToolResult(success=True, output="ok")

    @property
    def schema(self):
        return {"type": "object", "properties": {"key": {"type": "string"}}}

    def execute(self, input_str):
        self.runs.append(input_str)
        return self._result

    def execute_json(self, arguments):
        self.runs.append(dict(arguments or {}))
        return ToolResult(success=self._result.success, output=self._result.output,
                          error=self._result.error, truncated=self._result.truncated,
                          metadata=dict(self._result.metadata))


def _executor(llm, tool):
    manager = ToolManager()
    manager._tools["probe"] = tool
    return Executor(
        tool_manager=manager,
        llm=llm,
        approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                       interactive=False),
        guardian=None,
        rollout=None,
        instructions_text="",
        max_step_ops=10,
        llm_retry_delay=0,
    )


def _call(key="a", call_id="1"):
    return ToolCall(call_id, "probe", {"key": key})


def _tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool"]


def _nudges(messages):
    return [m for m in messages
            if m.get("role") == "user" and "[系统提示]" in str(m.get("content", ""))]


class TestFailureKeepsOutput:
    def test_partial_output_survives_failure(self):
        llm = FakeLLM([LLMToolResponse(content="", tool_calls=[_call()]),
                       LLMToolResponse(content="完成")])
        ex = _executor(llm, ProbeTool(ToolResult(success=False, output="已写入 3 行",
                                                 error="磁盘满了")))
        ex.execute_goal_loop(goal="g", system_prompt="s")
        content = _tool_messages(llm.calls[1])[0]["content"]
        assert "磁盘满了" in content, "错误原因不能丢"
        assert "已写入 3 行" in content, "失败前产生的输出必须一起回给模型"

    def test_blocked_wins_over_output(self):
        assert Executor._tool_message_text(ToolResult(True, "ok"), "被拦") == "被拦截: 被拦"
        assert Executor._tool_message_text(ToolResult(False, "", "boom")) == "执行失败: boom"
        text = Executor._tool_message_text(ToolResult(False, "半截", "boom"))
        assert text.startswith("执行失败: boom") and "半截" in text


class TestStallWatch:
    def test_repeat_nudge_at_threshold_and_periodic(self):
        w = StallWatch(repeat_after=2)
        assert w.observe(True) == ""              # 第 1 次重复还不提示
        msg = w.observe(True)                     # 第 2 次重复 → 提示
        assert "完全相同的工具调用" in msg
        assert w.observe(True) == ""              # 第 3 次不重复提示
        assert "完全相同的工具调用" in w.observe(True)

    def test_repeat_streak_resets_on_new_call(self):
        w = StallWatch(repeat_after=2)
        w.observe(True)
        assert w.observe(False) == ""
        assert w.repeat_streak == 0

    def test_failure_nudge_carries_last_error(self):
        w = StallWatch(fail_after=3)
        sig = ("probe(key='a')",)
        assert w.observe(False, sig, "第一错") == ""
        assert w.observe(False, sig, "第二错") == ""
        msg = w.observe(False, sig, "连接超时")
        assert "连续失败 3 次" in msg and "连接超时" in msg

    def test_failure_streak_only_counts_identical_calls(self):
        w = StallWatch(fail_after=2)
        w.observe(False, ("A",), "x")
        assert w.observe(False, ("B",), "y") == ""
        assert w.fail_streak == 1


class TestNudgeInLoop:
    def test_repeated_call_gets_a_nudge(self):
        llm = FakeLLM([LLMToolResponse(content="", tool_calls=[_call()]) for _ in range(4)]
                      + [LLMToolResponse(content="完成")])
        tool = ProbeTool()
        ex = _executor(llm, tool)
        ex.execute_goal_loop(goal="g", system_prompt="s")
        assert len(tool.runs) == 4, "同一调用跨轮不该被吞掉（只提醒，不替模型做决定）"
        assert any(_nudges(snapshot) for snapshot in llm.calls[2:]), \
            "连续原样重复后必须把'你在重复'告诉模型"

    def test_failing_call_gets_error_specific_nudge(self):
        llm = FakeLLM([LLMToolResponse(content="", tool_calls=[_call()]) for _ in range(4)]
                      + [LLMToolResponse(content="完成")])
        ex = _executor(llm, ProbeTool(ToolResult(success=False, output="", error="连接超时")))
        ex.execute_goal_loop(goal="g", system_prompt="s")
        nudges = [n["content"] for snapshot in llm.calls for n in _nudges(snapshot)]
        assert any("连续失败 3 次" in text and "连接超时" in text for text in nudges)


class TestBatchDedup:
    def test_identical_read_calls_run_once(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[_call("a", "1"), _call("a", "2")]),
            LLMToolResponse(content="完成"),
        ])
        tool = ProbeTool(ToolResult(success=True, output="内容"))
        ex = _executor(llm, tool)
        ex.execute_goal_loop(goal="g", system_prompt="s")
        assert len(tool.runs) == 1, "同批同参只读调用只该真跑一次"
        tool_msgs = _tool_messages(llm.calls[1])
        assert len(tool_msgs) == 2, "每个调用仍要有自己的结果消息（配对不能缺）"
        assert "复用首次结果" in tool_msgs[1]["content"]

    def test_different_args_still_run(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[_call("a", "1"), _call("b", "2")]),
            LLMToolResponse(content="完成"),
        ])
        tool = ProbeTool()
        ex = _executor(llm, tool)
        ex.execute_goal_loop(goal="g", system_prompt="s")
        assert len(tool.runs) == 2


class TestProgressSemantics:
    def test_success_without_new_info_does_not_extend(self):
        b = LoopBudget(base=3, extend=2, stall_limit=9, hard_cap=50)
        b.observe(TurnOutcome(succeeded=2))                     # 重复跑成功也不续期
        assert b.limit == 3 and b.extensions == 0

    def test_new_call_and_file_change_extend(self):
        b = LoopBudget(base=3, extend=2, stall_limit=9, hard_cap=50)
        b.observe(TurnOutcome(succeeded=1, new_calls=1))
        assert b.limit == 5
        b.observe(TurnOutcome(files_changed=1))
        assert b.limit == 7

    def test_repeat_success_is_stagnation_but_not_spinning(self):
        b = LoopBudget(base=9, extend=2, stall_limit=2, hard_cap=9)
        b.observe(TurnOutcome(succeeded=1, new_calls=1))
        b.observe(TurnOutcome(succeeded=1))                     # 无新信息
        b.observe(TurnOutcome(succeeded=1))
        assert b.stagnation_turns == 2
        assert b.spinning_turns == 0, "重读同一处不是'打转'，只是不续期"
        assert b.allow_next() is True

    def test_alternating_repeats_do_not_keep_extending(self):
        """A/B 交替每轮都成功——旧规则会一路续期到硬上限。"""
        b = LoopBudget(base=3, extend=2, stall_limit=9, hard_cap=50)
        b.observe(TurnOutcome(succeeded=1, new_calls=1))        # 首次新调用 → 续期一次
        assert b.limit == 5
        for _ in range(10):
            b.observe(TurnOutcome(succeeded=1))                 # 之后全是重复成功
        assert b.limit == 5, "重复轮不应再续期"
