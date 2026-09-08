"""
Agent 单循环模式（exec_mode="loop"）集成测试：FakeLLM，无网络。
"""
import json

from agent import Agent, AgentConfig
from agent.memory import Memory
from models.llm import LLMToolResponse, ToolCall
from tools.tool_manager import ToolManager


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.tools_calls = []
        self.chat_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append(messages)
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        if not self.script:
            return ""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_agent(tmp_path, script, **cfg_overrides):
    config = AgentConfig(
        exec_mode="loop",
        verbose=False,
        rollout_enabled=False,
        guardian_enabled=False,
        approval_policy="never",
        sandbox_mode="workspace-write",
        approval_interactive=False,
        instructions_enabled=False,
        enable_vision=False,
        enable_frame_compare=False,
        enable_anomaly_detect=False,
        snapshot_enabled=False,   # 测试不得触碰真实项目的 git 快照
        checkpoint_per_tool=False,
        repomap_enabled=False,
        **cfg_overrides,
    )
    return Agent(
        llm=FakeLLM(script),
        tool_manager=ToolManager(),
        memory=Memory(db_path=str(tmp_path)),
        config=config,
    )


class TestAgentLoopMode:
    def test_simple_task_end_to_end(self, tmp_path):
        agent = make_agent(tmp_path, [
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(3+4)"})]),
            LLMToolResponse(content="用 Python 算好了，3+4 = 7。"),
        ])
        out = agent.run("用python计算3加4")
        assert "7" in out
        # 经验已保存到临时记忆库
        assert len(agent.memory.experiences) >= 1
        exp = agent.memory.experiences[-1]
        assert exp.success is True
        assert exp.tool_usage.get("python") == 1
        # 会话消息已记录
        roles = [getattr(m, "role", "") for m in agent.memory.conversation_history]
        assert "user" in roles and "assistant" in roles

    def test_loop_mode_skips_planner(self, tmp_path):
        """循环模式不应调用 Planner（planner 的 chat 调用次数为 0）。"""
        agent = make_agent(tmp_path, [
            LLMToolResponse(content="直接回答：你好"),
        ])
        out = agent.run("打个招呼")
        assert "你好" in out
        # 脚本里没有任何 planner 响应 → chat 调用次数应为 0（计划模式会有多次）
        assert len(agent.llm.chat_calls) == 0

    def test_site_goal_no_unbound_print_info(self, tmp_path):
        """回归：带网站名的目标（命中意图检测 → print_info）且记忆召回分支未执行时，
        不得触发 UnboundLocalError（历史上 _run_loop 内局部 import print_info 遮蔽了模块级导入）。"""
        import inspect
        from agent import Agent as AgentCls
        src = inspect.getsource(AgentCls._run_loop)
        assert "from agent.ui_theme import print_info" not in src, (
            "_run_loop 内禁止局部导入 print_info（会遮蔽模块级导入导致 UnboundLocalError）"
        )
        # 功能路径：goal 命中意图检测（deepseek → chat.deepseek.com，不在浏览器关键词表内，
        # 不会触发浏览器预启动），verbose=False 使记忆召回分支不执行
        agent = make_agent(tmp_path, [
            LLMToolResponse(content="好的，这就去 deepseek 查。"),
        ])
        out = agent.run("去deepseek看看今天的新闻")
        assert "deepseek" in out.lower()

    def test_fallback_to_plan_mode_when_tools_unsupported(self, tmp_path):
        script = [
            NotImplementedError("tools not supported by provider"),
            # 以下为计划模式需要的文本响应（通过 chat() 消费）
            "1. 用python工具执行print(6+6)",        # planner
            json.dumps({"action": "use_tool", "tool": "python", "tool_input": "print(6+6)"}),  # legacy executor
            "完成了",                                # 总结
        ]
        agent = make_agent(tmp_path, script)
        out = agent.run("用python计算6加6")
        assert "12" in out or "完成了" in out
        # 发生了回退：exec_mode 被切换为 plan
        assert agent.config.exec_mode == "plan"

    def test_dashboard_events_emitted(self, tmp_path):
        agent = make_agent(tmp_path, [
            LLMToolResponse(content="", tool_calls=[ToolCall("1", "python", {"code": "print(1)"})]),
            LLMToolResponse(content="完成"),
        ])
        seen = []

        class FakeHub:
            def emit(self, event_type, data):
                seen.append(event_type)

        agent._dashboard = FakeHub()
        agent.run("测试")
        assert "run_start" in seen
        assert "tool_result" in seen
        assert "run_end" in seen


class TestStreamingBoldRendering:
    def test_streamed_bold_markers_consumed(self, tmp_path, monkeypatch):
        """流式答案中的 **加粗** 标记被渲染消费，不残留字面星号。"""
        import io
        from rich.console import Console
        import agent.ui_theme as ui
        from models.llm import StreamEvent

        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, highlight=False)
        monkeypatch.setattr(ui, "get_console", lambda use_rich=True: console)

        class FakeStreamLLM:
            def __init__(self):
                self.calls = 0

            def chat_with_tools_stream(self, messages, tools, **kwargs):
                self.calls += 1
                yield StreamEvent(type="text_delta", text="结果是 ")
                yield StreamEvent(type="text_delta", text="**42**")
                yield StreamEvent(type="done", finish_reason="stop")

        config = AgentConfig(
            exec_mode="loop",
            verbose=True,                       # 开启渲染路径
            rollout_enabled=False,
            guardian_enabled=False,
            approval_policy="never",
            sandbox_mode="workspace-write",
            approval_interactive=False,
            instructions_enabled=False,
            enable_vision=False,
            enable_frame_compare=False,
            enable_anomaly_detect=False,
            snapshot_enabled=False,             # 测试不得触碰真实项目的 git 快照
            checkpoint_per_tool=False,
            repomap_enabled=False,
        )
        agent = Agent(
            llm=FakeStreamLLM(),
            tool_manager=ToolManager(),
            memory=Memory(db_path=str(tmp_path)),
            config=config,
        )
        out = agent.run("测试加粗")
        assert "42" in out
        captured = buf.getvalue()
        assert "**" not in captured          # 星号被流式 Markdown 渲染消费
        assert "结果是 " in captured
        assert "42" in captured
