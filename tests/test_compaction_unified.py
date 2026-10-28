"""压缩只有一套实现：所有执行路径都必须走 Compactor。

历史上主循环用 Compactor、plan/FC 路径用 Rollout 自带的旧实现，
于是"零驻留指针"只覆盖一半，修 bug 得分两处改。这里锁住"单实现"。
"""
from agent.approval import ApprovalPolicy
from agent.compactor import Compactor
from agent.executor import Executor
from agent.rollout import Rollout
from models.llm import LLMToolResponse
from tools.tool_manager import ToolManager


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)

    def chat_with_tools(self, messages, tools, **kwargs):
        return self.script.pop(0) if self.script else LLMToolResponse(content="完成")

    def chat(self, messages, **kwargs):
        return ""


def _executor(llm):
    return Executor(tool_manager=ToolManager(), llm=llm,
                    approval_policy=ApprovalPolicy(mode="never",
                                                   sandbox_mode="workspace-write",
                                                   interactive=False),
                    rollout=None, max_step_ops=2)


def test_fc_path_goes_through_unified_compactor(monkeypatch):
    """plan/FC 路径必须调 _maybe_compact（= Compactor），而不是绕过它。"""
    ex = _executor(FakeLLM([LLMToolResponse(content="直接答完")]))
    calls = []
    real = ex._maybe_compact
    monkeypatch.setattr(ex, "_maybe_compact",
                        lambda messages: calls.append(len(messages)) or real(messages))
    ex._execute_step_fc(goal="测试目标", current_step="做一件事", history_summary="")
    assert calls, "FC 路径没有经过统一压缩入口"


def test_rollout_maybe_compact_delegates(monkeypatch):
    seen = []
    monkeypatch.setattr(Compactor, "maybe_compact",
                        lambda self, messages, cfg=None, **kw: seen.append(cfg) or messages)
    r = Rollout(run_id="t-delegate", enabled=False, summarizer=lambda m: "摘要")
    msgs = [{"role": "user", "content": "长" * 3000} for _ in range(20)]
    out = r.maybe_compact(msgs, "goal", max_tokens=10, keep_recent=4)

    assert seen, "Rollout.maybe_compact 应转交 Compactor"
    assert seen[0]["token_threshold"] == 10 and seen[0]["keep_last"] == 4
    assert out == msgs and out is not msgs, "未压缩时仍应返回副本（旧契约）"


def test_compactor_skips_when_summary_empty():
    """没有摘要器时不能把历史换成一条空摘要（等于白丢上下文）。"""
    comp = Compactor(llm=None, summarizer=None)
    msgs = [{"role": "system", "content": "规则"}] + [
        {"role": "user", "content": "长" * 500} for _ in range(30)
    ]
    out = comp.maybe_compact(msgs, {"keep_last": 4, "token_threshold": 10},
                             mode=lambda: "summary")
    assert out == msgs
