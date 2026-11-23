"""MCP 配置容错：非法 timeout 不中断启动，exclude 写错要能看见。"""
import json

import tools.mcp_client as mcp_mod
from tools.mcp_client import MCPClient

_TOOLS = [
    {"name": "getCanvas", "description": "read", "inputSchema": {"type": "object"}},
    {"name": "runAgent", "description": "delegate", "inputSchema": {"type": "object"}},
]


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, s):
        req = json.loads(s)
        if "id" not in req:
            return
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {"tools": _TOOLS}
        else:
            result = {"content": [{"text": "ok"}]}
        self.proc.responses.append(
            json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\n")

    def flush(self):
        pass

    def close(self):
        pass


class _FakeProc:
    def __init__(self, argv, **kw):
        self.responses = []
        self.stdin = _FakeStdin(self)
        self.stdout = self
        self.returncode = None

    def readline(self):
        return self.responses.pop(0) if self.responses else ""

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_mod.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(mcp_mod, "_cache_dir", lambda: str(tmp_path))


def test_bad_timeout_degrades_instead_of_crashing(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    c = MCPClient()

    assert c.connect_stdio("t", "x", [], timeout="120s")
    assert c._servers["t"]["timeout"] is None                 # 降级到默认值
    assert "不是数字" in capsys.readouterr().out


def test_exclude_accepts_comma_string(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    c = MCPClient()

    assert c.connect_stdio("t", "x", [], exclude="runAgent")
    assert "mcp_t_runAgent" not in c.list_tools()              # 不被逐字符拆开
    assert "mcp_t_getCanvas" in c.list_tools()
    assert "不存在" not in capsys.readouterr().out              # 命中就不该告警


def test_exclude_typo_is_reported(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    c = MCPClient()

    assert c.connect_stdio("t", "x", [], exclude=["runAgnet"])
    out = capsys.readouterr().out
    assert "runAgnet" in out and "不存在" in out
    assert "mcp_t_runAgent" in c.list_tools()                   # 拼错时工具仍注册，所以必须喊出来
