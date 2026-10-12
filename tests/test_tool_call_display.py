"""工具调用的显示顺序与配对。

背景：批次曾把整批 header 一次性预打印，导致"⏺ 命令行"与它下面的输出错位
（同批两条同名命令时尤其明显）。这里锁两条不变量：
  ① 串行批次：每条 header 必须夹在"前一条执行完、本条执行前"之间；
  ② tool_call / tool_output / tool_result 三处的 call_id 一致（前端据此精确配对）。
"""
from agent import Agent, AgentConfig
from agent.memory import Memory
from models.llm import LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager


class FakeLLM:
    """脚本化 LLM：回放预设响应，记录每次收到的消息。"""

    def __init__(self, script):
        self.script = list(script)
        self.tools_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append([dict(m) for m in messages])
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, messages, **kwargs):
        return ""


def _run(tmp_path, script, **overrides):
    """跑一次 loop，返回 (时间线, llm)。

    时间线元素：("call", call_id) / ("result", call_id) / ("exec", call_id)。
    exec 由包在 _execute_one_tool_call 外的探针插入，代表"这条真的开始执行了"。
    """
    timeline = []
    cfg = AgentConfig(
        exec_mode="loop", verbose=False, rollout_enabled=False,
        guardian_enabled=False, approval_policy="never",
        sandbox_mode="workspace-write", approval_interactive=False,
        instructions_enabled=False, enable_vision=False,
        enable_frame_compare=False, enable_anomaly_detect=False,
        snapshot_enabled=False, checkpoint_per_tool=False, repomap_enabled=False,
        **overrides,
    )
    llm = FakeLLM(script)
    agent = Agent(llm=llm, tool_manager=ToolManager(),
                  memory=Memory(db_path=str(tmp_path)), config=cfg)

    original = agent.executor._execute_one_tool_call

    def probe(tc, goal, *args, **kwargs):
        timeline.append(("exec", str(tc.id)))
        return original(tc, goal, *args, **kwargs)

    agent.executor._execute_one_tool_call = probe

    def sink(event_type, data):
        if event_type in ("tool_call", "tool_result"):
            timeline.append((event_type.split("_")[1], str(data.get("call_id") or "")))

    agent.run("列目录", event_sink=sink)
    return timeline, llm


def _serial_script():
    """两条命令，第二条带 `2>nul`（重定向 = medium 风险）→ 整批回退串行。

    只用 `&` 的复合命令仍算 low（会走并行），必须带重定向才与真实场景一致。
    """
    return [
        LLMToolResponse(content="", tool_calls=[
            ToolCall("c1", "terminal", {"command": "dir /b"}),
            ToolCall("c2", "terminal", {"command": "dir /b docs 2>nul & echo ok"}),
        ]),
        LLMToolResponse(content="完成。"),
    ]


class TestSerialBatch:
    def test_headers_stay_next_to_their_own_execution(self, tmp_path):
        """串行批次：第二条 header 必须晚于第一条的执行（不能成对提前打印）。"""
        timeline, _ = _run(tmp_path, _serial_script())
        seq = [t for t in timeline if t[0] in ("call", "exec")]
        assert seq == [("call", "c1"), ("exec", "c1"),
                       ("call", "c2"), ("exec", "c2")], f"header 未紧贴自己的执行: {seq}"


class TestCallIdPairing:
    def test_call_id_matches_across_call_and_result(self, tmp_path):
        """tool_call 与 tool_result 必须带同一个 call_id。"""
        timeline, _ = _run(tmp_path, _serial_script())
        calls = {t[1] for t in timeline if t[0] == "call"}
        results = {t[1] for t in timeline if t[0] == "result"}
        assert calls == results == {"c1", "c2"}, f"call={calls} result={results}"

    def test_missing_model_id_gets_synthesized(self, tmp_path):
        """模型不给 tool_call.id 时补 call_{n}，两侧仍一致。"""
        timeline, _ = _run(tmp_path, [
            LLMToolResponse(content="", tool_calls=[
                ToolCall("", "terminal", {"command": "dir /b"}),
            ]),
            LLMToolResponse(content="完成。"),
        ])
        calls = [t[1] for t in timeline if t[0] == "call"]
        results = [t[1] for t in timeline if t[0] == "result"]
        assert calls and calls == results and calls[0].startswith("call_"), (
            f"call={calls} result={results}")


class TestParallelBatch:
    def test_parallel_keeps_preprint_and_pairs_by_id(self, tmp_path):
        """并行批：两条低风险命令 → header 仍在派发前成对打印（有意为之，避免执行期无反馈），
        配对改由 call_id 承担，因此顺序不再是问题。"""
        timeline, _ = _run(tmp_path, [
            LLMToolResponse(content="", tool_calls=[
                ToolCall("p1", "terminal", {"command": "dir /b docs"}),
                ToolCall("p2", "terminal", {"command": "dir /b tests"}),
            ]),
            LLMToolResponse(content="完成。"),
        ])
        seq = [t for t in timeline if t[0] in ("call", "exec")]
        kinds = [t[0] for t in seq]
        assert kinds.index("exec") == 2, f"并行批应在执行前成对打印 header: {seq}"
        assert {t[1] for t in timeline if t[0] == "call"} == {"p1", "p2"}
        assert {t[1] for t in timeline if t[0] == "result"} == {"p1", "p2"}


class TestUnparseableCall:
    def test_broken_arguments_still_get_a_header(self, tmp_path):
        """参数解析失败（未执行）的条目也要有 header，否则界面上凭空冒出结果块。"""
        timeline, _ = _run(tmp_path, [
            LLMToolResponse(content="", tool_calls=[
                ToolCall("b1", "file", {"_raw": "{坏 JSON"}),
            ]),
            LLMToolResponse(content="完成。"),
        ])
        assert ("call", "b1") in timeline, f"缺 header: {timeline}"
        assert ("result", "b1") in timeline, f"缺结果: {timeline}"


class TestModelFeedbackUnchanged:
    def test_tool_results_feed_back_in_model_order(self, tmp_path):
        """回归：显示顺序的改动不得影响回喂给模型的顺序（仍按模型给出的顺序）。"""
        _, llm = _run(tmp_path, _serial_script())
        assert len(llm.tools_calls) >= 2, "应有第二轮调用"
        second = llm.tools_calls[1]
        tids = [m.get("tool_call_id") for m in second if m.get("role") == "tool"]
        assert tids == ["c1", "c2"], f"回喂顺序被改动: {tids}"
