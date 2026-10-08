"""浏览器实时画面：限帧/只留最新帧的逻辑、工具侧抓帧器、dashboard 的 SSE 端点。"""
import json
import threading
import time

import pytest

from tools import screencast
from tools.base import ToolResult
from tools.browser import BrowserTool
from tools.screencast import FrameStore, FrameThrottle, LiveStream


class TestFrameThrottle:
    def test_first_frame_allowed_then_throttled(self):
        throttle = FrameThrottle(min_gap_ms=1000)
        assert throttle.allow(now=100.0) is True
        assert throttle.allow(now=100.5) is False
        assert throttle.allow(now=101.5) is True
        assert (throttle.emitted, throttle.dropped) == (2, 1)

    def test_zero_gap_allows_everything(self):
        throttle = FrameThrottle(min_gap_ms=0)
        assert throttle.allow(now=1.0) and throttle.allow(now=1.0)
        assert throttle.dropped == 0


class TestFrameStore:
    def test_keeps_only_latest(self):
        store = FrameStore()
        store.put("a")
        seq = store.put("b")
        assert seq == 2
        assert store.latest()["data"] == "b"

    def test_wait_next_returns_newer_frame(self):
        store = FrameStore()
        store.put("a")
        threading.Timer(0.1, lambda: store.put("b")).start()
        frame = store.wait_next(1, timeout=1.0)
        assert frame and frame["data"] == "b"

    def test_wait_next_times_out_on_same_seq(self):
        store = FrameStore()
        store.put("a")
        assert store.wait_next(1, timeout=0.15) is None


class TestLiveStream:
    def _stream(self, min_gap_ms=0):
        return LiveStream(min_gap_ms=min_gap_ms, store=FrameStore(),
                          throttle=FrameThrottle(min_gap_ms))

    def test_no_viewer_means_no_capture(self):
        calls = []
        stream = self._stream()
        stream.set_capturer(lambda: calls.append(1) or "frame")
        stream.capture_now()
        assert calls == [] and stream.store.latest() is None

    def test_viewer_triggers_capture(self):
        calls = []
        stream = self._stream()
        stream.set_capturer(lambda: calls.append(1) or "frame")
        stream.acquire()
        frame = stream.capture_now()
        assert calls == [1] and frame["data"] == "frame"
        assert stream.stats()["viewers"] == 1

    def test_capture_error_keeps_last_frame(self):
        stream = self._stream()

        def _boom():
            raise RuntimeError("浏览器炸了")

        stream.set_capturer(_boom)
        stream.acquire()
        assert stream.capture_now() is None          # 不抛，返回已有最新帧
        stream.set_capturer(lambda: "ok")
        stream.capture_now()
        stream.set_capturer(_boom)
        assert stream.capture_now()["data"] == "ok"

    def test_throttle_limits_capture_rate(self):
        calls = []
        stream = self._stream(min_gap_ms=60_000)
        stream.set_capturer(lambda: calls.append(1) or "frame")
        stream.acquire()
        stream.capture_now()
        stream.capture_now()
        assert calls == [1], "最小间隔内的第二次不该再抓"
        assert stream.stats()["dropped"] == 1

    def test_release_stops_capture(self):
        calls = []
        stream = self._stream()
        stream.set_capturer(lambda: calls.append(1) or "frame")
        stream.acquire()
        stream.release()
        stream.capture_now()
        assert calls == [] and stream.viewers() == 0


class TestBrowserLiveCommand:
    def _tool(self, monkeypatch, alive=True):
        tool = BrowserTool.__new__(BrowserTool)
        monkeypatch.setattr(BrowserTool, "_is_browser_alive", lambda self: alive)
        monkeypatch.setattr(BrowserTool, "_page_alive", lambda self: alive)
        return tool

    @pytest.fixture(autouse=True)
    def _reset_active(self):
        screencast.ACTIVE.set_capturer(None)
        screencast.ACTIVE.store._frame = None
        screencast.ACTIVE.store._seq = 0
        screencast.ACTIVE.throttle.emitted = screencast.ACTIVE.throttle.dropped = 0
        screencast.ACTIVE.throttle._last = 0.0
        while screencast.ACTIVE.viewers():
            screencast.ACTIVE.release()
        yield
        screencast.ACTIVE.set_capturer(None)

    def test_on_off_and_status(self, monkeypatch):
        tool = self._tool(monkeypatch)
        assert "已开启" in tool._live("on").output
        assert screencast.ACTIVE.capture_now is not None
        assert "状态" in tool._live("").output
        assert "已关闭" in tool._live("off").output

    def test_unknown_mode(self, monkeypatch):
        tool = self._tool(monkeypatch)
        assert not tool._live("fly").success

    def test_capture_skips_when_browser_not_running(self, monkeypatch):
        tool = self._tool(monkeypatch, alive=False)
        called = []
        monkeypatch.setattr(BrowserTool, "execute",
                            lambda self, s: called.append(s) or ToolResult(success=True, output=""))
        assert tool._capture_frame_for_live() == ""
        assert called == [], "浏览器没跑时不该触发任何命令"

    def test_capture_reads_metadata_payload(self, monkeypatch):
        tool = self._tool(monkeypatch)
        monkeypatch.setattr(
            BrowserTool, "execute",
            lambda self, s: ToolResult(success=True, output="截图",
                                       metadata={"screenshot_base64": "QUJD"}))
        assert tool._capture_frame_for_live() == "QUJD"


class TestDashboardEndpoint:
    """直接驱动端点函数：无限 SSE 不适合走完整 TestClient（lifespan 会起调度器）。"""

    def _endpoint(self):
        from dashboard.server import app
        for route in app.routes:
            if getattr(route, "path", "") == "/api/browser-live":
                return route.endpoint
        return None

    def test_route_registered(self):
        assert self._endpoint() is not None

    def test_stream_pushes_latest_frame_and_releases_viewer(self, monkeypatch):
        import asyncio

        endpoint = self._endpoint()

        class _Request:
            async def is_disconnected(self):
                return False

        screencast.ACTIVE.set_capturer(lambda: "FRAMEDATA")

        async def _first_event():
            response = await endpoint(_Request())
            assert response.media_type == "text/event-stream"
            gen = response.body_iterator
            try:
                return await gen.__anext__()
            finally:
                await gen.aclose()

        line = asyncio.run(_first_event())
        payload = json.loads(line.split("data: ", 1)[1])
        assert payload["data"] == "FRAMEDATA"
        assert payload["format"] == "jpeg" and payload["seq"] == 1
        assert screencast.ACTIVE.viewers() == 0, "客户端断开后观看者必须归零"
        screencast.ACTIVE.set_capturer(None)
