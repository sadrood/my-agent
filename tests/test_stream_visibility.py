"""实时输出（terminal 逐行 / delegate 子 agent 片段）与事件渲染：CLI 与面板都要看得见。"""


from agent.executor import STREAM_TOOLS, Executor, stream_label
from tools.base import BaseTool, ToolResult


class _StreamTool(BaseTool):
    """假流式工具：执行时把几段输出推给回调（模拟 terminal 的逐行 / delegate 的片段）。"""

    name = "terminal"
    description = "假终端"
    risk_level = "low"
    min_sandbox_mode = "read-only"
    chunks = ["第一行\n", "第二行\n"]

    def execute(self, input_str: str) -> ToolResult:
        cb = getattr(self, "_output_callback", None)
        for chunk in self.chunks:
            if cb:
                cb(chunk)
        return ToolResult(success=True, output="".join(self.chunks))

    def schema(self):
        return {"type": "object", "properties": {}}


class _DelegateTool(_StreamTool):
    name = "delegate"
    description = "假子 agent"
    min_sandbox_mode = "workspace-write"


def _executor(sink):
    from models.llm import LLM
    ex = Executor(tool_manager=None, llm=LLM(), approval_policy=None, guardian=None,
                  rollout=None, instructions_text="", max_step_ops=5)
    ex._event_sink = sink
    return ex


class TestStreamPreviewCap:
    """实时预览限流：`dir /b` 这类命令不该把上百行刷满控制台。"""

    def test_forwards_only_the_head(self):
        from tools.base import StreamPreview
        p = StreamPreview(3)
        assert [p.allow() for _ in range(5)] == [True, True, True, False, False]
        assert (p.shown, p.skipped) == (3, 5 - 3)

    def test_notice_is_emitted_once(self):
        from tools.base import StreamPreview
        p = StreamPreview(2)
        for _ in range(4):
            p.allow()
        note = p.notice()
        assert "前 2 行" in note and "2 行未实时显示" in note
        assert p.notice() == "", "两个 reader 线程只能补一次说明"

    def test_no_folding_means_no_notice(self):
        from tools.base import StreamPreview
        p = StreamPreview(10)
        for _ in range(3):
            p.allow()
        assert p.notice() == ""

    def test_negative_means_unlimited(self):
        from tools.base import StreamPreview
        p = StreamPreview(-1)
        assert all(p.allow() for _ in range(50))
        assert p.notice() == ""

    def test_limit_comes_from_config(self, monkeypatch):
        from config import TOOL_CONFIG
        from tools.base import StreamPreview
        monkeypatch.setitem(TOOL_CONFIG, "stream_head_lines", 2)
        assert StreamPreview.from_config().limit == 2


class TestTerminalPreviewCap:
    def _run(self, lines: int, sink: list):
        from tools.terminal import TerminalTool
        tool = TerminalTool()
        tool.set_output_callback(sink.append) if hasattr(tool, "set_output_callback") else None
        tool._output_callback = sink.append
        cmd = f'python -c "for i in range({lines}): print(i)"'
        result = tool.execute_json({"command": cmd})
        return result

    def test_long_output_is_capped_but_result_is_complete(self, monkeypatch):
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "stream_head_lines", 5)
        sink: list = []
        result = self._run(30, sink)
        streamed = [s for s in sink if "未实时显示" not in s]
        assert len(streamed) <= 5, f"实时行数应被限流，实际 {len(streamed)}"
        assert any("未实时显示" in s for s in sink), "折叠了就要说明"
        body = result.output if hasattr(result, "output") else str(result)
        assert body.count("\n") >= 25, "工具结果必须是完整输出（不能跟着被截断）"


class TestStreamLabel:
    def test_terminal_label_is_plain(self):
        assert stream_label("terminal", {}) == "terminal"

    def test_delegate_label_names_the_engine(self):
        assert stream_label("delegate", {"runtime": "codex"}) == "sub:codex"

    def test_delegate_label_shows_nesting(self, monkeypatch):
        monkeypatch.setenv("MY_AGENT_DELEGATE_DEPTH", "1")
        assert stream_label("delegate", {"runtime": "claude"}) == "sub:claude#2"

    def test_delegate_label_survives_missing_runtime(self):
        assert stream_label("delegate", {}) == "sub:sub"

    def test_both_streaming_tools_are_registered(self):
        assert "terminal" in STREAM_TOOLS and "delegate" in STREAM_TOOLS


class TestStreamingWiring:
    """delegate 以前没接实时转发（只有 terminal 接），子 agent 的过程完全不可见。"""

    def _run(self, tool, sink):
        from tools.tool_manager import ToolManager
        tm = ToolManager()
        tm.register(tool)
        ex = _executor(sink)
        ex.tool_manager = tm
        return ex._dispatch_tool_call(tool.name, {"runtime": "custom", "goal": "x"}, "目标",
                                      checkpoint=False)

    def test_delegate_streams_to_the_event_sink(self):
        events = []
        self._run(_DelegateTool(), lambda et, data: events.append((et, data)))
        outs = [d for et, d in events if et == "tool_output"]
        assert outs, "子 agent 的输出必须能被实时转发"
        assert outs[0]["label"] == "sub:custom"
        assert "".join(d["text"] for d in outs) == "第一行\n第二行\n"

    def test_result_carries_the_streamed_marker(self):
        """标记挂在结果 metadata 上：主循环的 tool_result 在别处发射，靠它传递。"""
        result, _ = self._run(_DelegateTool(), lambda et, d: None)
        assert (result.metadata or {}).get("streamed") is True

    def test_terminal_still_streams(self):
        events = []
        self._run(_StreamTool(), lambda et, data: events.append((et, data)))
        assert [d["label"] for et, d in events if et == "tool_output"] == ["terminal"] * 2

    def test_non_stream_tool_is_not_streamed(self):
        class _Plain(_StreamTool):
            name = "file"

        events = []
        result, _ = self._run(_Plain(), lambda et, data: events.append((et, data)))
        assert not [d for et, d in events if et == "tool_output"]
        assert not (result.metadata or {}).get("streamed")

    def test_no_sink_does_not_explode(self):
        """终端模式（无面板）也要能跑：回调存在但 sink 为空时静默。"""
        result, blocked = self._run(_DelegateTool(), None)
        assert blocked == "" and result.success is True


class TestMainLoopEmitsStreamedResult:
    """主循环（UI/终端真正消费的那条路径）必须带上 streamed 标记。"""

    def _loop(self, tool, sink):
        from agent.approval import ApprovalPolicy
        from models.llm import LLMToolResponse, ToolCall
        from tests.test_executor_loop import FakeLLM, _make_executor
        from tools.tool_manager import ToolManager
        tm = ToolManager()
        tm.register(tool)
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", tool.name, {"runtime": "custom", "goal": "x"})]),
            LLMToolResponse(content="完成。"),
        ])
        ex = _make_executor(llm, approval=ApprovalPolicy(
            mode="never", sandbox_mode="workspace-write", interactive=False))
        ex.tool_manager = tm
        ex.execute_goal_loop(goal="跑一次", system_prompt="你是测试助手", event_sink=sink)
        return sink

    def test_loop_reports_live_output_and_streamed_result(self):
        events = []
        self._loop(_DelegateTool(), lambda et, data: events.append((et, data)))
        assert [d["label"] for et, d in events if et == "tool_output"] == ["sub:custom"] * 2
        results = [d for et, d in events if et == "tool_result" and d.get("tool") == "delegate"]
        assert results and results[-1]["streamed"] is True, "终端据此只打一行收尾，不重复整段"


class TestCliRendering:
    """事件渲染：实时输出逐段打印、结果不重复、团队成员带归属前缀。"""

    def _agent(self, verbose=True):
        from agent.agent import Agent, AgentConfig
        return Agent(config=AgentConfig(verbose=verbose, session_name="", enable_vision=False))

    def test_tool_output_prints_with_label(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_output", {"tool": "delegate", "label": "sub:codex",
                                               "text": "正在读文件…\n"})
        out = capsys.readouterr().out
        assert "sub:codex" in out and "正在读文件" in out

    def test_tool_name_prefix_is_dropped(self, capsys):
        """工具名做前缀是废话（上一行已写 ⏺ terminal(...)），只在 label 携带信息时才加。"""
        agent = self._agent()
        agent._loop_tool_event("tool_output", {"tool": "terminal", "label": "terminal",
                                               "text": "hello\n"})
        out = capsys.readouterr().out
        assert "hello" in out and "[terminal]" not in out

    def test_worker_tag_is_still_shown(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_output", {"tool": "terminal", "worker": "w1",
                                               "text": "hi\n"})
        assert "团队·w1" in capsys.readouterr().out

    def test_tool_output_is_silent_when_not_verbose(self, capsys):
        agent = self._agent(verbose=False)
        agent._loop_tool_event("tool_output", {"tool": "terminal", "label": "terminal",
                                               "text": "不该出现\n"})
        assert "不该出现" not in capsys.readouterr().out

    def test_streamed_result_is_not_printed_twice(self, capsys):
        agent = self._agent()
        body = "很长的一整段输出" * 3
        agent._loop_tool_event("tool_result", {"tool": "terminal", "success": True,
                                               "output": body, "streamed": True})
        out = capsys.readouterr().out
        assert body not in out, "实时显示过就不该整段再打一遍"
        assert "已实时显示" in out

    def test_unstreamed_result_still_prints_in_full(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_result", {"tool": "file", "success": True,
                                               "output": "文件内容ABC", "streamed": False})
        assert "文件内容ABC" in capsys.readouterr().out

    def test_single_call_result_has_no_tool_name_prefix(self, capsys):
        """单调用结果不加 `terminal: ` 前缀（上一行已写工具名）；多调用批次才要归属。"""
        agent = self._agent()
        agent._loop_tool_event("tool_result", {"tool": "terminal", "success": True,
                                               "output": "结果内容", "index": 1, "total": 1})
        out = capsys.readouterr().out
        assert "结果内容" in out and "terminal:" not in out

    def test_multi_call_batch_keeps_attribution(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_result", {"tool": "terminal", "success": True,
                                               "output": "第二条结果", "index": 2, "total": 2})
        out = capsys.readouterr().out
        assert "[2/2]" in out and "terminal" in out

    def test_team_worker_result_keeps_attribution(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_result", {"tool": "file", "worker": "researcher",
                                               "success": True, "output": "查到的东西",
                                               "index": 1, "total": 1})
        assert "researcher" in capsys.readouterr().out

    def test_team_worker_shows_up_in_the_terminal(self, capsys):
        agent = self._agent()
        agent._loop_tool_event("tool_call", {"tool": "file", "worker": "researcher",
                                             "arguments": {"path": "a.txt"}})
        out = capsys.readouterr().out
        assert "researcher" in out and "file" in out


class TestTeamCliProgress:
    def test_worker_step_lines_are_printed(self, capsys):
        from agent.team import Team
        team = Team(tool_manager=None)
        team._emit("step_start", {"worker": "researcher", "description": "查资料"})
        team._emit("step_end", {"worker": "researcher", "success": True})
        out = capsys.readouterr().out
        assert "团队·researcher" in out and "查资料" in out

    def test_events_without_worker_stay_quiet(self, capsys):
        from agent.team import Team
        team = Team(tool_manager=None)
        team._emit("plan", {"steps": []})
        assert "团队·" not in capsys.readouterr().out
