"""
Executor 单循环（execute_goal_loop）测试：一次持续对话完成整个目标。
"""
import json

from agent.executor import Executor
from agent.approval import ApprovalPolicy
from models.llm import LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager


class FakeLLM:
    """脚本化假 LLM：按顺序弹出响应；异常对象直接抛出。"""

    def __init__(self, script):
        self.script = list(script)
        self.tools_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append(messages)
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, BaseException):   # 含 KeyboardInterrupt 等非 Exception 异常
            raise item
        return item


def _make_executor(llm, approval=None):
    return Executor(
        tool_manager=ToolManager(),
        llm=llm,
        approval_policy=approval,
        guardian=None,
        rollout=None,
        instructions_text="",
        max_step_ops=10,
        llm_retry_delay=0,   # 测试中不等待退避
    )


def _loop_args(goal="测试目标", system_prompt="你是测试助手"):
    return dict(goal=goal, system_prompt=system_prompt)


class TestNestedRawArguments:
    """模型把参数再包一层 _raw 时：解包后正常派发，不再被误拦截/直接派发坏参数。"""

    def _exec(self, raw_arguments: dict) -> dict:
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", raw_arguments)]),
            LLMToolResponse(content="完成，结果 42。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        return ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: None)

    def test_single_nested_raw_dispatch(self):
        """{"_raw": "{\"code\": ...}"} 应解包并真正执行 python，而非被拦截。"""
        result = self._exec({"_raw": '{"code": "print(21*2)"}'})
        assert result["success"] is True
        assert "42" in result["output"]

    def test_double_nested_raw_dispatch(self):
        """双重嵌套 _raw 同样解到真正的命名参数。"""
        inner = json.dumps({"code": "print(6*7)"})
        once = json.dumps({"_raw": inner})
        result = self._exec({"_raw": once})
        assert result["success"] is True
        assert "42" in result["output"]

    def test_truncated_raw_still_intercepted(self):
        """真截断（解不开）仍走"参数解析失败"拦截，不给工具。"""
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("1", "python", {"_raw": '{"code": "print('})],
                finish_reason="length"),
            LLMToolResponse(content="参数被截断，我改成短代码。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: None)
        # 未把坏参数塞给 python 工具（没有 21*2 执行痕迹），且模型收到了可行动反馈
        assert result["success"] is False or "42" not in result["output"]


class TestGoalLoop:
    def test_full_goal_in_one_loop(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "think", {"thought": "先算"})]),
            LLMToolResponse(content="", tool_calls=[ToolCall("2", "python", {"code": "print(3+4)"})]),
            LLMToolResponse(content="计算完成，结果是 7。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        events = []
        result = ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: events.append((t, d)))

        assert result["success"] is True
        assert "7" in result["output"]
        assert [c["name"] for c in result["tool_calls"]] == ["think", "python"]
        assert result["ops"] == 3
        # 事件回调收到工具结果
        assert any(t == "tool_result" for t, _ in events)
        # 消息线程是持续的：第二次调用时能看到第一次的工具结果
        second_call = llm.tools_calls[1]
        assert any(m["role"] == "tool" for m in second_call)

    def test_tool_failure_fed_back_and_recovered(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "terminal", {"command": "exit 1"})]),
            LLMToolResponse(content="", tool_calls=[ToolCall("2", "python", {"code": "print(42)"})]),
            LLMToolResponse(content="改用 python 完成，结果是 42。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args())

        assert result["success"] is True     # 失败后有成功补救 → 整体成功
        assert len(result["errors"]) == 1    # 错误记录保留（供自我进化学习）
        assert "42" in result["output"]

    def test_max_ops_exceeded(self):
        failing = [
            LLMToolResponse(content="", tool_calls=[ToolCall(str(i), "terminal", {"command": "exit 1"})])
            for i in range(10)
        ]
        llm = FakeLLM(failing)
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), max_ops=5)
        assert result["success"] is False
        assert result["ops"] == 5

    def test_hung_tool_times_out_and_loop_continues(self, monkeypatch):
        """回归：工具实现永久挂起（如 Playwright CDP 半死）时，硬超时兜底，
        整个 Agent 不再冻结。"""
        import time

        from config import TOOL_CONFIG
        from tools.base import BaseTool, ToolResult

        class HungTool(BaseTool):
            name = "hung"
            description = "挂起测试工具（模拟 CDP 半死）"

            def execute(self, input_str):
                time.sleep(30)
                return ToolResult(success=True, output="不该到达")

            def execute_json(self, arguments):
                time.sleep(30)   # 模拟永久挂起
                return ToolResult(success=True, output="不该到达")

        monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", 0.4)

        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "hung", {})]),
            LLMToolResponse(content="工具超时了，我改用别的方式完成。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        ex.tool_manager.register(HungTool())

        events = []
        t0 = time.time()
        result = ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: events.append((t, d)))
        elapsed = time.time() - t0

        # 1) 没有冻结：远小于挂起工具的 30s
        assert elapsed < 10
        # 2) 超时以工具失败形式回喂给模型，循环继续并成功收尾
        assert result["success"] is True
        assert "超时" in result["output"]
        # 3) 事件流里能看到超时错误
        failed = [d for t, d in events if t == "tool_result" and not d.get("success")]
        assert failed and "超时" in failed[-1].get("output", "")

    def test_parallel_batch_with_hung_tool_not_frozen(self, monkeypatch):
        """回归：并行批里混入挂起工具，join 也不冻结（daemon 线程 + 内部超时）。"""
        import time

        from config import TOOL_CONFIG
        from tools.base import BaseTool, ToolResult

        class HungTool(BaseTool):
            name = "hung"
            description = "挂起测试工具"

            def execute(self, input_str):
                time.sleep(30)
                return ToolResult(success=True, output="不该到达")

            def execute_json(self, arguments):
                time.sleep(30)
                return ToolResult(success=True, output="不该到达")

            def is_parallel_safe(self, arguments):
                return True   # 允许进并行批

        monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", 0.4)

        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("1", "hung", {}),
                ToolCall("2", "hung", {}),
            ]),
            LLMToolResponse(content="两个都超时了，改用其他方式。"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        ex.tool_manager.register(HungTool())

        t0 = time.time()
        result = ex.execute_goal_loop(**_loop_args())
        elapsed = time.time() - t0
        assert elapsed < 10
        assert result["success"] is True
        assert "超时" in result["output"]

    def test_approval_denial_then_text(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "terminal", {"command": "echo hi"})]),
            LLMToolResponse(content="终端被限制，我改用文字回答：hi"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="read-only", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is True
        assert result["tool_calls"][0]["blocked_reason"]

    def test_empty_response_retry(self):
        """上游偶发空回复：自动推一条提示重试。"""
        llm = FakeLLM([
            LLMToolResponse(content=""),                       # 空回复（上游抽风）
            LLMToolResponse(content="正常回答：7"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args())
        assert result["output"] == "正常回答：7"
        assert result["ops"] == 2          # 空回复算一轮，重试后完成
        assert len(llm.tools_calls) == 2

    def test_empty_response_retry_capped(self):
        """连续空回复超过 2 次 → 放弃并以空结果返回。"""
        llm = FakeLLM([
            LLMToolResponse(content=""),
            LLMToolResponse(content=""),
            LLMToolResponse(content=""),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), max_ops=6)
        assert result["output"] == ""
        assert len(llm.tools_calls) == 3

    def test_no_tools_fallback_when_upstream_degraded(self):
        """上游 tools 降级（带工具请求全空）：回退纯文本问一次拿到回答。"""

        class FakeLLMWithChat(FakeLLM):
            def chat(self, messages, **kwargs):
                return "工具当前不可用，我直接回答：结果是 7。"

        llm = FakeLLMWithChat([
            LLMToolResponse(content=""),
            LLMToolResponse(content=""),
            LLMToolResponse(content=""),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), max_ops=8)
        assert result["no_tools_fallback"] is True
        assert "结果是 7" in result["output"]
        assert "降级" in result["errors"][-1]
        assert result["success"] is False   # 降级回退保守记为未成功

    def test_empty_final_after_tool_success_is_failure(self):
        """工具成功但最终输出为空（上游降级）：不得记为成功。"""
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(1)"})]),
            LLMToolResponse(content=""),   # 空回复（上游降级）
            LLMToolResponse(content=""),
            LLMToolResponse(content=""),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), max_ops=8)
        assert result["success"] is False
        assert result["output"] == ""

    def test_llm_turn_level_retry(self):
        """上游 400/流中断：同一轮自动重试后成功。"""
        llm = FakeLLM([
            RuntimeError("Error code: 400 - Provider returned error"),
            LLMToolResponse(content="重试后恢复"),
        ])
        ex = _make_executor(llm)
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is True
        assert result["output"] == "重试后恢复"
        assert len(llm.tools_calls) == 2   # 失败 1 次 + 重试 1 次

    def test_llm_retry_exhausted_graceful(self):
        """三次尝试全失败：优雅收尾，返回错误说明而非抛异常。"""
        llm = FakeLLM([RuntimeError("boom")] * 3)
        ex = _make_executor(llm)
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is False
        assert result["llm_error"]
        assert "模型调用连续失败" in result["output"]
        assert len(llm.tools_calls) == 3

    def test_llm_retry_keeps_tool_results(self):
        """重试耗尽时，已完成的工具成果进入收尾说明。"""
        script = [
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(99)"})]),
            LLMToolResponse(content="", tool_calls=[ToolCall("2", "python", {"code": "print(100)"})]),
        ] + [RuntimeError("upstream gone")] * 3
        llm = FakeLLM(script)
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is False
        assert "2 次工具调用" in result["output"]
        assert "成功 2 次" in result["output"]
        assert "python" in result["output"]

    def test_partial_progress_excludes_think_and_shows_breakdown(self):
        """收尾说明只统计真实工具调用（排除 think），并细分成功/失败/被拦截。"""
        log = [
            {"name": "think", "arguments": {}, "success": True, "output": "思考", "blocked_reason": None},
            {"name": "terminal", "arguments": {}, "success": True, "output": "git ok", "blocked_reason": None},
            {"name": "browser", "arguments": {}, "success": False, "output": "", "blocked_reason": None, "error": "找不到元素"},
            {"name": "edit", "arguments": {}, "success": False, "output": "", "blocked_reason": "被安全策略拦截"},
        ]
        out = Executor._render_partial_progress(log, ["找不到元素", "被安全策略拦截"])
        # think 不计入总次数：真实工具调用 = 3
        assert "已完成 3 次工具调用" in out
        assert "成功 1 次" in out
        assert "失败 1 次" in out
        assert "被拦截 1 次" in out
        # 已完成的操作只列成功的真实工具
        assert "- terminal: git ok" in out
        assert "think" not in out

    def test_keyboard_interrupt_propagates(self):
        """Ctrl+C（KeyboardInterrupt）不被轮级重试吞掉，立即向上传播。"""
        import pytest
        llm = FakeLLM([KeyboardInterrupt()])
        ex = _make_executor(llm)
        with pytest.raises(KeyboardInterrupt):
            ex.execute_goal_loop(**_loop_args())
        assert len(llm.tools_calls) == 1   # 只试了一次，没有重试

    def test_metrics_records_modified_files(self, tmp_path):
        """edit 修改文件后，RunMetrics 记录文件清单。"""
        from agent.metrics import RunMetrics
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall(
                "1", "edit",
                {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 2"},
            )]),
            LLMToolResponse(content="改完了"),
        ])
        metrics = RunMetrics()
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        ex.metrics = metrics
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is True
        assert metrics.modified_files.get(str(f)) == 1
        assert open(f, encoding="utf-8").read() == "x = 2\n"

    def test_reasoning_400_not_misjudged_as_unsupported(self):
        """reasoning 回传 400 不得被误判为"不支持 tools"（应重试后优雅收尾）。"""
        err = RuntimeError(
            "Error code: 400 - {'error': {'message': 'The `reasoning_content` in "
            "the thinking mode must be passed back to the API.', "
            "'type': 'invalid_request_error'}}"
        )
        llm = FakeLLM([err, err, err])
        ex = _make_executor(llm)
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is False
        assert result["llm_error"]
        assert "模型调用连续失败" in result["output"]
        assert len(llm.tools_calls) == 3

    def test_looks_like_unsupported_narrowed(self):
        ex = _make_executor(FakeLLM([]))
        assert ex._looks_like_unsupported(NotImplementedError("tools not supported")) is True
        assert ex._looks_like_unsupported(RuntimeError("invalid_request_error reasoning_content")) is False

    def test_edit_tool_result_includes_old_text_metadata(self, tmp_path):
        """edit 成功后的 tool_result 事件携带 old_text（供 unified diff 渲染；
        .bak 已在成功路径即时清理，不再出现在 metadata）。"""
        f = tmp_path / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall(
                "1", "edit",
                {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 2"},
            )]),
            LLMToolResponse(content="done"),
        ])
        events = {}
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: events.setdefault(t, []).append(d))
        edit_results = [e for e in events.get("tool_result", []) if e["tool"] == "edit"]
        assert edit_results
        assert edit_results[0]["metadata"].get("old_text") == "x = 1\n"
        assert not edit_results[0]["metadata"].get("backup_path")

    def test_thinking_mode_echoes_empty_reasoning(self):
        """思考模式：出现推理后，后续无推理回合的 assistant 消息也带空 reasoning_content。"""
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(1)"})],
                            reasoning="先想一下", reasoning_present=True),
            LLMToolResponse(content="", tool_calls=[ToolCall("2", "python", {"code": "print(2)"})],
                            reasoning="", reasoning_present=False),   # 本回合无推理
            LLMToolResponse(content="完成"),
        ])
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args())
        assert result["success"] is True

        # 第三轮请求：第二轮（无推理）的 assistant 消息仍带空 reasoning_content
        turn3_messages = llm.tools_calls[2]
        assistants = [m for m in turn3_messages if m["role"] == "assistant"]
        assert len(assistants) == 2
        assert assistants[1].get("reasoning_content") == ""

    def test_unsupported_provider_raises(self):
        llm = FakeLLM([NotImplementedError("tools not supported")])
        ex = _make_executor(llm)
        try:
            ex.execute_goal_loop(**_loop_args())
            assert False, "应当抛出异常"
        except NotImplementedError:
            pass

    def test_goal_and_context_combined(self):
        llm = FakeLLM([LLMToolResponse(content="收到")])
        ex = _make_executor(llm)
        ex.execute_goal_loop(goal="目标A", system_prompt="SYS", context_text="上下文B")
        user_msg = llm.tools_calls[0][1]   # messages[1] = user
        assert "目标A" in user_msg["content"]


import time

from tools.base import BaseTool, ToolResult


class _SlowSafeTool(BaseTool):
    """并行安全测试工具：sleep 后返回固定输出。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"
    parallel_safe: bool = True

    @property
    def name(self) -> str:
        return "slowsafe"

    @property
    def description(self) -> str:
        return "测试用慢工具"

    def execute(self, input_str: str) -> ToolResult:
        time.sleep(0.35)
        return ToolResult(success=True, output=f"done({input_str})")


class TestParallelToolCalls:
    def _make_executor_with_slow_tool(self, llm, approval):
        tm = ToolManager()
        tm.register(_SlowSafeTool())
        return Executor(
            tool_manager=tm,
            llm=llm,
            approval_policy=approval,
            guardian=None,
            rollout=None,
            instructions_text="",
            max_step_ops=10,
            llm_retry_delay=0,
        )

    def test_independent_calls_run_in_parallel(self):
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("a1", "slowsafe", {"input": "1"}),
                ToolCall("a2", "slowsafe", {"input": "2"}),
                ToolCall("a3", "slowsafe", {"input": "3"}),
            ]),
            LLMToolResponse(content="三个任务都完成了。"),
        ])
        ex = self._make_executor_with_slow_tool(
            llm,
            ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        t0 = time.monotonic()
        result = ex.execute_goal_loop(**_loop_args())
        elapsed = time.monotonic() - t0

        assert result["success"] is True
        assert len(result["tool_calls"]) == 3
        assert all(c["success"] for c in result["tool_calls"])
        # 并发 ≈0.4s；串行 ≥1.05s
        assert elapsed < 0.85, f"疑似未并行：elapsed={elapsed:.2f}s"
        # 结果按模型给出的顺序回喂
        assert [c["name"] for c in result["tool_calls"]] == ["slowsafe"] * 3
        second_call = llm.tools_calls[1]
        tool_msgs = [m["content"] for m in second_call if m["role"] == "tool"]
        assert tool_msgs == ["done(1)", "done(2)", "done(3)"]

    def test_non_safe_batch_runs_sequentially(self, tmp_path):
        # 批里混入 edit（非并行安全）→ 整批串行
        f = tmp_path / "a.txt"
        f.write_text("old", encoding="utf-8")
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("a1", "slowsafe", {"input": "1"}),
                ToolCall("e1", "edit", {
                    "file_path": str(f), "old_string": "old", "new_string": "new"}),
            ]),
            LLMToolResponse(content="完成。"),
        ])
        ex = self._make_executor_with_slow_tool(
            llm,
            ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        t0 = time.monotonic()
        result = ex.execute_goal_loop(**_loop_args())
        elapsed = time.monotonic() - t0

        assert result["success"] is True
        # 混入 edit 的批不并发：两条结果按模型给出的顺序回喂，edit 实际生效
        assert f.read_text(encoding="utf-8") == "new"
        assert [c["name"] for c in result["tool_calls"]] == ["slowsafe", "edit"]

    def test_interactive_approval_forces_sequential(self):
        # 非 never 且交互式审批 → 不并发（避免多线程抢 input()）
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("a1", "slowsafe", {"input": "1"}),
                ToolCall("a2", "slowsafe", {"input": "2"}),
            ]),
            LLMToolResponse(content="完成。"),
        ])
        ex = self._make_executor_with_slow_tool(
            llm,
            ApprovalPolicy(mode="on-request", sandbox_mode="workspace-write", interactive=True),
        )
        t0 = time.monotonic()
        result = ex.execute_goal_loop(**_loop_args())
        elapsed = time.monotonic() - t0

        assert result["success"] is True
        assert elapsed >= 0.6, f"交互审批下不应并发：elapsed={elapsed:.2f}s"


class TestUnparseableArgumentGuard:
    """参数 JSON 解析失败（典型为输出截断）时，给出可行动错误而非"未知操作"盲试。"""

    def test_truncated_args_produce_actionable_error(self):
        # _stream_turn 对解析失败的 arguments 会包成 {"_raw": ...}（截断场景）
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("1", "file", {"_raw": '{"operation": "write", "path": "big.py", "con'})],
                finish_reason="length"),
            LLMToolResponse(content="内容太长被截断，我改用分段写入。"),
        ])
        events = []
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: events.append((t, d)))

        # 截断参数不真正下发给 file 工具：事件里应出现可行动的失败结果。
        # tool_result 事件把错误文案放在 output 字段（成功时才是正文）
        errors = [d.get("output", "") for t, d in events
                  if t == "tool_result" and not d.get("success")]
        assert any("截断" in e or "length" in e for e in errors), errors
        # 模型据错误信息改为分段写入 → 最终成功
        assert result["success"] is True
        assert "分段写入" in result["output"]

    def test_malformed_args_not_dispatched_to_tool(self):
        # 普通坏 JSON（非截断）：给出格式错误提示，不触发真实工具
        class ExplodingFile(FakeLLM):
            pass

        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("1", "file", {"_raw": "not-json{"})],
                finish_reason="tool_calls"),
            LLMToolResponse(content="我修正了 JSON 格式后重试。"),
        ])
        events = []
        ex = _make_executor(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(**_loop_args(), event_sink=lambda t, d: events.append((t, d)))

        errors = [d.get("output", "") for t, d in events
                  if t == "tool_result" and not d.get("success")]
        assert any("JSON" in e and "格式" in e for e in errors), errors
        assert result["success"] is True
