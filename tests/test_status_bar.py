"""
TUI 常驻状态栏测试：渲染内容 + turn_start 事件接线（FakeLLM，无网络）。
"""
import sys
from io import StringIO

import pytest

from agent.metrics import RunMetrics, humanize_tokens


class TestRenderStatusBar:
    def test_empty_run_shows_turn_only(self):
        m = RunMetrics()
        assert m.render_status_bar() == "轮 0"

    def test_tokens_and_cache(self):
        m = RunMetrics(turns=3)
        m.add_llm_call(elapsed=1.0, input_tokens=12300, output_tokens=1800,
                       cached_tokens=8000)
        bar = m.render_status_bar()
        assert "轮 3" in bar
        assert f"↑{humanize_tokens(12300)}" in bar
        assert f"↓{humanize_tokens(1800)}" in bar
        assert "缓存 65%" in bar

    def test_policy_and_sandbox_segments(self):
        m = RunMetrics(turns=1)
        bar = m.render_status_bar(policy="on-failure", sandbox_mode="workspace-write")
        assert "沙箱 workspace-write" in bar
        assert "策略 on-failure" in bar

    def test_appcontainer_suffix(self):
        """OS 级沙箱开启时叠加 AppContainer 标记（Agent 层拼接逻辑）。"""
        m = RunMetrics(turns=1)
        bar = m.render_status_bar(
            policy="never", sandbox_mode="workspace-write+AppContainer")
        assert "workspace-write+AppContainer" in bar


class TestAgentTurnStartWiring:
    @pytest.fixture(autouse=True)
    def _fresh_console(self, monkeypatch):
        """重置 ui_theme 的 Console 单例，让每次输出进入当前测试的 capsys。"""
        import agent.ui_theme as ui
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setattr(ui, "_legacy_console", None)
        yield

    def _make_agent(self, tmp_path, verbose=True):
        """构造测试 Agent（FakeLLM，无网络、无快照），复用 test_agent_loop 惯例。"""
        from agent import Agent, AgentConfig
        from agent.memory import Memory
        from tools.tool_manager import ToolManager
        from tests.test_agent_loop import FakeLLM

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
        return Agent(
            llm=FakeLLM([]),
            tool_manager=ToolManager(),
            memory=Memory(db_path=str(tmp_path)),
            config=config,
        )

    def test_turn_start_prints_status_bar(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "status_bar", True)
        agent = self._make_agent(tmp_path)
        agent.metrics = RunMetrics()
        agent.metrics.add_llm_call(elapsed=1.0, input_tokens=5000, output_tokens=800)
        agent.metrics.turns = 2

        agent._loop_tool_event("turn_start", {"turn": 2, "max_ops": 80})
        out = capsys.readouterr().out
        assert "轮 2" in out
        assert "沙箱 workspace-write" in out
        assert "策略 never" in out

    def test_status_bar_disabled_is_silent(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "status_bar", False)
        agent = self._make_agent(tmp_path)
        agent.metrics = RunMetrics()
        agent.metrics.turns = 1

        agent._loop_tool_event("turn_start", {"turn": 1})
        assert "轮 1" not in capsys.readouterr().out

    def test_non_verbose_is_silent(self, tmp_path, capsys, monkeypatch):
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "status_bar", True)
        agent = self._make_agent(tmp_path, verbose=False)
        agent.metrics = RunMetrics()
        agent.metrics.turns = 1

        agent._loop_tool_event("turn_start", {"turn": 1})
        assert "轮 1" not in capsys.readouterr().out

    def test_other_events_unaffected(self, tmp_path, capsys, monkeypatch):
        """tool_call 渲染路径不被 turn_start 改动影响。"""
        from config import TUI_CONFIG
        monkeypatch.setitem(TUI_CONFIG, "status_bar", True)
        agent = self._make_agent(tmp_path)
        agent.metrics = RunMetrics()
        agent.metrics.turns = 1

        agent._loop_tool_event("tool_call", {"tool": "think", "arguments": {}})
        agent._loop_tool_event("turn_start", {"turn": 1})
        out = capsys.readouterr().out
        assert "轮 1" in out

    def test_switch_model_rewires_llm_metrics(self, tmp_path):
        """回归：switch_model 重建 LLM 后必须重接 metrics——
        否则 _record_usage 写进旧实例，token 统计/上下文水位全部归零。"""
        from agent.metrics import RunMetrics

        agent = self._make_agent(tmp_path)
        m = RunMetrics()
        agent.metrics = m
        agent.llm.metrics = m  # run() 启动时的正常接线状态

        agent.switch_model(model="some-other-model")
        assert agent.llm.metrics is m, "switch_model 重建 LLM 后丢失 metrics 接线"
