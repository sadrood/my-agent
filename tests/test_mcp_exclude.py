"""MCP 工具屏蔽：per-server exclude 里的工具不应注册。

背景：Toonflow 暴露了 runAgent（委托给内置 agent，会二次消耗文本模型）；
my_agent 默认只走直调工具，需要能按服务器屏蔽指定工具。
"""
from tools.mcp_client import MCPClient


class _Client(MCPClient):
    def __init__(self, exclude):
        self._servers = {"toonflow": {"exclude": set(exclude)}}
        self._tools = {}
        self._tool_manager = None

    def disconnect(self, *a, **k):
        return None

    def _send_request(self, server_name, method, params):
        assert method == "tools/list"
        return {"tools": [
            {"name": "runAgent", "description": "delegate", "inputSchema": {"type": "object"}},
            {"name": "getCanvas", "description": "read", "inputSchema": {"type": "object"}},
        ]}


def test_exclude_skips_tool():
    c = _Client(["runAgent"])
    count = c._discover_tools("toonflow")
    assert count == 1
    assert "mcp_toonflow_getCanvas" in c._tools
    assert "mcp_toonflow_runAgent" not in c._tools


def test_without_exclude_registers_all():
    c = _Client([])
    assert c._discover_tools("toonflow") == 2
