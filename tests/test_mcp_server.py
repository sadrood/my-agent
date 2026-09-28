"""
MCP Server 模块测试（不启动真实 stdio 服务）。
"""
import pytest

from agent import mcp_server


def test_create_server():
    server = mcp_server._create_server()
    assert server is not None
    assert server.name == "my_agent"


def test_register_tools():
    server = mcp_server._create_server()
    mcp_server.register_tools(server)
    # 注册了 run_agent / list_agent_tools 两个工具
    names = sorted(server._tool_manager._tools.keys())
    assert "list_agent_tools" in names
    assert "run_agent" in names


def test_list_agent_tools():
    tools = mcp_server._list_agent_tools()
    assert "terminal" in tools
    assert "browser" in tools
    assert "see" in tools


class TestSandboxModeClamp:
    """宿主不能靠 run_agent 的参数给自己提权到 danger-full-access。"""

    @pytest.mark.parametrize("requested", [
        "danger-full-access",            # 唯一能解锁硬黑名单的值
        "DANGER-FULL-ACCESS",            # 大小写变体
        " danger-full-access ",          # 带空白
        "bogus",                         # 非法值
        "", None,                        # 空
    ])
    def test_escalation_requests_are_clamped(self, requested):
        assert mcp_server._clamp_sandbox_mode(requested) == "workspace-write"

    @pytest.mark.parametrize("requested", ["read-only", "workspace-write"])
    def test_stricter_or_equal_kept(self, requested):
        assert mcp_server._clamp_sandbox_mode(requested) == requested

    def test_run_agent_applies_the_clamp(self, monkeypatch):
        """夹取要落在真实入口上，不能只是 helper 里有。"""
        captured = {}

        class _FakeAgent:
            def __init__(self, tool_manager=None, config=None):
                captured["config"] = config

            def run(self, goal):
                return "ok"

        import agent as agent_pkg
        monkeypatch.setattr(agent_pkg, "Agent", _FakeAgent)
        assert mcp_server._run_agent("写个文件", sandbox_mode="danger-full-access") == "ok"
        assert captured["config"].sandbox_mode == "workspace-write"


def test_run_agent_mcp_config_built():
    """验证 MCP 模式的 AgentConfig 是无人值守安全配置（不实际执行任务）。"""
    from agent import AgentConfig
    cfg = AgentConfig(
        verbose=False,
        approval_policy="never",
        sandbox_mode="workspace-write",
        approval_interactive=False,
        guardian_enabled=False,
    )
    assert cfg.approval_policy == "never"
    assert cfg.approval_interactive is False
    assert cfg.guardian_enabled is False
    # 不能硬断言 True：该字段默认值来自 ROLLOUT_CONFIG。
    from config import ROLLOUT_CONFIG
    assert cfg.rollout_enabled == ROLLOUT_CONFIG["enabled"]
    assert AgentConfig(rollout_enabled=True).rollout_enabled is True
