"""QQ 桥的实时画面与截图（A+B）：链接回执、跨进程推帧、截图发到 QQ、面板取图端点。"""
import asyncio
import threading
import time

import pytest

from agent.qqbot import QQBotBridge, QQBotConfig


@pytest.fixture(autouse=True)
def _keep_an_event_loop():
    """asyncio.run() 会关掉线程的 loop；botpy.Client 之后要靠 get_event_loop() 建实例。

    本文件跑完必须把 loop 还回去，否则后面的测试（例如他们的 _build_client 用例）
    会报 "There is no current event loop in thread 'MainThread'"。
    """
    yield
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _cfg(**kw):
    base = {"app_id": "1", "app_secret": "s", "allow_from": "me"}
    base.update(kw)
    return QQBotConfig(base)


class _FakeAPI:
    """假 botpy API：记录发出去的消息与上传请求。"""

    def __init__(self, base64_ok=True, url_ok=True):
        self.sent = []
        self.uploads = []
        self.base64_ok = base64_ok
        self.url_ok = url_ok
        self._http = self

    async def request(self, route, json=None):
        payload = dict(json or {})
        self.uploads.append(payload)
        ok = self.base64_ok if "file_data" in payload else self.url_ok
        if not ok:
            raise RuntimeError("平台拒绝了这个参数")
        return {"file_id": "x", "file_info": "info", "ttl": 60}

    async def post_c2c_message(self, **kw):
        self.sent.append(("c2c", kw))

    async def post_group_message(self, **kw):
        self.sent.append(("group", kw))


class _FakeSession:
    def __init__(self, data="ZmFrZQ==", fmt="png"):
        self.agent = None
        self._data = data
        self._fmt = fmt

    def screenshot(self):
        return self._data, self._fmt


def _bridge_with_api(api, **cfg_kw):
    b = QQBotBridge(_cfg(**cfg_kw))
    b._api = api
    b._loop = None
    b._ctx["c:me"] = {"api": api, "msg_id": "M1", "loop": None, "recv_at": time.time()}
    b.sessions["c:me"] = _FakeSession()
    return b


class TestConfig:
    def test_frame_url_derived_from_panel_and_token(self):
        cfg = _cfg(dashboard_url="http://1.2.3.4:8080/", dashboard_token="tok")
        assert cfg.frame_url() == "http://1.2.3.4:8080/api/frame.png?token=tok"

    def test_frame_url_empty_without_token(self):
        assert _cfg(dashboard_url="http://x").frame_url() == ""
        assert _cfg(dashboard_token="tok").frame_url() == ""

    def test_live_link_prefers_explicit_url(self):
        assert _cfg(dashboard_url="http://a", live_url="http://b").live_link() == "http://b"
        assert _cfg(dashboard_url="http://a").live_link() == "http://a"
        assert _cfg().live_link() == ""

    def test_send_frame_flag_parsing(self):
        assert _cfg().send_frame is True
        assert _cfg(send_frame="false").send_frame is False
        assert _cfg(send_frame=False).send_frame is False
        assert _cfg(send_frame="0").send_frame is False

    def test_push_interval_has_a_floor(self):
        assert _cfg(frame_push_seconds="0.01").push_seconds >= 0.5

    def test_ack_carries_the_live_link(self):
        b = QQBotBridge(_cfg(dashboard_url="http://1.2.3.4:8080"))
        assert "http://1.2.3.4:8080" in b.ack_text()
        assert "已收到" in b.ack_text()
        assert "实时画面" not in QQBotBridge(_cfg()).ack_text(), "没配链接就别写这一行"


class TestScreenshot:
    def test_screenshot_returns_empty_without_agent(self):
        from agent.qqbot import UserSession
        assert UserSession("c:me").screenshot() == ("", "png")

    def test_screenshot_reads_browser_tool_metadata(self):
        from agent.qqbot import UserSession

        class _Tool:
            def execute(self, cmd):
                assert cmd == "screenshot_base64"
                return type("R", (), {"success": True,
                                      "metadata": {"screenshot_base64": "AAA"}})()

        class _TM:
            def get_tool(self, name):
                return _Tool() if name == "browser" else None

        session = UserSession("c:me")
        session.agent = type("A", (), {"tool_manager": _TM()})()
        assert session.screenshot() == ("AAA", "png")


class TestSendFrame:
    def _run(self, bridge):
        result = asyncio.run(bridge.send_frame("c:me"))
        asyncio.set_event_loop(asyncio.new_event_loop())   # 别把线程的 loop 留空（botpy 要用）
        return result

    def test_base64_path_sends_media_message(self):
        api = _FakeAPI()
        b = _bridge_with_api(api)
        self._run(b)
        assert api.uploads and "file_data" in api.uploads[0], "先试 base64 直传"
        kind, kw = api.sent[-1]
        assert kind == "c2c" and kw.get("msg_type") == 7 and kw.get("media")

    def test_falls_back_to_public_url(self):
        api = _FakeAPI(base64_ok=False)
        b = _bridge_with_api(api, public_frame_url="https://cdn.example/x.png")
        self._run(b)
        assert any("url" in u for u in api.uploads), "base64 失败要退到 URL 通道"
        assert api.sent and api.sent[-1][1].get("msg_type") == 7

    def test_both_channels_failing_explains_why(self):
        api = _FakeAPI(base64_ok=False, url_ok=False)
        b = _bridge_with_api(api, public_frame_url="https://cdn.example/x.png")
        b.sent_plain = []
        async def _capture(key, text):
            b.sent_plain.append(text)
        b._send_plain = _capture
        self._run(b)
        assert b.sent_plain and "上传通道" in b.sent_plain[0]
        assert "base64 直传失败" in b.sent_plain[0]

    def test_no_frame_reports_plainly(self):
        api = _FakeAPI()
        b = _bridge_with_api(api)
        b.sessions["c:me"] = _FakeSession(data="")
        captured = []
        async def _capture(key, text):
            captured.append(text)
        b._send_plain = _capture
        self._run(b)
        assert captured and "没有可用画面" in captured[0]
        assert not api.sent, "没画面就不该发消息"

    def test_schedule_without_loop_is_silent(self):
        b = _bridge_with_api(_FakeAPI())
        b.schedule_frame("c:me")     # loop 为 None：不抛异常


class TestFramePush:
    def test_push_loop_sends_and_stops(self, monkeypatch):
        import httpx
        calls = []
        stop = threading.Event()

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, headers, json))
            stop.set()                          # 推一次就停，避免测试里空转
            return type("R", (), {"status_code": 200})()

        monkeypatch.setattr(httpx, "post", fake_post)
        b = QQBotBridge(_cfg(dashboard_url="http://panel:8080", dashboard_token="tok"))
        b.sessions["c:me"] = _FakeSession()
        b._push_loop("c:me", stop)
        assert calls and calls[0][0] == "http://panel:8080/api/frame"
        assert calls[0][1] == {"X-Frame-Token": "tok"}
        assert calls[0][2]["data"] == "ZmFrZQ==" and calls[0][2]["format"] == "png"

    def test_no_pusher_without_panel_config(self):
        assert QQBotBridge(_cfg())._start_frame_pusher("c:me") is None

    def test_pusher_stops_with_the_task(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: type("R", (), {"status_code": 200})())
        b = QQBotBridge(_cfg(dashboard_url="http://panel", dashboard_token="t",
                             frame_push_seconds="0.5"))
        b.sessions["c:me"] = _FakeSession()
        stop = b._start_frame_pusher("c:me")
        assert stop is not None
        time.sleep(0.1)
        stop.set()
        assert stop.is_set()


class TestSlashShot:
    def test_shot_schedules_a_frame_and_replies_nothing(self):
        b = QQBotBridge(_cfg(runner=None))
        scheduled = []
        b.schedule_frame = lambda key: scheduled.append(key)
        assert b.handle_text("me", "/shot") == ""
        assert scheduled == ["c:me"]

    def test_help_mentions_shot(self):
        b = QQBotBridge(_cfg())
        assert "/shot" in b.handle_text("me", "/help")


class TestPanelEndpoints:
    """面板侧：收外部推帧 + token 保护的取图端点。"""

    @pytest.fixture
    def client(self, monkeypatch):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        import dashboard.server as srv
        from tools import screencast
        monkeypatch.setattr(srv, "_FRAME_TOKEN", "secret-token", raising=False)
        screencast.ACTIVE.store._frame = None
        return fastapi_testclient.TestClient(srv.app)

    def test_push_requires_token(self, client):
        assert client.post("/api/frame", json={"data": "AAA"}).status_code == 403
        assert client.post("/api/frame", json={"data": "AAA"},
                           headers={"X-Frame-Token": "wrong"}).status_code == 403

    def test_push_then_fetch_returns_image_bytes(self, client):
        payload = {"data": "ZmFrZWpwZWc=", "format": "jpeg"}
        r = client.post("/api/frame", json=payload, headers={"X-Frame-Token": "secret-token"})
        assert r.status_code == 200 and r.json()["ok"] is True
        img = client.get("/api/frame.png?token=secret-token")
        assert img.status_code == 200
        assert img.headers["content-type"].startswith("image/")
        assert img.content == b"fakejpeg"

    def test_fetch_needs_token(self, client):
        client.post("/api/frame", json={"data": "ZmFrZWpwZWc="},
                    headers={"X-Frame-Token": "secret-token"})
        assert client.get("/api/frame.png").status_code == 403
        assert client.get("/api/frame.png?token=nope").status_code == 403

    def test_fetch_without_frame_is_404(self, client):
        assert client.get("/api/frame.png?token=secret-token").status_code == 404

    def test_push_rejects_empty_payload(self, client):
        r = client.post("/api/frame", json={"data": ""},
                        headers={"X-Frame-Token": "secret-token"})
        assert r.status_code == 400

    def test_probe_media_explains_the_channels(self):
        b = QQBotBridge(_cfg(public_frame_url="https://cdn/x.png"))
        text = b.probe_media()
        assert "base64" in text and "https://cdn/x.png" in text
