"""
上下文压缩（compaction）测试：超阈值压缩旧历史、未超阈值不压缩、摘要失败安全。
"""
from agent.executor import Executor
from agent.approval import ApprovalPolicy
from tools.tool_manager import ToolManager


class FakeLLM:
    def __init__(self, summary="（历史已压缩）"):
        self.summary = summary
        self.summarize_calls = 0

    def chat(self, messages, **kwargs):
        self.summarize_calls += 1
        return self.summary

    def chat_with_tools(self, *a, **k):
        return None


def _make(llm, threshold=100, keep=3):
    import agent.executor as ex
    ex.COMPACT_CONFIG = {"enabled": True, "token_threshold": threshold, "keep_last": keep}
    return Executor(
        tool_manager=ToolManager(),
        llm=llm,
        approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        guardian=None, rollout=None, instructions_text="", max_step_ops=10, llm_retry_delay=0,
    )


def test_compact_over_threshold():
    llm = FakeLLM(summary="摘要内容")
    ex = _make(llm, threshold=100, keep=3)
    messages = [{"role": "user", "content": "x" * 200}] * 6   # 字符数超阈值
    out = ex._maybe_compact(list(messages))
    assert llm.summarize_calls == 1
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "摘要内容"
    assert len(out) == 4   # 1 摘要 + 保留最近 3 条


def test_no_compact_under_threshold():
    llm = FakeLLM()
    ex = _make(llm, threshold=10000, keep=3)
    messages = [{"role": "user", "content": "短"}] * 4
    out = ex._maybe_compact(list(messages))
    assert llm.summarize_calls == 0
    assert out == messages


def test_estimate_tokens():
    ex = _make(FakeLLM())
    assert ex._estimate_tokens([{"role": "user", "content": "a" * 30}]) == 10


def test_summarize_failure_is_safe():
    class BoomLLM(FakeLLM):
        def chat(self, messages, **kwargs):
            raise RuntimeError("上游挂了")

    ex = _make(BoomLLM(), threshold=10, keep=3)
    messages = [{"role": "user", "content": "x" * 50}] * 5
    out = ex._maybe_compact(list(messages))
    assert out == messages   # 失败安全：原样返回
