"""
Agent 单循环模式（exec_mode="loop"）集成测试：FakeLLM，无网络。
"""
import json

import pytest

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
        # 功能路径：goal 命中意图检测（示例域名不在浏览器关键词表内，不会触发浏览器预启动），verbose=False 使记忆召回分支不执行
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

    def test_max_steps_is_enforced(self, tmp_path):
        """`--max-steps` 必须真的生效。"""
        plan = "\n".join(f"{i}. 第{i}步" for i in range(1, 8))    # 7 步
        script = [NotImplementedError("tools not supported by provider"), plan] + [
            json.dumps({"action": "think", "reasoning": "想了下"})   # 每步都成功
            for _ in range(7)
        ] + ["总结完毕"]
        agent = make_agent(tmp_path, script, max_steps=3)
        agent.run("跑个多步计划")
        assert agent.state.current_step == 3, (
            f"max_steps=3 没生效，实际跑了 {agent.state.current_step} 步")

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


class TestTurnSpinnerTimer:
    """思考转圈带秒表：上游慢的时候，能看出是"还在等"还是"卡住了"。"""

    class _StubSpinner:
        def __init__(self):
            self.texts = []
            self.stopped = False

        def update(self, text):
            self.texts.append(text)

        def stop(self):
            self.stopped = True

    @staticmethod
    def _bare_agent():
        agent = Agent.__new__(Agent)        # 只测秒表方法，不走完整初始化
        agent._turn_spinner = None
        agent._turn_spinner_timer = None
        return agent

    @staticmethod
    def _running_timers():
        import threading
        return [t.name for t in threading.enumerate() if t.name == "turn-spinner-timer"]

    def test_timer_refreshes_with_elapsed_seconds(self):
        import time
        agent = self._bare_agent()
        spinner = self._StubSpinner()
        agent._start_spinner_timer(spinner)
        time.sleep(0.3)
        agent._turn_spinner = spinner
        agent._stop_turn_spinner()

        assert len(spinner.texts) >= 2, "秒表应持续刷新"
        text = spinner.texts[0]
        assert "思考中" in text and "s" in text, f"应带秒数，实际 {text!r}"
        # token 计数与中断方式是后来补的：光有秒数看不出"在等什么"
        assert "↑" in text and "↓" in text, f"应带 token 计数，实际 {text!r}"
        assert "Ctrl+C" in text, f"应告诉用户怎么中断，实际 {text!r}"
        assert spinner.stopped is True

    def test_timer_thread_is_reclaimed(self):
        import time
        agent = self._bare_agent()
        spinner = self._StubSpinner()
        agent._start_spinner_timer(spinner)
        time.sleep(0.2)
        assert self._running_timers(), "启动后应有秒表线程"
        agent._turn_spinner = spinner
        agent._stop_turn_spinner()
        time.sleep(0.15)
        assert self._running_timers() == [], "停止后秒表线程必须回收"
        assert agent._turn_spinner_timer is None

    def test_stop_is_safe_before_any_start(self):
        agent = self._bare_agent()
        agent._stop_turn_spinner()          # 不该抛
        assert agent._turn_spinner is None

    def test_stop_is_idempotent(self):
        agent = self._bare_agent()
        spinner = self._StubSpinner()
        agent._start_spinner_timer(spinner)
        agent._turn_spinner = spinner
        agent._stop_turn_spinner()
        agent._stop_turn_spinner()          # 再来一次也不该抛
        assert agent._turn_spinner_timer is None


class TestRepeatedCallFolding:
    """连续同名工具调用折叠成一行汇总（长任务防刷屏）。

    edit / 写文件 / 团队 worker 的调用不参与折叠：那些是要看的内容。
    """

    @pytest.fixture(autouse=True)
    def _fresh_console(self, monkeypatch):
        """重置 ui_theme 的 Console 单例，让输出进入当前测试的 capsys。"""
        import agent.ui_theme as ui
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setattr(ui, "_legacy_console", None)
        yield

    @staticmethod
    def _make_agent(tmp_path, verbose=True):
        config = AgentConfig(
            verbose=verbose,
            rollout_enabled=False,
            guardian_enabled=False,
            approval_policy="never",
            sandbox_mode="workspace-write",
            approval_interactive=False,
            instructions_enabled=False,
            enable_vision=False,
            enable_frame_compare=False,
            enable_anomaly_detect=False,
            snapshot_enabled=False,
            checkpoint_per_tool=False,
            repomap_enabled=False,
        )
        return Agent(llm=FakeLLM([]), tool_manager=ToolManager(),
                     memory=Memory(db_path=str(tmp_path)), config=config)

    @staticmethod
    def _call(agent, tool, **args):
        agent._loop_tool_event("tool_call", {"tool": tool, "arguments": args})

    def test_first_three_printed_fourth_onwards_folded(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(6):
            self._call(agent, "terminal", command=f"cmd{i}")
        out = capsys.readouterr().out

        assert "cmd0" in out and "cmd2" in out
        assert "cmd3" not in out and "cmd5" not in out

    def test_summary_line_when_switching_tool(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(6):
            self._call(agent, "terminal", command=f"cmd{i}")
        capsys.readouterr()                      # 丢掉前半段，只看换工具时的那一行
        self._call(agent, "file", operation="read", path="a.py")
        out = capsys.readouterr().out

        assert "×6" in out and "3 次同名调用已折叠" in out
        assert "a.py" in out

    def test_run_end_flushes_summary(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 2)
        agent = self._make_agent(tmp_path)

        for i in range(5):
            self._call(agent, "terminal", command=f"cmd{i}")
        capsys.readouterr()
        agent._loop_tool_event("run_end", {"status": "completed"})

        assert "×5" in capsys.readouterr().out

    def test_short_streak_has_no_summary(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(3):
            self._call(agent, "terminal", command=f"cmd{i}")
        agent._loop_tool_event("run_end", {"status": "completed"})
        out = capsys.readouterr().out

        assert "cmd0" in out and "cmd2" in out
        assert "已折叠" not in out

    def test_folded_call_result_is_suppressed(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(5):
            self._call(agent, "terminal", command=f"cmd{i}")
            agent._loop_tool_event("tool_result",
                                   {"tool": "terminal", "success": True, "output": f"out{i}"})
        out = capsys.readouterr().out

        assert "out0" in out and "out2" in out
        assert "out3" not in out and "out4" not in out

    def test_edit_never_folds(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(6):
            self._call(agent, "edit", file_path=f"f{i}.py", old_string="a", new_string="b")
        out = capsys.readouterr().out

        assert out.count("✏") == 6, "edit 的每一处改动都要看得见"
        assert "已折叠" not in out

    def test_file_write_never_folds(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 3)
        agent = self._make_agent(tmp_path)

        for i in range(6):
            self._call(agent, "file", operation="write", path=f"w{i}.txt")
        out = capsys.readouterr().out

        assert "w5.txt" in out
        assert "已折叠" not in out

    def test_team_worker_calls_never_fold(self, tmp_path, capsys, monkeypatch):
        """带归属前缀的团队调用不折叠（前缀是分辨谁在干活的关键）。"""
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 2)
        agent = self._make_agent(tmp_path)

        for i in range(4):
            agent._loop_tool_event("tool_call", {"tool": "terminal",
                                                 "arguments": {"command": f"cmd{i}"},
                                                 "worker": "w1"})
        out = capsys.readouterr().out

        assert "cmd3" in out and "已折叠" not in out

    def test_zero_disables_folding(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 0)
        agent = self._make_agent(tmp_path)

        for i in range(8):
            self._call(agent, "terminal", command=f"cmd{i}")
        agent._loop_tool_event("run_end", {"status": "completed"})
        out = capsys.readouterr().out

        assert "cmd7" in out and "已折叠" not in out

    def test_folding_does_not_swallow_events(self, tmp_path, monkeypatch, capsys):
        """折叠只影响终端渲染；事件照旧发给 Dashboard/监听方。"""
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 2)
        agent = self._make_agent(tmp_path)
        seen = []
        monkeypatch.setattr(agent, "_emit", lambda et, d: seen.append(et))

        for i in range(5):
            self._call(agent, "terminal", command=f"cmd{i}")

        assert seen == ["tool_call"] * 5

    def test_non_verbose_is_silent(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "repeat_call_fold_after", 2)
        agent = self._make_agent(tmp_path, verbose=False)

        for i in range(5):
            self._call(agent, "terminal", command=f"cmd{i}")
        agent._loop_tool_event("run_end", {"status": "completed"})

        assert capsys.readouterr().out == ""
