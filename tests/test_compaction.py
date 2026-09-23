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
    """估算改为 CJK 感知（英文≈4 字符/token）。

    旧断言 `"a"*30 == 10` 编码的是"字符数/3"的老公式；该公式把中文低估 3~4 倍
    （实测「你好世界」→1 vs 实际 4），会让压缩迟迟不触发、最终撞上游窗口 400。
    现复用 agent/rollout.py 的估算：英文按 4 字符/token、中文按 1 字符/token。
    """
    ex = _make(FakeLLM())
    assert ex._estimate_tokens([{"role": "user", "content": "a" * 40}]) == 10
    assert ex._estimate_tokens([{"role": "user", "content": "中" * 10}]) == 10


def test_summarize_failure_is_safe():
    class BoomLLM(FakeLLM):
        def chat(self, messages, **kwargs):
            raise RuntimeError("上游挂了")

    ex = _make(BoomLLM(), threshold=10, keep=3)
    messages = [{"role": "user", "content": "x" * 50}] * 5
    out = ex._maybe_compact(list(messages))
    assert out == messages   # 失败安全：原样返回


class TestSystemPromptSurvivesCompaction:
    """压缩必须保留开头的 system 消息（执行器规则）。

    实测故障（2026-09-22 审计）：`old_part = messages[:-keep]` 把 `messages[0]`
    （执行器系统提示：一次只调一个工具、参数必须来自 schema、收尾用自然语言…）
    一起压成摘要，压缩后第 0 条变成「## 之前的执行摘要」—— 之后所有 LLM 调用都不再
    带执行器规则，而 `executor.py` 的单循环路径正是每步都用压缩后的 messages。
    """

    def _rollout(self):
        from agent.rollout import Rollout
        r = Rollout.__new__(Rollout)
        r.config = {"keep_messages": 4}
        r.summarizer = lambda *a, **k: "摘要内容"
        r.emit = lambda *a, **k: None
        return r

    def _messages(self, n_pairs=6):
        msgs = [{"role": "system", "content": "【执行器规则】一次只调一个工具"}]
        msgs.append({"role": "user", "content": "目标"})
        for i in range(n_pairs):
            msgs.append({"role": "assistant", "content": f"a{i}"})
            msgs.append({"role": "tool", "content": f"t{i}"})
        return msgs

    def test_system_prompt_kept(self):
        r = self._rollout()
        msgs = self._messages()
        out = r.maybe_compact(msgs, "目标", max_tokens=1)
        assert len(out) < len(msgs), "没有发生压缩，用例失去意义"
        assert out[0]["role"] == "system"
        assert "执行器规则" in str(out[0].get("content", "")), \
            "压缩后执行器系统提示被摘要顶掉了"

    def test_summary_still_present(self):
        r = self._rollout()
        out = r.maybe_compact(self._messages(), "目标", max_tokens=1)
        assert any("之前的执行摘要" in str(m.get("content", "")) for m in out), \
            "摘要没了，压缩就白做了"

    def test_short_conversation_untouched(self):
        r = self._rollout()
        msgs = self._messages(n_pairs=1)
        assert r.maybe_compact(msgs, "目标", max_tokens=1) == msgs
