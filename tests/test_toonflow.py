"""
Toonflow 对接的离线测试（httpx 打桩，不联网、不需要真的装 Toonflow）。

契约来自上游源码核实（2026-09-17）：
- 登录: POST /api/login/login {username,password} → {"data":{"token":"Bearer <jwt>"}}
- 其余: Authorization: <token>（token 本身已含 "Bearer " 前缀）
- 路径: 就是 router.ts 里 app.use 的原样路径
"""
import json
import sys
import types

import pytest

from models.toonflow import ToonflowClient, ToonflowError
from tools.toonflow import ToonflowTool, _split_chapters


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeHttpx:
    """记录请求并按脚本返回响应。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None,
                **kwargs):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers or {}, **kwargs})
        return self.handler(method, url, params, json, headers or {})


@pytest.fixture
def fake_http(monkeypatch):
    def install(handler):
        fake = FakeHttpx(handler)
        mod = types.ModuleType("httpx")
        mod.request = fake.request
        mod.HTTPError = Exception
        monkeypatch.setitem(sys.modules, "httpx", mod)
        return fake
    return install


def make_client(**kw):
    kw.setdefault("base_url", "http://127.0.0.1:10588")
    return ToonflowClient(**kw)


# ----------------------------------------------------------------------
# 登录与鉴权
# ----------------------------------------------------------------------

class TestAuth:
    def test_login_returns_token_and_is_cached(self, fake_http):
        def handler(method, url, params, body, headers):
            if url.endswith("/api/login/login"):
                return FakeResponse({"data": {"token": "Bearer JWT123", "name": "admin"}})
            return FakeResponse({"data": "ok"})

        fake = fake_http(handler)
        c = make_client()
        assert c.login() == "Bearer JWT123"
        c.login()                                    # 第二次应命中缓存
        logins = [x for x in fake.calls if "login" in x["url"]]
        assert len(logins) == 1, "重复登录应走缓存"

    def test_token_sent_in_authorization_header(self, fake_http):
        def handler(method, url, params, body, headers):
            if "login" in url:
                return FakeResponse({"data": {"token": "Bearer JWT123"}})
            return FakeResponse({"data": []})

        fake = fake_http(handler)
        make_client().request("GET", "/api/project/getProject")
        call = fake.calls[-1]
        assert call["headers"]["Authorization"] == "Bearer JWT123"

    def test_login_failure_message_is_actionable(self, fake_http):
        def handler(method, url, params, body, headers):
            return FakeResponse({"msg": "用户名或密码错误"}, status_code=400)

        fake_http(handler)
        with pytest.raises(ToonflowError) as e:
            make_client().login()
        assert "400" in str(e.value)

    def test_401_triggers_relogin_once(self, fake_http):
        state = {"logins": 0, "protected": 0}

        def handler(method, url, params, body, headers):
            if "login" in url:
                state["logins"] += 1
                return FakeResponse({"data": {"token": f"Bearer T{state['logins']}"}})
            state["protected"] += 1
            if state["protected"] == 1:
                return FakeResponse({"msg": "token 失效"}, status_code=401)
            return FakeResponse({"data": "ok"})

        fake = fake_http(handler)
        c = make_client()
        data = c.request("GET", "/api/project/getProject")
        assert data == {"data": "ok"}
        assert state["logins"] == 2, "401 后应重新登录一次"
        assert fake.calls[-1]["headers"]["Authorization"] == "Bearer T2"

    def test_connection_error_explains_how_to_start(self, fake_http):
        def handler(*a, **kw):
            raise ConnectionError("Connection refused")

        fake_http(handler)
        with pytest.raises(ToonflowError) as e:
            make_client().login()
        msg = str(e.value)
        assert "10588" in msg and "启动" in msg, "要告诉用户怎么把服务跑起来"

    def test_loopback_bypasses_env_proxy(self, fake_http):
        """回归：本机服务不得走环境代理。

        实测：Toonflow 没启动时，httpx 读环境代理配置会返回**莫名其妙的 HTTP 502**
        （本该是"连接被拒绝"），把排查方向带偏。回环地址必须 trust_env=False。
        """
        from models.toonflow import _is_loopback

        fake = fake_http(lambda *a, **kw: FakeResponse({"data": {"token": "Bearer T"}}))
        make_client(base_url="http://127.0.0.1:10588").login()
        assert fake.calls[-1]["trust_env"] is False

        fake2 = fake_http(lambda *a, **kw: FakeResponse({"data": {"token": "Bearer T"}}))
        make_client(base_url="https://toonflow.example.com").login()
        assert fake2.calls[-1]["trust_env"] is True, "远端部署仍应允许走代理"

        assert _is_loopback("http://localhost:10588") is True
        assert _is_loopback("http://10.0.0.5:10588") is False


# ----------------------------------------------------------------------
# 错误翻译
# ----------------------------------------------------------------------

class TestErrorMessages:
    def _client_with(self, fake_http, resp):
        def handler(method, url, params, body, headers):
            if "login" in url:
                return FakeResponse({"data": {"token": "Bearer T"}})
            return resp
        fake_http(handler)
        return make_client()

    def test_400_surfaces_field_details(self, fake_http):
        c = self._client_with(fake_http, FakeResponse(
            {"msg": "参数校验失败", "errors": ["projectType 必填"]}, status_code=400))
        with pytest.raises(ToonflowError) as e:
            c.request("POST", "/api/project/addProject", json_body={})
        msg = str(e.value)
        assert "projectType" in msg and "补齐" in msg

    def test_404_points_to_routes_command(self, fake_http):
        c = self._client_with(fake_http, FakeResponse({"msg": "not found"}, status_code=404))
        with pytest.raises(ToonflowError) as e:
            c.request("GET", "/api/nope")
        assert "routes" in str(e.value)

    def test_500_surfaces_upstream_reason(self, fake_http):
        """500 要把上游真实原因放前面，并给对号入座的提示（密钥 / 队列 / 或都不像）。"""
        # 密钥类
        c = self._client_with(fake_http, FakeResponse({"message": "无效的令牌"}, status_code=500))
        with pytest.raises(ToonflowError) as e:
            c.request("POST", "/api/project/getProject")
        assert "无效的令牌" in str(e.value) and "密钥" in str(e.value)

    def test_500_queue_full_is_called_transient(self, fake_http):
        c = self._client_with(fake_http, FakeResponse(
            {"message": "video_queue_full 视频队列已满"}, status_code=500))
        with pytest.raises(ToonflowError) as e:
            c.request("POST", "/api/production/workbench/generateVideo")
        assert "瞬时" in str(e.value), "队列满是瞬时状态，应提示重试而不是改配置"

    def test_500_without_hint_still_shows_detail(self, fake_http):
        c = self._client_with(fake_http, FakeResponse({"msg": "boom"}, status_code=500))
        with pytest.raises(ToonflowError) as e:
            c.request("POST", "/api/project/getProject")
        assert "boom" in str(e.value)


# ----------------------------------------------------------------------
# 工具命令
# ----------------------------------------------------------------------

def tool_with(handler, fake_http):
    fake = fake_http(handler)
    client = make_client()
    return ToonflowTool(client=client), fake


def ok_handler(routes=None):
    routes = routes or {}

    def handler(method, url, params, body, headers):
        if "login" in url:
            return FakeResponse({"data": {"token": "Bearer T", "name": "admin"}})
        for path, payload in routes.items():
            if url.endswith(path):
                return FakeResponse(payload)
        return FakeResponse({"data": []})
    return handler


class TestToolCommands:
    def test_health_reports_version(self, fake_http):
        t, _ = tool_with(ok_handler({"/api/other/getVersion": {"data": "1.0.8"}}), fake_http)
        r = t.execute_json({"command": "health"})
        assert r.success is True
        assert "1.0.8" in r.output and "已连通" in r.output

    def test_health_failure_gives_checklist(self, fake_http):
        def handler(*a, **kw):
            raise ConnectionError("refused")
        t, _ = tool_with(handler, fake_http)
        r = t.execute_json({"command": "health"})
        assert r.success is False and "排查" in r.error

    def test_projects_list(self, fake_http):
        t, fake = tool_with(ok_handler(
            {"/api/project/getProject": {"data": [{"id": 1, "name": "第七次葬礼"}]}}), fake_http)
        r = t.execute_json({"command": "projects"})
        assert r.success and "第七次葬礼" in r.output

    def test_create_project_sends_all_zod_fields(self, fake_http):
        t, fake = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "create_project", "name": "新剧",
                            "art_style": "3", "image_model": "img-1",
                            "video_model": "vid-1", "video_ratio": "9:16"})
        assert r.success, r.error
        body = [c for c in fake.calls if c["url"].endswith("/api/project/addProject")][-1]["json"]
        # 字段名必须与上游 zod 完全一致（缺一个就是 400）
        assert set(body) == {"projectType", "name", "intro", "type", "artStyle",
                             "directorManual", "videoRatio", "imageModel",
                             "videoModel", "imageQuality", "mode"}
        assert body["name"] == "新剧" and body["artStyle"] == "3"
        assert all(isinstance(v, str) for v in body.values()), "zod 要求全为字符串"

    def test_create_project_requires_name(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "create_project"})
        assert r.success is False and "name" in r.error

    def test_add_novel_from_text_splits_chapters(self, fake_http):
        t, fake = tool_with(ok_handler(), fake_http)
        text = "第一章 雨夜\n他关上了花店的门。\n\n第二章 葬礼\n她走了。"
        r = t.execute_json({"command": "add_novel", "project_id": 7, "text": text})
        assert r.success, r.error
        body = [c for c in fake.calls if c["url"].endswith("/api/novel/addNovel")][-1]["json"]
        assert body["projectId"] == 7
        assert [d["chapter"] for d in body["data"]] == ["第一章 雨夜", "第二章 葬礼"]
        assert "关上了花店的门" in body["data"][0]["chapterData"]

    def test_add_novel_requires_project_id(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "add_novel", "text": "x"})
        assert r.success is False and "project_id" in r.error

    def test_add_novel_accepts_explicit_data(self, fake_http):
        t, fake = tool_with(ok_handler(), fake_http)
        data = [{"index": 1, "reel": "", "chapter": "一", "chapterData": "内容"}]
        r = t.execute_json({"command": "add_novel", "project_id": 3, "data": data})
        assert r.success
        body = [c for c in fake.calls if c["url"].endswith("/api/novel/addNovel")][-1]["json"]
        assert body["data"] == data

    def test_generic_call_get_with_query(self, fake_http):
        t, fake = tool_with(ok_handler(
            {"/api/production/workbench/getVideoList": {"data": [{"url": "a.mp4"}]}}), fake_http)
        r = t.execute_json({"command": "call", "method": "GET",
                            "path": "/api/production/workbench/getVideoList",
                            "query": {"projectId": 5}})
        assert r.success and "a.mp4" in r.output
        assert fake.calls[-1]["params"] == {"projectId": 5}

    def test_generic_call_post_body(self, fake_http):
        t, fake = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "call", "method": "POST",
                            "path": "/api/production/storyboard/batchGenerateImage",
                            "body": {"projectId": 1}})
        assert r.success
        assert fake.calls[-1]["json"] == {"projectId": 1}

    def test_call_requires_path(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "call"})
        assert r.success is False and "path" in r.error

    def test_unknown_command_lists_options(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        r = t.execute_json({"command": "fly"})
        assert r.success is False and "health" in r.error

    def test_long_response_is_truncated(self, fake_http, monkeypatch):
        t, _ = tool_with(ok_handler({"/api/project/getProject":
                                     {"data": ["x" * 50000]}}), fake_http)
        t._get_client().max_chars = 500
        r = t.execute_json({"command": "projects"})
        assert "已截断" in r.output and len(r.output) < 2000

    def test_string_entry(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        assert t.execute("health").success is True
        assert t.execute("projects").success is True

    def test_approval_metadata(self, fake_http):
        t, _ = tool_with(ok_handler(), fake_http)
        assert t.build_approval_request({"command": "projects"}).risk_level == "low"
        assert t.build_approval_request(
            {"command": "create_project"}).risk_level == "medium"
        assert t.build_approval_request(
            {"command": "call", "method": "POST"}).risk_level == "medium"
        assert t.build_approval_request(
            {"command": "call", "method": "GET"}).risk_level == "low"

    def test_registered_in_tool_manager(self):
        from tools.tool_manager import ToolManager
        assert "toonflow" in ToolManager().list_tools()


class TestChapterSplit:
    def test_splits_on_chinese_titles(self):
        ch = _split_chapters("第一章 甲\na\n第二章 乙\nb")
        assert [c["title"] for c in ch] == ["第一章 甲", "第二章 乙"]

    def test_splits_on_markdown_headings(self):
        ch = _split_chapters("# 开场\n内容\n## 高潮\n更多")
        assert len(ch) == 2 and ch[1]["title"] == "高潮"

    def test_no_titles_still_produces_one_chapter(self):
        ch = _split_chapters("就是一段没有标题的文字")
        assert len(ch) == 1 and ch[0]["body"] == "就是一段没有标题的文字"

    def test_long_body_is_hard_split(self):
        ch = _split_chapters("第一章 长\n" + "字" * 9000, max_chars=4000)
        assert len(ch) >= 3
        assert all(len(c["body"]) <= 4000 for c in ch)
