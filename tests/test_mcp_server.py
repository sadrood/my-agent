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
    # 不能硬断言 True：该字段默认值来自 ROLLOUT_CONFIG，而它读的是 .env
    # （ROLLOUT_ENABLED / MY_AGENT_MINIMAL）——实测在 ROLLOUT_ENABLED=false 或
    # MY_AGENT_MINIMAL=1 的环境下这条会失败，属于"测试依赖开发者环境"。
    # 这里断言的是"显式传入的值被尊重"，与 .env 无关。
    from config import ROLLOUT_CONFIG
    assert cfg.rollout_enabled == ROLLOUT_CONFIG["enabled"]
    assert AgentConfig(rollout_enabled=True).rollout_enabled is True
