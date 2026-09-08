"""
Executor v3 function calling 主循环测试（FakeLLM 脚本化，无网络）。
"""
import json

from agent.executor import Executor
from agent.approval import ApprovalPolicy
from models.llm import LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager


class FakeLLM:
    """脚本化假 LLM：按顺序弹出响应；异常对象直接抛出。"""

    def __init__(self, script):
        self.script = list(script)
        self.chat_calls = []
        self.tools_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append(messages)
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        if not self.script:
            return ""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_executor(llm, approval=None, guardian=None):
    return Executor(
        tool_manager=ToolManager(),
        llm=llm,
        approval_policy=approval,
        guardian=guardian,
        rollout=None,
        instructions_text="",
        max_step_ops=10,
    )


def _basic_args():
    return dict(goal="测试目标", current_step="执行一个测试步骤",
                history_summary="", step_context="", vision_feedback="",
                failure_warnings="")


class TestFunctionCalling:
    def test_think_then_terminal_then_finish(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "think", {"thought": "先想"})]),
            LLMToolResponse(content="", tool_calls=[ToolCall("2", "terminal", {"command": "echo hi"})]),
            LLMToolResponse(content="步骤完成：成功输出 hi"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_step(**_basic_args())

        assert result["status"] == "completed"
        assert result["success"] is True
        assert result["tools_used"] == ["think", "terminal"]
        assert "步骤完成" in result["output"]
        # 3 轮模型调用
        assert len(llm.tools_calls) == 3

    def test_approval_denial_is_fed_back(self):
        # read-only 沙箱下 terminal 被拒绝；模型随后用文字收尾
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "terminal", {"command": "echo hi"})]),
            LLMToolResponse(content="终端被拒绝，改用文字说明。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="read-only", interactive=False),
        )
        result = ex.execute_step(**_basic_args())

        assert result["status"] == "completed"
        call = result["tool_calls"][0]
        assert call["success"] is False
        assert call["blocked_reason"]
        assert "拒绝" in call["blocked_reason"] or "沙箱" in call["blocked_reason"]

    def test_guardian_block(self):
        class BlockGuardian:
            def __init__(self):
                self.review_count = 0
                self.block_count = 0

            def should_review(self, risk):
                return True

            def review(self, request, goal):
                self.review_count += 1
                from agent.guardian import GuardianVerdict
                self.block_count += 1
                return GuardianVerdict(verdict="block", reason="测试拦截", used=True)

        guardian = BlockGuardian()
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "terminal", {"command": "echo hi"})]),
            LLMToolResponse(content="被拦截后改用文字完成。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
            guardian=guardian,
        )
        result = ex.execute_step(**_basic_args())

        assert guardian.review_count == 1
        assert "Guardian 拦截" in result["tool_calls"][0]["blocked_reason"]

    def test_failed_tool_leads_to_failed_step(self):
        # 工具连续失败且模型没有给出文字收尾 → 循环耗尽 → failed
        failing_calls = [
            LLMToolResponse(content="", tool_calls=[ToolCall(str(i), "terminal", {"command": "exit 1"})])
            for i in range(12)   # 超过 max_step_ops(10)
        ]
        llm = FakeLLM(failing_calls)
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_step(**_basic_args())
        assert result["status"] == "failed"
        assert result["success"] is False

    def test_legacy_fallback_when_tools_unsupported(self):
        llm = FakeLLM([
            NotImplementedError("tools not supported by provider"),
            json.dumps({"action": "use_tool", "tool": "python", "tool_input": "print(42)"}),
        ])
        ex = _make_executor(llm)
        result = ex.execute_step(**_basic_args())

        assert result["status"] == "completed"
        assert result["tool"] == "python"
        assert "42" in result["output"]
        assert ex._fc_supported is False

    def test_legacy_finish_action(self):
        llm = FakeLLM([
            NotImplementedError("tools not supported"),
            json.dumps({"action": "finish", "result": "全部完成"}),
        ])
        ex = _make_executor(llm)
        result = ex.execute_step(**_basic_args())
        assert result["status"] == "completed"
        assert result["output"] == "全部完成"


class TestThinkToolSchema:
    def test_schema_shape(self):
        from agent.executor import THINK_TOOL_SCHEMA
        fn = THINK_TOOL_SCHEMA["function"]
        assert fn["name"] == "think"
        assert fn["parameters"]["required"] == ["thought"]
