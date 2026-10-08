"""回归测试：单循环路径的上下文压缩不能切出"孤立的 tool 消息"。
with 'tool_calls'"，无参数可赖、重试 3 次后整轮任务失败（长任务最容易触发）。"""
import pytest

from agent.executor import Executor


class FakeLLM:
    """只要被调用就返回固定摘要（避免联网）。"""

    def chat(self, messages, **kwargs):
        return "摘要"

    def chat_with_tools(self, messages, tools, **kwargs):
        raise AssertionError("压缩不应走 chat_with_tools")

    def compact_threshold_tokens(self):
        return 100


class FakeToolManager:
    def list_openai_schemas(self):
        return []


@pytest.fixture
def compactor(monkeypatch):
    """构造一个可按 keep 值触发压缩的 executor。"""
    from agent.executor import COMPACT_CONFIG

    def build(keep: int) -> Executor:
        monkeypatch.setitem(COMPACT_CONFIG, "keep_last", keep)
        monkeypatch.setitem(COMPACT_CONFIG, "token_threshold", 100)
        ex = Executor(llm=FakeLLM(), tool_manager=FakeToolManager())
        ex._summarize_old = lambda old: "旧历史摘要"
        return ex

    return build


def build_messages(n_turns: int, tools_per_turn: int) -> list:
    """构造形如单循环的真实序列：system + 每轮 assistant(tool_calls) + N×tool。"""
    msgs = [{"role": "system", "content": "系统提示"}]
    for t in range(n_turns):
        ids = []
        for i in range(tools_per_turn):
            ids.append({"id": f"call_{t}_{i}", "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"}})
        msgs.append({"role": "assistant", "content": "", "tool_calls": ids})
        for tc in ids:
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": "输出" * 100})
    return msgs


def assert_protocol_valid(messages: list) -> None:
    """任何 tool 消息之前必须存在带该 tool_call_id 的 assistant(tool_calls)。"""
    seen = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                seen.add(tc["id"])
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in seen, (
                f"孤立 tool 消息: {m.get('tool_call_id')} 之前没有配对的 "
                "assistant(tool_calls) —— 上游会 400"
            )


class TestCompactionBoundary:
    def test_every_cut_position_keeps_protocol_valid(self, compactor):
        """核心回归：遍历所有 keep 值，压缩后都不得出现孤立 tool 消息。"""
        for keep in range(2, 30):
            ex = compactor(keep)
            msgs = build_messages(n_turns=12, tools_per_turn=3)
            out = ex._maybe_compact(list(msgs))
            assert_protocol_valid(out)
            assert out[0] == msgs[0], "系统提示必须原样保留"

    def test_keeps_pairing_when_cut_lands_inside_tool_group(self, compactor):
        """切点落在 tool 组中间时，要把它的 assistant(tool_calls) 一起留下。"""
        ex = compactor(keep=5)
        msgs = build_messages(n_turns=6, tools_per_turn=4)
        out = ex._maybe_compact(list(msgs))
        assert out is not msgs, "应当发生了压缩（前置条件不成立会掩盖回归）"
        assert out[0]["role"] == "system"
        assert out[1]["content"].startswith("## 之前的执行摘要"), "摘要应紧跟系统提示"
        assert out[2]["role"] != "tool", "保留区不能以孤立 tool 开头"
        assert_protocol_valid(out)

    def test_protocol_valid_for_many_shapes(self, compactor):
        """不同工具数/轮数组合都不能切坏。"""
        for tools_per_turn in (1, 2, 5, 8):
            for keep in (3, 7, 11, 20):
                ex = compactor(keep)
                msgs = build_messages(n_turns=10, tools_per_turn=tools_per_turn)
                assert_protocol_valid(ex._maybe_compact(list(msgs)))

    def test_no_orphan_prepended_reference_is_preserved(self, compactor):
        """保留区补进来的 assistant 必须真的带着对应 tool_calls。"""
        ex = compactor(keep=4)
        msgs = build_messages(n_turns=8, tools_per_turn=3)
        out = ex._maybe_compact(list(msgs))
        tail = out[2:]                      # out = [原 system, 摘要] + 保留区
        if tail and tail[0]["role"] == "assistant":
            assert tail[0].get("tool_calls"), "补进来的 assistant 必须带 tool_calls"


class TestTokenEstimate:
    def test_tool_call_arguments_counted(self):
        """写文件类调用把内容放在 arguments 里，只数 content 会严重低估。"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "file", "arguments": "x" * 30000}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        est = Executor._estimate_tokens(msgs)
        # 30000 个 ASCII 字符 ≈ 7500 token；只数 content 的话这里≈0
        assert est > 5000, (
            f"tool_calls 的 arguments 未计入估算（得到 {est}），"
            "会导致压缩触发过晚、上下文先撑爆上游窗口"
        )

    def test_cjk_counted_near_one_token_per_char(self):
        """中文一字≈1 token，不能被"字符数/3"低估。"""
        assert Executor._estimate_tokens([{"role": "user", "content": "你好世界"}]) == 4
        assert Executor._estimate_tokens([{"role": "user", "content": "中" * 100}]) >= 90

    def test_ascii_uses_quarter_ratio(self):
        """英文按 ≈4 字符/token 估算。"""
        assert Executor._estimate_tokens([{"role": "user", "content": "x" * 400}]) == 100

    def test_multimodal_text_parts_counted(self):
        """多模态 content 数组里的 text 部分也要计入。"""
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "中" * 50},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]}]
        assert Executor._estimate_tokens(msgs) >= 45
