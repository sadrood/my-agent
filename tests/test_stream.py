"""
流式输出测试：LLM 流式事件解析 + Executor 流式循环。
"""
import json

from agent.executor import Executor
from agent.approval import ApprovalPolicy
from models.llm import LLM, StreamEvent, LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager


# ============================================================
# Fake 流式对象（模拟 openai 流式 chunk 结构）
# ============================================================

class FakeFn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class FakeTC:
    def __init__(self, index, name=None, arguments=None):
        self.index = index
        self.function = FakeFn(name, arguments)


class FakeDelta:
    def __init__(self, content=None, reasoning=None, tool_calls=None):
        self.content = content
        self.reasoning = reasoning
        self.reasoning_content = None
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta or FakeDelta()
        self.finish_reason = finish_reason


class FakeChunk:
    def __init__(self, choice=None, usage=None):
        self.choices = [choice] if choice else []
        self.usage = usage


class FakeUsageDetails:
    def __init__(self, cached):
        self.cached_tokens = cached


class FakeUsage:
    def __init__(self, prompt, completion, cached):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = FakeUsageDetails(cached)


class FakeCompletions:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def create(self, **kwargs):
        return self.chunks


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


class TestLLMStream:
    def test_stream_events_sequence(self):
        chunks = [
            FakeChunk(FakeChoice(FakeDelta(content="你"), None)),
            FakeChunk(FakeChoice(FakeDelta(reasoning="让我想想"), None)),
            FakeChunk(FakeChoice(FakeDelta(tool_calls=[
                FakeTC(0, name="add", arguments='{"a":'),
                FakeTC(1, name="sub", arguments='{"x":'),
            ]), None)),
            FakeChunk(FakeChoice(FakeDelta(tool_calls=[
                FakeTC(0, arguments='3,"b":4}'),
                FakeTC(1, arguments='1}'),
            ]), None)),
            FakeChunk(FakeChoice(FakeDelta(), "tool_calls")),
        ]
        llm = LLM()
        llm.client = FakeClient(FakeCompletions(chunks))
        llm.max_retries = 0

        events = list(llm.chat_with_tools_stream(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "add", "parameters": {"type": "object"}}}],
        ))
        types = [e.type for e in events]
        assert types == ["text_delta", "reasoning_delta", "tool_delta", "tool_delta", "tool_delta", "tool_delta", "done"]
        text = "".join(e.text for e in events if e.type == "text_delta")
        assert text == "你"
        reasoning = "".join(e.text for e in events if e.type == "reasoning_delta")
        assert reasoning == "让我想想"
        assert events[-1].finish_reason == "tool_calls"

    def test_stream_turn_parsing(self):
        """Executor._stream_turn 把流式事件累积为 LLMToolResponse。"""
        chunks = [
            FakeChunk(FakeChoice(FakeDelta(content="开头"), None)),
            FakeChunk(FakeChoice(FakeDelta(tool_calls=[
                FakeTC(0, name="python", arguments='{"code": "print(9)"}'),
            ]), None)),
            FakeChunk(FakeChoice(FakeDelta(), "tool_calls")),
        ]
        llm = LLM()
        llm.client = FakeClient(FakeCompletions(chunks))
        llm.max_retries = 0

        ex = Executor(tool_manager=ToolManager(), llm=llm)
        deltas = []
        resp = ex._stream_turn(
            [{"role": "user", "content": "x"}],
            [{"type": "function", "function": {"name": "python", "parameters": {"type": "object"}}}],
            on_text_delta=lambda k, t: deltas.append((k, t)),
        )
        assert resp.content == "开头"
        assert resp.finish_reason == "tool_calls"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "python"
        assert resp.tool_calls[0].arguments == {"code": "print(9)"}
        assert deltas == [("text", "开头")]


class FakeStreamLLM:
    """按轮次脚本化产出流式事件的假 LLM。"""

    def __init__(self, turn_scripts):
        self.turn_scripts = list(turn_scripts)
        self.calls = 0
        self.seen_messages = []

    def chat_with_tools_stream(self, messages, tools, **kwargs):
        self.calls += 1
        self.seen_messages.append(messages)
        if not self.turn_scripts:
            yield StreamEvent(type="text_delta", text="（脚本耗尽）")
            yield StreamEvent(type="done", finish_reason="stop")
            return
        for ev in self.turn_scripts.pop(0):
            yield ev


class TestExecutorStreamLoop:
    def _make(self, llm, approval=None):
        return Executor(
            tool_manager=ToolManager(),
            llm=llm,
            approval_policy=approval,
            guardian=None,
            rollout=None,
            instructions_text="",
            max_step_ops=10,
        )

    def test_stream_loop_full_flow(self):
        llm = FakeStreamLLM([
            # 第 1 轮：调用 python 工具
            [
                StreamEvent(type="tool_delta", tool_index=0, tool_name="python",
                            tool_args_delta='{"code": "print(6+6)"}'),
                StreamEvent(type="done", finish_reason="tool_calls"),
            ],
            # 第 2 轮：流式输出最终答案
            [
                StreamEvent(type="text_delta", text="结果是 "),
                StreamEvent(type="text_delta", text="12。"),
                StreamEvent(type="done", finish_reason="stop"),
            ],
        ])
        ex = self._make(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        deltas = []
        turns = []
        result = ex.execute_goal_loop(
            goal="计算6加6",
            system_prompt="你是助手",
            stream=True,
            on_turn_start=lambda: turns.append(1),
            on_text_delta=lambda k, t: deltas.append((k, t)),
        )

        assert result["success"] is True
        assert result["output"] == "结果是 12。"
        assert [c["name"] for c in result["tool_calls"]] == ["python"]
        assert result["tool_calls"][0]["success"] is True
        assert "12" in result["tool_calls"][0]["output"]
        assert deltas == [("text", "结果是 "), ("text", "12。")]
        assert len(turns) == 2   # 每轮模型调用前触发

    def test_stream_records_usage_and_first_token(self):
        """流式调用把 usage 与首 token 延迟记入 RunMetrics。"""
        from agent.metrics import RunMetrics
        chunks = [
            FakeChunk(FakeChoice(FakeDelta(content="答"), None)),
            FakeChunk(FakeChoice(FakeDelta(), "stop")),
            FakeChunk(usage=FakeUsage(prompt=100, completion=20, cached=60)),
        ]
        llm = LLM()
        llm.client = FakeClient(FakeCompletions(chunks))
        llm.max_retries = 0
        metrics = RunMetrics()
        llm.metrics = metrics

        list(llm.chat_with_tools_stream(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}],
        ))
        assert metrics.input_tokens == 100
        assert metrics.output_tokens == 20
        assert metrics.cached_tokens == 60
        assert len(metrics.first_token_seconds) == 1
        assert metrics.llm_seconds > 0

    def test_reasoning_roundtrip_in_message_thread(self):
        """思考模式：reasoning 必须在下一轮消息中原样回传（否则服务器 400）。"""
        llm = FakeStreamLLM([
            [
                StreamEvent(type="reasoning_delta", text="让我想想", field="reasoning_content"),
                StreamEvent(type="tool_delta", tool_index=0, tool_name="python",
                            tool_args_delta='{"code": "print(1)"}'),
                StreamEvent(type="done", finish_reason="tool_calls"),
            ],
            [
                StreamEvent(type="text_delta", text="完成"),
                StreamEvent(type="done", finish_reason="stop"),
            ],
        ])
        ex = self._make(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(goal="g", system_prompt="s", stream=True)
        assert result["success"] is True
        # 第二轮请求中的 assistant 消息带回了 reasoning_content
        turn2_messages = llm.seen_messages[1]
        assistant_msgs = [m for m in turn2_messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].get("reasoning_content") == "让我想想"

    def test_nonstream_reasoning_captured(self):
        """非流式调用同样捕获 reasoning 供回传。"""
        class FakeMsg:
            content = ""
            reasoning_content = "思考内容"
            reasoning = None
            tool_calls = None

        class FakeChoice2:
            message = FakeMsg()
            finish_reason = "stop"

        class FakeResp2:
            choices = [FakeChoice2()]
            usage = None

        class FakeCompletionsSingle:
            def __init__(self, resp):
                self.resp = resp

            def create(self, **kwargs):
                return self.resp

        llm = LLM()
        llm.client = FakeClient(FakeCompletionsSingle(FakeResp2()))
        llm.max_retries = 0
        resp = llm.chat_with_tools([], [])
        assert resp.reasoning == "思考内容"
        assert resp.reasoning_field == "reasoning_content"
        assert resp.reasoning_present is True

    def test_empty_reasoning_field_presence_detected(self):
        """推理字段存在但为空串：reasoning_present 仍为 True（thinking 服务要求回传空字段）。"""
        class FakeMsg:
            content = "直接回答"
            reasoning_content = ""      # 空串但字段存在
            reasoning = None
            tool_calls = None

        class FakeChoice2:
            message = FakeMsg()
            finish_reason = "stop"

        class FakeResp2:
            choices = [FakeChoice2()]
            usage = None

        class FakeCompletionsSingle:
            def __init__(self, resp):
                self.resp = resp

            def create(self, **kwargs):
                return self.resp

        llm = LLM()
        llm.client = FakeClient(FakeCompletionsSingle(FakeResp2()))
        llm.max_retries = 0
        resp = llm.chat_with_tools([], [])
        assert resp.reasoning == ""
        assert resp.reasoning_present is True   # 关键：字段存在即标记

    def test_stream_empty_reasoning_field(self):
        """流式：空 reasoning_content 增量也要产出事件（标记字段存在）。"""
        empty_delta = type("D", (), {"reasoning_content": "", "reasoning": None, "tool_calls": None})()
        chunks = [
            FakeChunk(FakeChoice(FakeDelta(content="答"), None)),
            FakeChunk(FakeChoice(empty_delta, None)),
            FakeChunk(FakeChoice(FakeDelta(), "stop")),
        ]
        llm = LLM()
        llm.client = FakeClient(FakeCompletions(chunks))
        llm.max_retries = 0

        ex = Executor(tool_manager=ToolManager(), llm=llm)
        resp = ex._stream_turn(
            [{"role": "user", "content": "x"}],
            [{"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}],
        )
        assert resp.content == "答"
        assert resp.reasoning_present is True
        assert resp.reasoning == ""

    def test_stream_fallback_to_nonstream(self):
        """LLM 没有流式方法时回退一次性调用。"""
        from tests.test_executor_loop import FakeLLM  # 复用非流式假 LLM
        llm = FakeLLM([
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(1)"})]),
            LLMToolResponse(content="完成"),
        ])
        ex = self._make(
            llm,
            approval=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
        )
        result = ex.execute_goal_loop(goal="g", system_prompt="s", stream=True)
        assert result["success"] is True
        assert "完成" in result["output"]
