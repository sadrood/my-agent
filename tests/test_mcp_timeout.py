"""MCP 每服务器请求超时：SSH 远程桥接的握手/调用可能超过本地默认 15s，需可配置。"""
import json
import threading
import time

from tools.mcp_client import MCPClient


class _Stdin:
    def write(self, s):
        pass

    def flush(self):
        pass


class _Stdout:
    def __init__(self, line, delay):
        self.line, self.delay = line, delay

    def readline(self):
        time.sleep(self.delay)
        return self.line


class _Proc:
    def __init__(self, stdout):
        self.stdin = _Stdin()
        self.stdout = stdout

    def poll(self):
        return None

    def kill(self):
        pass


def _client(timeout, stdout):
    c = MCPClient()
    c._servers["toonflow"] = {
        "type": "stdio", "name": "toonflow", "process": _Proc(stdout),
        "lock": threading.Lock(), "timeout": timeout,
    }
    return c


def test_server_timeout_bounds_wait():
    resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n"
    c = _client(0.3, _Stdout(resp, delay=5))
    t0 = time.time()
    assert c._send_request("toonflow", "initialize", {}) is None
    assert time.time() - t0 < 2


def test_slow_response_within_timeout_succeeds():
    resp = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) + "\n"
    c = _client(5, _Stdout(resp, delay=0.5))
    assert c._send_request("toonflow", "initialize", {}) == {"ok": True}