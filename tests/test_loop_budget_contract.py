"""`main` 上的 LoopBudget 没有 `stagnation_alarm` —— 执行循环必须能照跑。
executor 侧已改成 `getattr` 兜底；本测试把"这个兜底不能被人顺手'清理'掉"钉住。"""
from agent.approval import ApprovalPolicy
from agent.executor import Executor
from models.llm import LLMToolResponse
from tools.tool_manager import ToolManager


class _ScriptedLLM:
    def __init__(self, replies):
        self._replies = list(replies)

    def chat_with_tools(self, messages, tools, **kw):
        return self._replies.pop(0) if self._replies else LLMToolResponse(content="结束")

    def chat(self, messages, **kw):
        return "结束"


class TestBudgetWithoutStagnationAlarm:
    def test_loop_survives_missing_method(self, monkeypatch):
        """没有该方法时，循环要正常收尾，而不是 AttributeError。"""
        import agent.loop_budget as lb

        monkeypatch.delattr(lb.LoopBudget, "stagnation_alarm", raising=False)
        assert not hasattr(lb.LoopBudget.fixed(1), "stagnation_alarm"), \
            "模拟失败：方法还在"

        ex = Executor(
            llm=_ScriptedLLM([LLMToolResponse(content="做完了")]),
            tool_manager=ToolManager(),
            approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                           interactive=False),
            guardian=None, rollout=None, instructions_text="",
            max_step_ops=5, llm_retry_delay=0,
        )
        result = ex.execute_goal_loop(goal="简单目标", system_prompt="测试",
                                      stream=False, max_ops=3)
        assert isinstance(result, dict), "循环没能正常返回"
        assert "output" in result

    def test_guard_still_calls_it_when_present(self, monkeypatch):
        """方法存在时必须**真的调用**（别把兜底写成永远跳过）。"""
        import agent.loop_budget as lb

        called = []
        real = lb.LoopBudget.stagnation_alarm

        def _spy(self):
            called.append(1)
            return real(self)

        monkeypatch.setattr(lb.LoopBudget, "stagnation_alarm", _spy)
        ex = Executor(
            llm=_ScriptedLLM([LLMToolResponse(content="做完了")]),
            tool_manager=ToolManager(),
            approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                           interactive=False),
            guardian=None, rollout=None, instructions_text="",
            max_step_ops=5, llm_retry_delay=0,
        )
        ex.execute_goal_loop(goal="简单目标", system_prompt="测试",
                             stream=False, max_ops=3)
        assert called, "方法在，但兜底把它跳过了"
