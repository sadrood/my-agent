"""MCP 工具名解析回归：服务器名含下划线时必须仍能路由。

实测故障（2026-09-22 审计）：工具注册名是 `mcp_{server}_{tool}`，而解析用的是
`tool_full_name[4:].split("_", 1)` —— 第一个下划线就切。服务器名带下划线时
（如 `my_fs`）`mcp_my_fs_read_file` 会解出服务器名 `"my"`，查不到 → 返回 None，
调用方只看到"MCP 工具无响应"，与超时/崩溃无法区分。
"""
import pytest

from tools.mcp_client import MCPClient


class _Client(MCPClient):
    """绕过 __init__（会起线程/连服务器），只测名字解析。"""

    def __init__(self, server_names):
        self._servers = {n: {} for n in server_names}
        self.sent = []

    def disconnect(self, *a, **k):
        """__del__ 会调它；替身没有真连接，直接 no-op（否则测试收尾刷告警）。"""
        return None

    def _send_request(self, server_name, method, params):
        self.sent.append((server_name, method, params))
        return {"ok": True}


@pytest.mark.parametrize("servers,full,exp_server,exp_tool", [
    (["my_fs"], "mcp_my_fs_read_file", "my_fs", "read_file"),
    (["fs"], "mcp_fs_read_file", "fs", "read_file"),
    (["a_b_c"], "mcp_a_b_c_do_thing", "a_b_c", "do_thing"),
    (["srv", "my_srv"], "mcp_my_srv_act", "my_srv", "act"),      # 最长前缀优先
    (["srv", "my_srv"], "mcp_srv_act", "srv", "act"),
])
def test_server_name_with_underscore(servers, full, exp_server, exp_tool):
    c = _Client(servers)
    c.call_tool(full, {"x": 1})
    assert c.sent, f"{full} 没被路由出去"
    server, method, params = c.sent[0]
    assert server == exp_server, f"服务器名解错了：{server}"
    assert params["name"] == exp_tool


def test_bad_prefix_rejected():
    c = _Client(["srv"])
    out = c.call_tool("not_mcp_thing", {})
    assert out.get("isError") is True
    assert not c.sent


def test_unknown_server_falls_back_to_old_split():
    """服务器还没登记时按老办法切，至少把请求发出去（而不是静默丢弃）。"""
    c = _Client([])
    c.call_tool("mcp_unknown_tool_x", {})
    assert c.sent and c.sent[0][0] == "unknown"
