# -*- coding: utf-8 -*-
"""知乎数据开放平台对接的离线测试（httpx 打桩，不联网）。

契约来自官方文档逐接口核实（2026-09-18）：
- 鉴权: Authorization: Bearer <secret> + X-Request-Timestamp(秒级 Unix 时间戳)
- 信封: {"Code":0,"Message":"success","Data":{...}}；Code!=0 时原因在 Message
- 特例: 直答 /v1/chat/completions 是 OpenAI 兼容格式（无信封，且路径不带 /api）
- 参数名是 PascalCase，工具侧 snake_case，映射在 models/zhihu.py
"""
import json
import sys
import types

import pytest

from models.zhihu import (ENDPOINTS, ERROR_HINTS, ZHIDA_MODELS, ZhihuClient,
                          ZhihuError, mask_secret)
from tools.zhihu import ZhihuTool


# ----------------------------------------------------------------------
# 打桩
# ----------------------------------------------------------------------

class FakeHTTPError(Exception):
    pass


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHTTPError(f"HTTP {self.status_code}")


class FakeStream:
    """httpx.stream(...) 的上下文管理器替身（把状态码等委托给内部响应）。"""

    def __init__(self, response, lines=None, chunks=None):
        self._response = response
        self._lines = lines or []
        self._chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def status_code(self):
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    @property
    def text(self):
        return self._response.text

    def json(self):
        return self._response.json()

    def raise_for_status(self):
        return self._response.raise_for_status()

    def iter_lines(self):
        return iter(self._lines)

    def iter_bytes(self, size=0):
        return iter(self._chunks)

    def read(self):
        return b""


class FakeHttpx:
    def __init__(self, handler, stream_handler=None):
        self.handler = handler
        self.stream_handler = stream_handler
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None,
                files=None, data=None, **kwargs):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers or {}, "files": files,
                           "data": data, "timeout": timeout})
        return self.handler(method, url, params, json, headers or {}, files, data)

    def stream(self, method, url, json=None, headers=None, timeout=None,
               follow_redirects=None, **kwargs):
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}, "stream": True})
        if self.stream_handler is None:
            raise AssertionError("本用例未预期流式/下载请求")
        return self.stream_handler(method, url, json, headers or {})


@pytest.fixture
def fake_http(monkeypatch):
    """安装假的 httpx 模块；返回 (install, ...) 形式的工厂。"""
    def install(handler, stream_handler=None):
        fake = FakeHttpx(handler, stream_handler)
        mod = types.ModuleType("httpx")
        mod.request = fake.request
        mod.stream = fake.stream
        mod.HTTPError = FakeHTTPError
        monkeypatch.setitem(sys.modules, "httpx", mod)
        return fake
    return install


def ok(data):
    return FakeResponse({"Code": 0, "Message": "success", "Data": data})


def err(code, message="boom"):
    return FakeResponse({"Code": code, "Message": message, "Data": None})


def make_client(**kw):
    kw.setdefault("access_secret", "S" * 40)
    kw.setdefault("base_url", "https://developer.zhihu.com")
    return ZhihuClient(**kw)


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------

class TestAuth:
    def test_bearer_and_timestamp_headers(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        c = make_client()
        c.hot(5)
        headers = fake.calls[-1]["headers"]
        assert headers["Authorization"] == "Bearer " + "S" * 40
        assert headers["X-Request-Timestamp"].isdigit()
        assert len(headers["X-Request-Timestamp"]) == 10, "必须是秒级时间戳"

    def test_missing_secret_is_actionable_and_has_no_network(self, fake_http):
        fake = fake_http(lambda *a: ok({}))
        c = make_client(access_secret="")
        assert c.configured is False
        with pytest.raises(ZhihuError) as e:
            c.hot(5)
        assert "ZHIHU_ACCESS_SECRET" in str(e.value)
        assert fake.calls == [], "未配置时不应发请求"

    def test_secret_never_leaks_in_error(self, fake_http):
        secret = "SECRET_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        fake_http(lambda *a: err(30001, "频率限制"))
        c = make_client(access_secret=secret)
        with pytest.raises(ZhihuError) as e:
            c.hot(5)
        assert secret not in str(e.value)

    @pytest.mark.parametrize("secret,expect", [
        ("", ""),
        ("short", "*****"),
        ("A" * 40, "AAAAAA…AAAA（共 40 位）"),
    ])
    def test_mask_secret(self, secret, expect):
        assert mask_secret(secret) == expect


# ----------------------------------------------------------------------
# 信封解包与错误翻译
# ----------------------------------------------------------------------

class TestEnvelope:
    def test_unwraps_data(self, fake_http):
        fake_http(lambda *a: ok({"Total": 1, "Items": [{"Title": "x"}]}))
        assert make_client().hot(1) == {"Total": 1, "Items": [{"Title": "x"}]}

    @pytest.mark.parametrize("code", [10001, 20001, 30001, 30002, 30003, 40003, 40004,
                                      40005, 40006, 50002, 90001])
    def test_every_documented_code_has_a_hint(self, code, fake_http):
        fake_http(lambda *a: err(code))
        assert code in ERROR_HINTS, f"错误码 {code} 缺少可行动说明"
        with pytest.raises(ZhihuError) as e:
            make_client().hot(1)
        assert str(code) in str(e.value)
        assert ERROR_HINTS[code][:6] in str(e.value)

    def test_rate_limit_message_mentions_quota(self, fake_http):
        fake_http(lambda *a: err(30001))
        with pytest.raises(ZhihuError) as e:
            make_client().search("x")
        assert "quota" in str(e.value)

    def test_404_and_429_are_explained(self, fake_http):
        fake_http(lambda *a: FakeResponse({"detail": "nope"}, status_code=404))
        with pytest.raises(ZhihuError) as e:
            make_client().hot(1)
        assert "routes" in str(e.value), "404 应引导去看接口清单"

        fake_http(lambda *a: FakeResponse({}, status_code=429))
        with pytest.raises(ZhihuError) as e:
            make_client().hot(1)
        assert "限流" in str(e.value)

    def test_data_null_is_returned_as_none(self, fake_http):
        fake_http(lambda *a: FakeResponse({"Code": 0, "Message": "success", "Data": None}))
        assert make_client().hot(1) is None

    def test_empty_api_ids_sends_no_param(self, fake_http):
        fake = fake_http(lambda *a: ok([]))
        make_client().quota()
        assert fake.calls[-1]["params"] is None
        make_client().quota(["knowledge", "zhihu_search"])
        assert fake.calls[-1]["params"] == {"APIIDs": "knowledge,zhihu_search"}


# ----------------------------------------------------------------------
# 时钟偏差自愈
# ----------------------------------------------------------------------

class TestClockSkew:
    def test_20001_with_date_header_retries_with_corrected_timestamp(self, fake_http):
        future = "Fri, 18 Sep 2026 02:15:29 GMT"
        seen = []

        def handler(method, url, params, body, headers, files, data):
            seen.append(headers["X-Request-Timestamp"])
            if len(seen) == 1:
                return FakeResponse({"Code": 20001, "Message": "auth fail", "Data": None},
                                    headers={"date": future})
            return ok([{"APIID": "hot_list"}])

        fake_http(handler)
        c = make_client()
        assert c.hot(1) == [{"APIID": "hot_list"}]
        assert len(seen) == 2, "首次 20001 且拿到 Date 头时应重试一次"
        assert seen[0] != seen[1], "重试必须带上纠正后的时间戳"

    def test_20001_without_date_header_does_not_retry(self, fake_http):
        fake = fake_http(lambda *a: err(20001))
        with pytest.raises(ZhihuError) as e:
            make_client().hot(1)
        assert len(fake.calls) == 1
        assert "10 分钟" in str(e.value), "应提示时钟超窗这一常见原因"

    def test_skew_is_reported_by_health(self, fake_http):
        fake_http(lambda *a: FakeResponse(
            {"Code": 0, "Message": "success", "Data": [{"APIID": "hot_list"}]},
            headers={"date": "Fri, 18 Sep 2026 02:15:29 GMT"}))
        info = make_client().health()
        assert info["configured"] is True
        assert info["quota"] == [{"APIID": "hot_list"}]
        assert isinstance(info["clock_skew"], float)

    def test_health_never_raises(self, fake_http):
        def boom(*a):
            raise FakeHTTPError("dns fail")

        fake_http(boom)
        info = make_client().health()
        assert "failed" in str(info["quota"])

    def test_network_error_is_translated(self, fake_http):
        def boom(*a):
            raise FakeHTTPError("connection refused")

        fake_http(boom)
        with pytest.raises(ZhihuError) as e:
            make_client().hot(1)
        assert "连不上知乎开放平台" in str(e.value)


# ----------------------------------------------------------------------
# 参数映射与取值边界（文档给出的上下限）
# ----------------------------------------------------------------------

class TestParams:
    def test_search_maps_query_count_sortby_and_clamps(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        c = make_client()
        c.search("RAG 评测", 99, "VoteUpCount:desc:(10,)")
        assert fake.calls[-1]["params"] == {
            "Query": "RAG 评测", "Count": 10, "SortBy": "VoteUpCount:desc:(10,)"}

    def test_search_path(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().search("x")
        assert fake.calls[-1]["url"].endswith("/api/v1/content/zhihu_search")
        assert fake.calls[-1]["method"] == "GET"

    def test_web_search_clamps_to_20_and_keeps_filter(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().web_search("x", 50, 'host=="a.com"', "realtime")
        assert fake.calls[-1]["params"] == {
            "Query": "x", "Count": 20, "Filter": 'host=="a.com"', "SearchDB": "realtime"}

    def test_hot_clamps_to_30(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().hot(1000)
        assert fake.calls[-1]["params"] == {"Limit": 30}

    def test_answers_clamps_limit_to_50(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().answers("https://www.zhihu.com/question/1", -5, 500)
        assert fake.calls[-1]["params"] == {
            "QuestionUrl": "https://www.zhihu.com/question/1", "Offset": 0, "Limit": 50}

    def test_none_params_are_dropped_but_empty_strings_kept(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        c = make_client()
        c.web_search("x")                     # filter / search_db 都是 None
        assert fake.calls[-1]["params"] == {"Query": "x", "Count": 10}
        c.content_comments("u", order="")
        assert fake.calls[-1]["params"]["Order"] == "", "空串与缺省在上游含义不同，不能滤掉"

    def test_favlist_token_is_pascal_case(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().favlist_items(123456789, 0, 20)
        assert fake.calls[-1]["params"]["FavlistUrlToken"] == 123456789

    def test_kb_items_path_has_id_and_cursor(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().kb_items("7526139256098382426", "cursor-1", 20)
        call = fake.calls[-1]
        assert call["url"].endswith("/api/v1/knowledge/bases/7526139256098382426/items")
        assert call["params"] == {"Cursor": "cursor-1", "Limit": 20}

    def test_my_contents_maps_sort_fields(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().my_contents("answer", 20, 50, "like_count", "asc")
        assert fake.calls[-1]["params"] == {
            "ContentType": "answer", "Offset": 20, "Limit": 50,
            "SortField": "like_count", "SortOrder": "asc"}

    def test_kb_search_posts_json_body(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().kb_search("退款规则", ["kb1"], ["personal"], 5)
        call = fake.calls[-1]
        assert call["method"] == "POST"
        assert call["url"].endswith("/api/v1/knowledge/search")
        assert call["json"] == {"Query": "退款规则", "Limit": 5,
                                "KnowledgeBaseIDs": ["kb1"], "RecallScopes": ["personal"]}

    def test_kb_search_omits_empty_scope_keys(self, fake_http):
        fake = fake_http(lambda *a: ok({"Items": []}))
        make_client().kb_search("q", None, ["public"])
        assert fake.calls[-1]["json"] == {"Query": "q", "Limit": 10,
                                         "RecallScopes": ["public"]}


# ----------------------------------------------------------------------
# 鉴权作用于 multipart
# ----------------------------------------------------------------------

class TestUpload:
    def test_upload_file_uses_lowercase_field_and_strips_content_type(self, tmp_path,
                                                                     fake_http):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 hello")
        fake = fake_http(lambda *a: ok({"file_id": "file_123"}))
        assert make_client().upload_file(str(p)) == "file_123"
        call = fake.calls[-1]
        assert call["url"].endswith("/resources/v1/files")
        assert "file" in call["files"], "文档规定字段名是小写 file"
        assert "Content-Type" not in call["headers"], "multipart 不能自己带 Content-Type"
        assert call["headers"]["Authorization"].startswith("Bearer ")

    def test_kb_upload_uses_capital_field_and_optional_kb(self, tmp_path, fake_http):
        p = tmp_path / "产品资料.md"
        p.write_bytes(b"# hi")
        fake = fake_http(lambda *a: ok({"RecallContentID": "rc1"}))
        c = make_client()
        c.kb_upload(str(p))
        assert "File" in fake.calls[-1]["files"], "知识库上传字段名是大写 File"
        assert fake.calls[-1]["data"] is None, "不传 kb_id 时走默认知识库"
        c.kb_upload(str(p), "kb-1")
        assert fake.calls[-1]["data"] == {"KnowledgeBaseID": "kb-1"}

    def test_kb_upload_rejects_unsupported_extension(self, tmp_path, fake_http):
        p = tmp_path / "evil.exe"
        p.write_bytes(b"MZ")
        fake_http(lambda *a: ok({}))
        with pytest.raises(ZhihuError) as e:
            make_client().kb_upload(str(p))
        assert "不支持" in str(e.value)

    def test_missing_file_is_reported_before_request(self, fake_http):
        fake = fake_http(lambda *a: ok({}))
        with pytest.raises(ZhihuError) as e:
            make_client().kb_upload("D:/nope/missing.pdf")
        assert "文件不存在" in str(e.value)
        assert fake.calls == []


# ----------------------------------------------------------------------
# 直答（OpenAI 兼容，非信封）
# ----------------------------------------------------------------------

class TestZhida:
    def test_non_stream_parses_content_and_reasoning(self, fake_http):
        fake = fake_http(lambda *a: FakeResponse({
            "id": "chatcmpl-1", "model": "zhida-thinking-1p5",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "reasoning_content": "先分析…", "content": "结论"}}]}))
        res = make_client().zhida("RAG 怎么评测")
        assert res["content"] == "结论"
        assert res["reasoning"] == "先分析…"
        assert res["model"] == "zhida-thinking-1p5"
        call = fake.calls[-1]
        assert call["url"].endswith("/v1/chat/completions"), "直答路径不带 /api 前缀"
        assert call["method"] == "POST"
        assert call["json"]["messages"] == [{"role": "user", "content": "RAG 怎么评测"}]
        assert call["json"]["stream"] is False

    def test_openai_error_object_is_translated(self, fake_http):
        fake_http(lambda *a: FakeResponse(
            {"error": {"message": "no such model", "type": "invalid_request_error",
                       "code": "model_not_found"}}, status_code=400))
        with pytest.raises(ZhihuError) as e:
            make_client().zhida("x")
        assert "no such model" in str(e.value)

    def test_inline_error_in_body(self, fake_http):
        fake_http(lambda *a: FakeResponse({"error": {"message": "server error"}}))
        with pytest.raises(ZhihuError) as e:
            make_client().zhida("x")
        assert "server error" in str(e.value)

    def test_unknown_model_rejected_locally(self, fake_http):
        fake = fake_http(lambda *a: ok({}))
        with pytest.raises(ZhihuError) as e:
            make_client().zhida("x", model="gpt-4")
        assert "zhida-" in str(e.value)
        assert fake.calls == [], "本地就能挡住的参数错误不应发请求"

    def test_stream_accumulates_deltas(self, fake_http):
        lines = [
            'data: {"id":"c1","model":"zhida-thinking-1p5","choices":[{"index":0,'
            '"delta":{"role":"assistant","reasoning_content":"先分析"}}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"一"}}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"二"}}]}',
            'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        resp = FakeResponse({}, headers={})
        resp.headers = {}
        fake_http(lambda *a: ok({}),
                  stream_handler=lambda *a: FakeStream(resp, lines=lines))
        res = make_client().zhida("q", stream=True)
        assert res["content"] == "一二"
        assert res["reasoning"] == "先分析"

    def test_stream_error_chunk_raises(self, fake_http):
        lines = ['data: {"error":{"message":"boom"}}']
        fake_http(lambda *a: ok({}),
                  stream_handler=lambda *a: FakeStream(FakeResponse({}), lines=lines))
        with pytest.raises(ZhihuError):
            make_client().zhida("q", stream=True)

    def test_all_documented_models_accepted(self, fake_http):
        fake_http(lambda *a: FakeResponse({"choices": [{"message": {"content": "x"}}]}))
        for m in ZHIDA_MODELS:
            assert make_client().zhida("q", model=m)["content"] == "x"


# ----------------------------------------------------------------------
# 异步任务（PDF 解析 / PPT 生成）
# ----------------------------------------------------------------------

class TestTasks:
    def test_pdf_task_flow_uploads_then_polls(self, tmp_path, fake_http):
        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF")
        seq = []

        def handler(method, url, params, body, headers, files, data):
            seq.append((method, url, body))
            if url.endswith("/resources/v1/files"):
                return ok({"file_id": "file_1"})
            if url.endswith("/api/v1/pdf-parse/tasks"):
                return ok({"task_id": "pdf_1", "task_status": "pending"})
            return ok({"task_id": "pdf_1", "task_status": "succeeded", "progress": 1,
                       "result": {"url": "https://cdn/x.json", "summary": "摘要"},
                       "error": None})

        fake_http(handler)
        c = make_client(poll_interval=0)
        fid = c.upload_file(str(p))
        tid = c.create_pdf_task(fid)
        final = c.wait_task("pdf", tid)
        assert final["task_status"] == "succeeded"
        assert final["result"]["summary"] == "摘要"
        assert [s[1].rsplit("/", 1)[-1] for s in seq] == ["files", "tasks", "pdf_1"]

    def test_wait_task_returns_failed_status(self, fake_http):
        fake_http(lambda *a: ok({"task_id": "pdf_1", "task_status": "failed",
                                 "error": {"code": "parse_failed"}}))
        final = make_client(poll_interval=0).wait_task("pdf", "pdf_1")
        assert final["task_status"] == "failed"

    def test_wait_task_timeout_is_actionable(self, fake_http):
        fake_http(lambda *a: ok({"task_id": "pdf_1", "task_status": "running",
                                 "progress": 0.35}))
        with pytest.raises(ZhihuError) as e:
            make_client(poll_interval=0, task_timeout=0).wait_task("pdf", "pdf_1")
        msg = str(e.value)
        assert "超时" in msg and "0.35" in msg and "task" in msg

    def test_ppt_pages_bounds_checked_locally(self, fake_http):
        fake = fake_http(lambda *a: ok({}))
        c = make_client()
        for bad in (5, 22, 0):
            with pytest.raises(ZhihuError):
                c.create_ppt_task("https://www.zhihu.com/answer/1", bad)
        assert fake.calls == []
        assert c.create_ppt_task("https://www.zhihu.com/answer/1", 6) == ""  # 上游没给 id

    def test_task_status_paths(self, fake_http):
        fake = fake_http(lambda *a: ok({"task_status": "running"}))
        c = make_client()
        c.task_status("pdf", "pdf_1")
        assert fake.calls[-1]["url"].endswith("/api/v1/pdf-parse/tasks/pdf_1")
        c.task_status("ppt", "ppt_1")
        assert fake.calls[-1]["url"].endswith("/api/v1/ppt-generation/tasks/ppt_1")
        with pytest.raises(ZhihuError):
            c.task_status("docx", "x")


class TestDownload:
    def test_download_sanitizes_name_and_creates_dir(self, tmp_path, fake_http):
        resp = FakeResponse({}, headers={})
        fake_http(lambda *a: ok({}),
                  stream_handler=lambda *a: FakeStream(
                      resp, chunks=[b"chunk1", b"chunk2"]))
        c = make_client()
        c.save_dir = str(tmp_path / "out")
        dest = c.download("https://cdn/x/a b:c?.pptx")
        assert dest.startswith(str(tmp_path / "out"))
        import os
        assert os.path.isfile(dest)
        assert open(dest, "rb").read() == b"chunk1chunk2"
        assert "?" not in os.path.basename(dest) and ":" not in os.path.basename(dest)

    def test_download_error_is_translated(self, fake_http):
        resp = FakeResponse({}, status_code=403)
        fake_http(lambda *a: ok({}),
                  stream_handler=lambda *a: FakeStream(resp, chunks=[]))
        with pytest.raises(ZhihuError) as e:
            make_client().download("https://cdn/x.json")
        assert "下载产物失败" in str(e.value)


# ----------------------------------------------------------------------
# 工具层
# ----------------------------------------------------------------------

class ToolFake(ZhihuClient):
    """记录调用、返回预设值的假客户端（工具层只依赖这几个方法）。"""


ALL_COMMANDS = {"health", "routes", "quota", "search", "web_search", "hot", "answers",
                "zhida", "recommend", "my_contents", "content_detail",
                "content_comments", "account_stats", "content_stats", "followees",
                "collections", "favlists", "favlist_items", "kb_list", "kb_items",
                "kb_upload", "kb_search", "pdf_parse", "ppt", "task", "call"}


class TestToolSurface:
    def test_metadata(self):
        t = ZhihuTool()
        assert t.name == "zhihu"
        assert t.risk_level == "low"
        assert t.min_sandbox_mode == "read-only"

    def test_command_enum_matches_handlers(self):
        schema = ZhihuTool().schema
        assert set(schema["properties"]["command"]["enum"]) == ALL_COMMANDS
        assert schema["required"] == ["command"]

    def test_description_is_not_truncated_by_base(self):
        # base.to_openai_schema() 会截到 1024 字符，超了就等于悄悄丢说明
        assert len(ZhihuTool().description) <= 1024

    def test_description_mentions_quota_limits(self):
        d = ZhihuTool().description
        assert "5000" in d and "100" in d and "10" in d

    def test_routes_command_lists_every_endpoint(self):
        res = ZhihuTool().execute_json({"command": "routes"})
        assert res.success
        for name in ENDPOINTS:
            assert name in res.output, f"routes 清单缺少 {name}"

    def test_unknown_command(self):
        res = ZhihuTool().execute_json({"command": "nope"})
        assert res.success is False
        assert "未知命令" in res.error

    def test_write_commands_raise_sandbox_and_risk(self):
        t = ZhihuTool()
        for args in ({"command": "kb_upload", "path": "a.md"},
                     {"command": "pdf_parse", "path": "a.pdf"},
                     {"command": "ppt", "content_url": "u"}):
            req = t.build_approval_request(args)
            assert req.risk_level == "medium", args
            assert req.min_sandbox_mode == "workspace-write", args
        req = t.build_approval_request({"command": "search", "query": "x"})
        assert req.risk_level == "low"
        assert req.min_sandbox_mode == "read-only"

    def test_parallel_safe_only_for_reads(self):
        t = ZhihuTool()
        assert t.is_parallel_safe({"command": "search"}) is True
        assert t.is_parallel_safe({"command": "hot"}) is True
        for cmd in ("kb_upload", "pdf_parse", "ppt", "call"):
            assert t.is_parallel_safe({"command": cmd}) is False, cmd

    def test_string_entry_maps_positional_argument(self):
        calls = []

        class Rec(ZhihuClient):
            def search(self, query, count=10, sort_by=None):
                calls.append(("search", query))
                return {"Items": []}

            def hot(self, limit=30):
                calls.append(("hot", limit))
                return {"Items": []}

            def answers(self, url, offset=0, limit=20):
                calls.append(("answers", url))
                return {"Items": []}

            def kb_items(self, kb_id, cursor=None, limit=20):
                calls.append(("kb_items", kb_id))
                return {"Items": []}

            def health(self):
                calls.append(("health", None))
                return {"configured": False}

        t = ZhihuTool(client=Rec(access_secret="S" * 40))
        t.execute("search 什么是 RAG")
        t.execute("hot")
        t.execute("answers https://www.zhihu.com/question/1")
        t.execute("kb_items kb-9")
        t.execute("")                       # 空输入应落到 health 自检
        assert calls == [("search", "什么是 RAG"), ("hot", 30),
                         ("answers", "https://www.zhihu.com/question/1"),
                         ("kb_items", "kb-9"), ("health", None)]


class TestToolValidation:
    @pytest.mark.parametrize("args,field", [
        ({"command": "search"}, "query"),
        ({"command": "search", "query": "   "}, "query"),
        ({"command": "web_search"}, "query"),
        ({"command": "answers"}, "question_url"),
        ({"command": "content_detail"}, "content_url"),
        ({"command": "content_comments"}, "content_url"),
        ({"command": "content_stats"}, "content_url"),
        ({"command": "ppt"}, "content_url"),
        ({"command": "zhida"}, "question"),
        ({"command": "kb_items"}, "kb_id"),
        ({"command": "kb_upload"}, "path"),
        ({"command": "pdf_parse"}, "path"),
        ({"command": "favlist_items"}, "favlist_token"),
        ({"command": "task"}, "task_id"),
        ({"command": "call"}, "path"),
    ])
    def test_missing_required_argument(self, args, field):
        res = ZhihuTool().execute_json(args)
        assert res.success is False
        assert field in res.error

    def test_kb_search_needs_scope_or_ids(self):
        res = ZhihuTool().execute_json({"command": "kb_search", "query": "q"})
        assert res.success is False
        assert "kb_ids" in res.error and "scopes" in res.error

    def test_task_needs_kind(self):
        res = ZhihuTool().execute_json({"command": "task", "task_id": "pdf_1"})
        assert res.success is False
        assert "kind" in res.error

    def test_unconfigured_client_gives_setup_guide(self):
        res = ZhihuTool(client=ZhihuClient(access_secret="")).execute_json(
            {"command": "health"})
        assert res.success is False
        assert "ZHIHU_ACCESS_SECRET" in res.error
        assert "developer.zhihu.com" in res.error


class FixedClient(ZhihuClient):
    """按命令返回固定数据，用来验证格式化输出。"""

    def __init__(self, payloads):
        super().__init__(access_secret="S" * 40)
        self.payloads = payloads

    def search(self, query, count=10, sort_by=None):
        return self.payloads["search"]

    def web_search(self, query, count=10, filter=None, search_db=None):
        return self.payloads["search"]

    def hot(self, limit=30):
        return self.payloads["hot"]

    def answers(self, url, offset=0, limit=20):
        return self.payloads["answers"]

    def content_comments(self, url, offset=0, limit=20, order=None):
        return self.payloads["comments"]

    def my_contents(self, *a, **k):
        return self.payloads["contents"]

    def account_stats(self, *a, **k):
        return self.payloads["stats"]

    def kb_search(self, query, kb_ids=None, scopes=None, limit=10):
        return self.payloads["kb"]

    def quota(self, api_ids=None):
        return self.payloads["quota"]


@pytest.fixture
def tool():
    return ZhihuTool(client=FixedClient({
        "quota": [{"APIID": "hot_list", "APIName": "热榜", "TotalQuota": 100,
                   "TotalUsed": 3, "RemainingQuota": 97}],
        "search": {"HasMore": False, "Items": [
            {"Title": "RAG 评测方法综述", "ContentType": "Article",
             "ContentID": "123", "ContentText": "本文介绍 <em>RAGAS</em> …",
             "Url": "https://zhuanlan.zhihu.com/p/123", "CommentCount": 15,
             "VoteUpCount": 128, "AuthorName": "张三", "EditTime": 1710000000,
             "AuthorityLevel": "2",
             "CommentInfoList": [{"Content": "写得清楚"}]}]},
        "hot": {"Total": 1, "Items": [
            {"Title": "如何评价某热点？", "Url": "https://www.zhihu.com/question/1",
             "ThumbnailUrl": "", "Summary": "摘要正文"}]},
        "answers": {"Items": [
            {"ContentType": "answer", "ContentToken": "456",
             "Url": "https://www.zhihu.com/question/1/answer/456",
             "Summary": "这是一段回答摘要"}],
            "Paging": {"IsEnd": False, "NextOffset": "20", "Totals": 30}},
        "comments": {"Items": [
            {"Comment": {"ID": 456, "CreatedAt": 1742822400,
                         "Content": "<p>示例评论</p>", "LikeCount": 2,
                         "AuthorToken": "u1"},
             "Children": [{"Comment": {"Content": "<p>回复内容</p>"}}]}],
            "Paging": {"IsEnd": True}},
        "contents": {"Items": [
            {"ContentType": "answer", "Url": "https://www.zhihu.com/answer/1",
             "CreatedAt": 1745486539, "LikeCount": 128, "CommentCount": 12,
             "FavoriteCount": 20, "Title": "如何理解某个问题？",
             "Summary": "摘要…"}],
            "Paging": {"IsEnd": True, "Totals": 1}},
        "stats": {"ContentType": "all",
                  "Metrics": {"Updated": "2026-09-08 12:00:00", "ViewCount": 100,
                              "UpvoteCount": 10, "Yesterday": {"ViewCount": 20}},
                  "CreationCounts": {"Answer": 3, "Article": 1, "Video": 0,
                                     "Follower": 50},
                  "Followers": {"Total": 50, "Yesterday": 2},
                  "Audience": {"Gender": [{"Name": "男", "Ratio": 0.7},
                                          {"Name": "女", "Ratio": 0.3}]}},
        "kb": {"Items": [{"DocName": "退款规则", "KnowledgeBaseID": "kb1",
                          "Content": ["七天内提交", "三个工作日到账"],
                          "OriginUrl": "https://a/b.md"}]},
    }))


class TestTaskCommands:
    """异步任务在工具层的终态处理（失败不能被当成成功）。"""

    class C(ZhihuClient):
        def __init__(self, final, download=None):
            super().__init__(access_secret="S" * 40)
            self._final = final
            self.downloaded = []
            self._download = download

        def upload_file(self, path):
            return "file_1"

        def create_pdf_task(self, file_id):
            return "pdf_1"

        def create_ppt_task(self, url, num_pages=12):
            return "ppt_1"

        def wait_task(self, kind, task_id):
            return self._final

        def download(self, url, filename=None):
            self.downloaded.append((url, filename))
            return self._download or ("output/zhihu/" + (filename or "x"))

    def _pdf(self, tmp_path):
        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-1.4")
        return str(p)

    def test_failed_pdf_task_is_reported_as_failure(self, tmp_path):
        c = self.C({"task_id": "pdf_1", "task_status": "failed", "progress": 0,
                    "result": None,
                    "error": {"code": "parse_failed", "message": "PDF parse failed"}})
        res = ZhihuTool(client=c).execute_json(
            {"command": "pdf_parse", "path": self._pdf(tmp_path)})
        assert res.success is False
        assert "parse_failed" in res.error and "failed" in res.error
        assert "完成" not in res.error, "失败的解析绝不能报成完成"
        assert c.downloaded == [], "失败时不应尝试下载"

    def test_failed_ppt_task_is_reported_as_failure(self):
        c = self.C({"task_id": "ppt_1", "task_status": "failed", "progress": 0,
                    "result": None,
                    "error": {"code": "ppt_generation_failed",
                              "message": "PPT 生成失败，请稍后重试"}})
        res = ZhihuTool(client=c).execute_json(
            {"command": "ppt", "content_url": "https://www.zhihu.com/answer/1"})
        assert res.success is False
        assert "ppt_generation_failed" in res.error
        assert "知乎服务端" in res.error, "要点明这是上游失败而非本地参数问题"

    def test_pdf_success_downloads_and_reports_local_path(self, tmp_path):
        c = self.C({"task_id": "pdf_1", "task_status": "succeeded", "progress": 1,
                    "result": {"url": "https://cdn/r.json", "summary": "摘要",
                               "expires_at_ms": 1782800000000},
                    "error": None})
        res = ZhihuTool(client=c).execute_json(
            {"command": "pdf_parse", "path": self._pdf(tmp_path)})
        assert res.success is True
        assert "摘要" in res.output and "解析结果已保存" in res.output
        assert c.downloaded and c.downloaded[0][1].startswith("pdf_parse_")
        assert "2026-" in res.output, "过期时间应转成可读格式"

    def test_succeeded_without_url_is_a_failure(self):
        c = self.C({"task_id": "ppt_1", "task_status": "succeeded", "progress": 1,
                    "result": None, "error": None})
        res = ZhihuTool(client=c).execute_json(
            {"command": "ppt", "content_url": "https://www.zhihu.com/answer/1"})
        assert res.success is False
        assert "没有产物链接" in res.error

    def test_wait_false_only_creates_task(self, tmp_path):
        c = self.C({"task_id": "pdf_1", "task_status": "pending", "result": None,
                    "error": None})
        res = ZhihuTool(client=c).execute_json(
            {"command": "pdf_parse", "path": self._pdf(tmp_path), "wait": False})
        assert res.success is True
        assert "pdf_1" in res.output and c.downloaded == []


class TestFormatting:
    def test_quota_lists_remaining(self, tool):
        out = tool.execute_json({"command": "quota"}).output
        assert "热榜" in out and "97" in out

    def test_search_output_is_readable_not_raw_json(self, tool):
        out = tool.execute_json({"command": "search", "query": "RAG"}).output
        assert "RAG 评测方法综述" in out
        assert "张三" in out and "赞同 128" in out
        assert "<em>" not in out, "高亮标签应清掉"
        assert "https://zhuanlan.zhihu.com/p/123" in out
        assert "{" not in out, "列表类命令不该回原始 JSON"

    def test_hot_output(self, tool):
        out = tool.execute_json({"command": "hot"}).output
        assert "如何评价某热点？" in out and "摘要正文" in out

    def test_answers_shows_next_offset(self, tool):
        out = tool.execute_json(
            {"command": "answers",
             "question_url": "https://www.zhihu.com/question/1"}).output
        assert "回答摘要" in out and "offset=20" in out

    def test_comments_strips_html_and_nests_children(self, tool):
        out = tool.execute_json({"command": "content_comments",
                                 "content_url": "u"}).output
        assert "<p>" not in out and "示例评论" in out and "回复内容" in out

    def test_contents_output(self, tool):
        out = tool.execute_json({"command": "my_contents"}).output
        assert "如何理解某个问题？" in out and "赞 128" in out

    def test_stats_flattens_nested_metrics(self, tool):
        out = tool.execute_json({"command": "account_stats"}).output
        assert "总览" in out and "昨日" in out and "粉丝" in out
        assert "男 70.0%" in out

    def test_kb_search_lists_fragments(self, tool):
        out = tool.execute_json({"command": "kb_search", "query": "退款",
                                 "kb_ids": ["kb1"]}).output
        assert "退款规则" in out and "七天内提交" in out and "来源:" in out

    def test_empty_search_explains_reason(self):
        t = ZhihuTool(client=FixedClient({"search": {"Items": [],
                                                     "EmptyReason": "无匹配"}}))
        out = t.execute_json({"command": "search", "query": "x"}).output
        assert "无结果" in out and "无匹配" in out

    def test_long_output_is_truncated_by_summarize(self):
        c = ZhihuClient(access_secret="S" * 40, max_chars=50)
        text = c.summarize({"Items": ["x" * 500]})
        assert "已截断" in text and len(text) < 200
