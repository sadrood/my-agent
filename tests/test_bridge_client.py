"""内嵌浏览器桥客户端：命令串行化、瞬态重试、错误分类、探测缓存。"""
import json
import threading
import time

import pytest

import tools.bridge as bridge
from tools.embedded_browser import EmbeddedBrowserTool


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestErrorClassification:
    def test_transient_signs(self):
        assert bridge.is_transient(ConnectionRefusedError(10061, "refused"))
        assert bridge.is_transient(TimeoutError("timed out"))
        assert bridge.is_transient(RuntimeError("connection reset by peer"))

    def test_fatal_errors_are_not_transient(self):
        assert not bridge.is_transient(ValueError("bad json"))
        assert not bridge.is_transient(RuntimeError("unknown action: foo"))

    def test_describe_error_distinguishes_bridge_down(self):
        assert "桥不可达" in bridge.describe_error(ConnectionRefusedError())
        assert "调用失败" in bridge.describe_error(ValueError("参数错了"))


class TestSerialization:
    def test_concurrent_calls_never_overlap(self, monkeypatch):
        """桥是单队列：并发调用必须排队，否则会串话。"""
        state = {"active": 0, "peak": 0}
        guard = threading.Lock()

        def fake_urlopen(req, timeout=None):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.03)
            with guard:
                state["active"] -= 1
            return _Resp({"ok": True, "output": "x"})

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
        client = bridge.BridgeClient("http://h/browser")
        threads = [threading.Thread(target=lambda: client.call("status")) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert state["peak"] == 1, "并发调用没有被串行化"


class TestRetry:
    def test_transient_error_is_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionRefusedError(10061, "refused")
            return _Resp({"ok": True, "output": "ok"})

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
        client = bridge.BridgeClient("http://h/browser")
        assert client.call("status")["ok"] is True
        assert calls["n"] == 3

    def test_fatal_error_is_not_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise ValueError("bad payload")

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
        client = bridge.BridgeClient("http://h/browser")
        result = client.call("status")
        assert result["ok"] is False and calls["n"] == 1
        assert "调用失败" in result["error"]

    def test_bridge_reported_transient_error_is_retried(self, monkeypatch):
        """桥回 200 但内容说"连不上"（桥自己也没起来）—— 也算瞬态。"""
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _Resp({"ok": False, "error": "connection refused"})

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
        client = bridge.BridgeClient("http://h/browser")
        assert client.call("status")["ok"] is False
        assert calls["n"] == bridge.DEFAULT_RETRIES

    def test_bridge_reported_fatal_error_returns_immediately(self, monkeypatch):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            return _Resp({"ok": False, "error": "unknown action: fly"})

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
        client = bridge.BridgeClient("http://h/browser")
        assert client.call("fly")["error"] == "unknown action: fly"
        assert calls["n"] == 1


class TestProbeCache:
    def _patch(self, monkeypatch, state):
        def fake_urlopen(req, timeout=None):
            url = getattr(req, "full_url", str(req))
            if url.endswith("/health"):
                if not state["healthy"]:
                    raise ConnectionRefusedError(10061, "refused")
                return _Resp({"ok": True})
            return _Resp({"ok": True, "output": "x"})

        monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)

    def test_cache_expires(self, monkeypatch):
        state = {"healthy": True}
        self._patch(monkeypatch, state)
        client = bridge.BridgeClient("http://h/browser", probe_ttl=0.05)
        assert client.probe() is True
        state["healthy"] = False
        assert client.probe() is True, "TTL 内应复用上次结果"
        time.sleep(0.06)
        assert client.probe() is False, "TTL 过期后必须重新探测"

    def test_failed_call_invalidates_cache(self, monkeypatch):
        state = {"healthy": True}
        self._patch(monkeypatch, state)
        client = bridge.BridgeClient("http://h/browser", retries=1)
        assert client.probe() is True
        state["healthy"] = False
        monkeypatch.setattr(bridge.urllib.request, "urlopen",
                            lambda req, timeout=None: (_ for _ in ()).throw(
                                ConnectionRefusedError(10061, "refused")))
        client.call("status")
        state["healthy"] = True
        assert client.probe(use_cache=True) is False or client._probe_at == 0.0, \
            "调用失败后不能还拿旧缓存说'在线'"


class TestEmbeddedToolWiring:
    @pytest.fixture
    def tool(self, monkeypatch):
        monkeypatch.setattr("tools.embedded_browser.probe_bridge", lambda *a, **kw: False)
        return EmbeddedBrowserTool(bridge_url="http://h/browser")

    def test_call_bridge_goes_through_client(self, tool, monkeypatch):
        seen = {}

        def fake_call(action, **params):
            seen["action"], seen["params"] = action, params
            return {"ok": True, "output": "done"}

        monkeypatch.setattr(tool._client, "call", fake_call)
        assert tool._call_bridge("click", selector="#a")["output"] == "done"
        assert seen == {"action": "click", "params": {"selector": "#a"}}

    def test_bridge_error_becomes_tool_error(self, tool):
        result = tool._to_result({"ok": False, "error": "内嵌浏览器桥暂时不可达"})
        assert result.success is False and "不可达" in result.error
