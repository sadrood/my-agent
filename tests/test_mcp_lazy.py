"""MCP 按需连接（lazy）：启动只注册缓存工具，首次调用才起进程。"""
import json
import os

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
    instances = []

    def __init__(self, argv, **kw):
        self.responses = []
        self.stdin = _FakeStdin(self)
        self.stdout = self
        self.killed = False
        self.returncode = None
        _FakeProc.instances.append(self)

    def readline(self):
        return self.responses.pop(0) if self.responses else ""

    def close(self):
        pass

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.killed = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


def _setup(monkeypatch, tmp_path):
    _FakeProc.instances = []
    monkeypatch.setattr(mcp_mod.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(mcp_mod, "_cache_dir", lambda: str(tmp_path))


def test_lazy_with_cache_defers_process(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    with open(os.path.join(str(tmp_path), "toonflow.json"), "w", encoding="utf-8") as f:
        json.dump({"tools": _TOOLS}, f)

    c = MCPClient()
    assert c.connect_stdio("toonflow", "ssh", [], exclude=["runAgent"], lazy=True)
    assert "[MCP]" not in capsys.readouterr().out      # 按需连接：启动不输出任何 MCP 日志
    assert not _FakeProc.instances                     # 启动未起进程
    assert "mcp_toonflow_getCanvas" in c.list_tools()
    assert "mcp_toonflow_runAgent" not in c.list_tools()

    result = c.call_tool("mcp_toonflow_getCanvas", {})  # 首次调用才连接
    assert len(_FakeProc.instances) == 1
    assert result["content"][0]["text"] == "ok"


def test_lazy_bootstrap_caches_then_disconnects(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    c = MCPClient()
    assert c.connect_stdio("toonflow", "ssh", [], exclude=["runAgent"], lazy=True)
    assert len(_FakeProc.instances) == 1               # 无缓存：连一次取清单
    assert _FakeProc.instances[0].killed               # 取完即断开
    assert os.path.exists(os.path.join(str(tmp_path), "toonflow.json"))
    assert "mcp_toonflow_getCanvas" in c.list_tools()
    assert c._servers["toonflow"]["process"] is None    # 占位条目，无活进程

    result = c.call_tool("mcp_toonflow_getCanvas", {})  # 调用时重连
    assert len(_FakeProc.instances) == 2
    assert result["content"][0]["text"] == "ok"


def test_non_lazy_connects_immediately(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    c = MCPClient()
    assert c.connect_stdio("toonflow", "ssh", [])
    assert len(_FakeProc.instances) == 1
    assert not _FakeProc.instances[0].killed
    assert c._servers["toonflow"]["process"] is not None