"""
Rollout 事件追踪与压缩测试。
"""
import os

from agent.rollout import Rollout, clip_result, clip_text, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2          # 中文 1 字 1 token
    assert estimate_tokens("abcd") == 1          # 英文 4 字符 1 token


def test_emit_and_transcript(tmp_path):
    r = Rollout(run_id="t1", config={"enabled": True, "dir": str(tmp_path)})
    r.emit("tool_call", {"tool": "terminal"})
    r.emit("tool_result", {"success": True})
    r.close()

    assert len(r.events) == 2
    assert "terminal" in r.get_transcript()
    assert os.path.exists(r.log_path)

    # JSONL 落盘可读
    with open(r.log_path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 2
    for line in lines:
        assert '"event"' in line


def test_disabled(tmp_path):
    r = Rollout(run_id="t2", enabled=False, config={"dir": str(tmp_path)})
    r.emit("x", {})
    assert len(r.events) == 0
    assert r.log_path == ""


def test_maybe_compact_no_trigger():
    r = Rollout(run_id="t3", enabled=False)
    messages = [{"role": "user", "content": "短消息"}]
    out = r.maybe_compact(messages, "goal", max_tokens=100000)
    assert out == messages


def test_maybe_compact_triggers(tmp_path):
    fake_summaries = []

    def summarizer(msgs):
        fake_summaries.append(msgs)
        return "压缩摘要"

    r = Rollout(run_id="t4", enabled=False, summarizer=summarizer)
    messages = [{"role": "user", "content": "长" * 3000} for _ in range(20)]
    out = r.maybe_compact(messages, "goal", max_tokens=1000, keep_recent=4)

    assert len(fake_summaries) == 1
    assert len(out) < len(messages)
    assert out[0]["role"] == "system"
    assert "压缩摘要" in out[0]["content"]
    assert len(out) == 5  # 1 条摘要 + 4 条最近消息


def test_maybe_compact_without_summarizer_returns_copy():
    r = Rollout(run_id="t5", enabled=False)
    messages = [{"role": "user", "content": "长" * 3000} for _ in range(20)]
    out = r.maybe_compact(messages, "goal", max_tokens=10, keep_recent=2)
    assert out == messages          # 无摘要器 → 原样返回（副本）
    assert out is not messages


def _tool_round(round_idx, n_calls=2):
    """构造一个完整工具闭环：assistant(tool_calls) + N 条 tool 响应。"""
    msgs = [{
        "role": "assistant",
        "content": f"第{round_idx}轮" + "长" * 500,
        "tool_calls": [
            {"id": f"call_{round_idx}_{i}", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}}
            for i in range(n_calls)
        ],
    }]
    msgs += [
        {"role": "tool", "tool_call_id": f"call_{round_idx}_{i}", "content": f"结果{round_idx}_{i}" + "长" * 500}
        for i in range(n_calls)
    ]
    return msgs


def test_compact_keeps_tool_round_integrity(tmp_path):
    """回归：截断线切在 tool 闭环中间时，compaction 后不能出现孤立的
    role=tool 消息（其 tool_calls 被截掉）——其它模型等严格校验会 400。"""
    def summarizer(msgs):
        return "摘要"

    r = Rollout(run_id="t6", enabled=False, summarizer=summarizer)
    # 4 个完整闭环（各 3 条，共 12 条）；keep=5 → 保留尾部 5 条，
    # 截断线落在第 3 个闭环的 tool 响应中间
    messages = []
    for i in range(4):
        messages.extend(_tool_round(i))
    out = r.maybe_compact(messages, "goal", max_tokens=10, keep_recent=5)

    roles = [m["role"] for m in out]
    assert roles[0] == "system"          # 摘要
    # 校验协议对仗：从每个 tool 消息向前找最近的 assistant，
    # 它必须带 tool_calls，且 tool_calls 数 ≥ 从它开始的连续 tool 消息数
    _check_tool_protocol(out[1:])


def _check_tool_protocol(msgs):
    """校验消息列表协议：每个 tool 消息都能回溯到带 tool_calls 的 assistant。"""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "tool":
            # 向前找最近的 assistant
            j = i - 1
            while j >= 0 and msgs[j]["role"] == "tool":
                j -= 1
            assert j >= 0, f"孤立 tool 消息在索引 {i}"
            assert msgs[j]["role"] == "assistant" and msgs[j].get("tool_calls"), \
                f"tool 消息前无 assistant(tool_calls)：索引 {i}"
            n_calls = len(msgs[j]["tool_calls"])
            n_tools = i - j   # 从 assistant 后的连续 tool 消息数
            assert n_tools <= n_calls, f"tool 响应数({n_tools})超过 tool_calls 数({n_calls})"
        i += 1


def test_compact_drops_leading_orphan_tool(tmp_path):
    """截断线恰好把保留区开头切成 tool 消息时，向前补齐 assistant(tool_calls)
    或裁掉，保证协议对仗。"""
    def summarizer(msgs):
        return "摘要"

    r = Rollout(run_id="t7", enabled=False, summarizer=summarizer)
    messages = []
    for i in range(4):
        messages.extend(_tool_round(i, n_calls=1))
    # 保留 3 条 → 尾部 = 最后闭环的 tool + 中间闭环的 assistant/tool
    out = r.maybe_compact(messages, "goal", max_tokens=10, keep_recent=3)

    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    # 要么补回了 assistant(tool_calls)，要么把孤立 tool 裁掉了
    _check_tool_protocol(out[1:])


class TestClipLimits:
    """落盘截断：追踪文件是事后诊断的唯一依据，截太狠等于没有记录。

    实测事故（2026-09-18）：agent 的最终自审报告在 rollout 里被截到 300 字、断在
    半句，复盘"它提了哪些问题"只能去 memory/sessions/*.json 里翻。现在上限可配，
    且截断时附原文长度——避免把"被截断"误读成"就这么多"。
    """

    def test_short_text_passes_through_untouched(self):
        assert clip_text("你好，世界") == "你好，世界"
        assert clip_result("短结果") == "短结果"

    def test_none_becomes_empty_string(self):
        assert clip_text(None) == ""
        assert clip_result(None) == ""

    def test_long_text_is_cut_with_a_visible_note(self):
        out = clip_text("x" * 9000)
        assert out.startswith("x" * 100)
        assert "落盘截断" in out and "9000" in out, "要能看出被截断以及原文多长"
        assert len(out) < 9000

    def test_model_output_limit_is_much_larger_than_the_old_300(self):
        """回归：默认不能再把最终回答截到 300 字。"""
        assert len(clip_text("y" * 3000)) > 300, "3000 字的回答不该被截"
        assert "落盘截断" not in clip_text("y" * 3000)

    def test_result_limit_is_separate_and_smaller(self):
        # 工具返回默认 2000：3000 字要截，但截出来的比模型输出上限短
        assert "落盘截断" in clip_result("y" * 3000)
        assert len(clip_result("y" * 3000)) < len(clip_text("y" * 3000))

    def test_explicit_limit_overrides_config(self):
        assert clip_text("z" * 500, limit=100).startswith("z" * 100)
        assert "落盘截断" in clip_text("z" * 500, limit=100)

    def test_zero_limit_means_no_truncation(self):
        big = "q" * 20000
        assert clip_text(big, limit=0) == big
