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
    assert cfg.rollout_enabled is True
