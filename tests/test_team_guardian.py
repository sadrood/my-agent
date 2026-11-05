"""团队 Worker 的 Guardian 审校：高风险调用不能再绕过盲审。"""
from agent.guardian import GuardianVerdict
from agent.roles import AgentRole
from agent.team import WorkerAgent
from tools.base import ToolResult


class _FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, **kwargs):
        return self.replies.pop(0)


class _BlockGuardian:
    def should_review(self, risk_level):
        return True

    def review(self, request, goal):
        return GuardianVerdict(verdict="block", reason="测试拦截", used=True)


class _Manager:
    def __init__(self):
        self.called = []

    def build_approval_request(self, tool_name, args):
        from tools.base import ApprovalRequest
        return ApprovalRequest(tool_name=tool_name, arguments=args, command="x", risk_level="high")

    def is_parallel_safe(self, name, args):
        return True

    def execute(self, name, input_str):
        self.called.append(name)
        return ToolResult(success=True, output="ran")


def test_guardian_blocks_tool_before_execution():
    role = AgentRole(name="test", description="", system_prompt="", tools=["terminal"], temperature=0)
    llm = _FakeLLM(['{"tool": "terminal", "tool_input": "echo hi"}', "done"])
    manager = _Manager()
    worker = WorkerAgent("w", role, llm, manager, approval=None, guardian=_BlockGuardian())
    out = worker.execute("测试任务")
    assert "Guardian 拦截" in out
    assert not manager.called, "Guardian 拒绝后不能再执行工具"
