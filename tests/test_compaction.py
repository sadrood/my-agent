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
    assert out[0]["content"].startswith("## 之前的执行摘要")
    assert "摘要内容" in out[0]["content"]
    assert len(out) == 4   # 1 摘要 + 保留最近 3 条


def test_no_compact_under_threshold():
    llm = FakeLLM()
    ex = _make(llm, threshold=10000, keep=3)
    messages = [{"role": "user", "content": "短"}] * 4
    out = ex._maybe_compact(list(messages))
    assert llm.summarize_calls == 0
    assert out == messages


def test_estimate_tokens():
    """估算改为 CJK 感知（英文≈4 字符/token）。"""
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
    （执行器系统提示：一次只调一个工具、参数必须来自 schema、收尾用自然语言…）"""

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


class TestExecutorSystemHeadPreserved:
    """执行器压缩同样必须原样保留开头的 system 消息（对齐 rollout 实现）。"""

    HEAD = "【执行器规则】一次只调一个工具；项目指令：见 AGENTS.md"

    def _messages(self, n_pairs=6):
        msgs = [{"role": "system", "content": self.HEAD}]
        msgs.append({"role": "user", "content": "目标"})
        for i in range(n_pairs):
            msgs.append({"role": "assistant", "content": f"a{i}"})
            msgs.append({"role": "tool", "content": f"t{i}"})
        return msgs

    def _ex(self, summary="摘要内容"):
        return _make(FakeLLM(summary=summary), threshold=10, keep=3)

    def test_head_system_kept_byte_identical(self):
        ex = self._ex()
        msgs = self._messages()
        out = ex._maybe_compact(list(msgs))
        assert len(out) < len(msgs), "没有发生压缩，用例失去意义"
        assert out[0] == msgs[0], "系统提示必须逐字节原样保留"
        assert out[0]["content"] == self.HEAD

    def test_summary_sits_right_after_head(self):
        ex = self._ex()
        out = ex._maybe_compact(self._messages())
        assert out[1]["role"] == "system"
        assert out[1]["content"].startswith("## 之前的执行摘要")
        assert "摘要内容" in out[1]["content"]
        assert out[1]["content"] != self.HEAD

    def test_no_system_head_behaves_as_before(self):
        ex = self._ex()
        msgs = [{"role": "user", "content": "x" * 200}] * 6
        out = ex._maybe_compact(list(msgs))
        assert out[0]["role"] == "system"
        assert out[0]["content"].startswith("## 之前的执行摘要")
        assert len(out) == 4

    def test_all_system_messages_untouched(self):
        llm = FakeLLM()
        ex = _make(llm, threshold=10, keep=3)
        msgs = [{"role": "system", "content": "x" * 200}] * 6
        out = ex._maybe_compact(list(msgs))
        assert out == msgs and llm.summarize_calls == 0

    def test_second_compaction_does_not_resummarize_head(self):
        """摘要驻留头部：再压一次不会把上一条摘要再摘要（本次接受的权衡）。"""
        llm = FakeLLM()
        ex = _make(llm, threshold=10, keep=3)
        msgs = self._messages()
        once = ex._maybe_compact(list(msgs))
        twice = ex._maybe_compact(list(once))
        assert twice == once, "头部摘要不该被二次压缩"
        assert llm.summarize_calls == 1
        assert twice[0]["content"] == self.HEAD

    def test_real_executor_prompt_shape_survives(self):
        """按主循环的真实构造方式拼系统提示（基础 + 审批提示 + AGENTS 项目指令）。"""
        from agent.executor import EXECUTOR_FC_SYSTEM_PROMPT, APPROVAL_NOTICE_TEMPLATE
        prompt = EXECUTOR_FC_SYSTEM_PROMPT + APPROVAL_NOTICE_TEMPLATE.format(
            approval_policy="never", sandbox_mode="workspace-write")
        prompt += "\n\n## 项目指令（必须遵守）\n规则 12：公共文件禁止整文件 git add。"
        ex = self._ex()
        msgs = self._messages(8)
        msgs[0] = {"role": "system", "content": prompt}
        out = ex._maybe_compact(list(msgs))
        assert len(out) < len(msgs), "没有发生压缩，用例失去意义"
        assert out[0]["content"] == prompt
        assert "## 项目指令（必须遵守）" in out[0]["content"]
