"""
models/video_gen.py 与 tools/video_gen.py 的离线单元测试。

httpx.post / httpx.get 被 monkeypatch 拦截，不发起真实网络请求。
重点覆盖：
- 查询端点在 **HOST 根路径**（/agnesapi，不在 /v1 下）——实测踩过的坑
- 异步任务状态流转与**超时不丢任务**（返回 task_id 供稍后查询）
- 工具层 generate / status 两个命令
"""
import json

import pytest

from tools.video_gen import VideoGenTool

VIDEO_URL = "https://cos.example.com/videos/task_abc.mp4"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64   # 伪 mp4 头


class FakeResponse:
    def __init__(self, payload, status_code=200, content=None):
        self._payload = payload
        self.status_code = status_code
        self.content = content if content is not None else b""

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload) if isinstance(self._payload, dict) \
            else str(self._payload)


def make_model(tmp_path, **kw):
    from models.video_gen import VideoGenModel
    return VideoGenModel(
        api_key="sk-test", base_url="https://api.example.com/v1",
        model="agnes-video-2.5-flash", save_dir=str(tmp_path), timeout=5, **kw,
    )


# ============================================================
# 底层客户端
# ============================================================

class TestVideoGenModel:
    def test_query_base_strips_v1(self, tmp_path):
        """查询端点在 HOST 根路径：/v1/agnesapi 会 404（实测）。"""
        m = make_model(tmp_path)
        assert m.query_base == "https://api.example.com"
        assert not m.query_base.endswith("/v1")

    def test_create_returns_video_id(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        seen = {}

        def _post(url, json=None, headers=None, timeout=None):
            seen["url"] = url
            seen["payload"] = json
            return FakeResponse({"id": "task_1", "video_id": "task_1",
                                 "status": "queued"})

        monkeypatch.setattr(vg.httpx, "post", _post)
        m = make_model(tmp_path)
        d = m.create("一只猫", seconds="5", size="720P")
        assert d["video_id"] == "task_1"
        assert seen["url"] == "https://api.example.com/v1/videos"
        pl = seen["payload"]
        assert pl["model"] == "agnes-video-2.5-flash"
        assert pl["prompt"] == "一只猫"
        assert pl["mode"] == "text"
        assert pl["seconds"] == "5"
        assert pl["size"] == "720P"
        assert pl["aspect_ratio"] == "16:9"

    def test_create_accepts_task_id_alias(self, tmp_path, monkeypatch):
        """不同提供方字段名不一（id/video_id/task_id），都要能取到。"""
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"task_id": "task_9"}))
        assert make_model(tmp_path).create("猫")["video_id"] == "task_9"

    def test_create_without_id_raises(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"status": "queued"}))
        with pytest.raises(RuntimeError, match="缺少任务 ID"):
            make_model(tmp_path).create("猫")

    def test_query_builds_root_path_url(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        seen = {}

        def _get(url, headers=None, timeout=None):
            seen["url"] = url
            return FakeResponse({"status": "in_progress", "progress": 40})

        monkeypatch.setattr(vg.httpx, "get", _get)
        d = make_model(tmp_path).query("task_7")
        assert d["status"] == "in_progress"
        assert seen["url"].startswith("https://api.example.com/agnesapi?")
        assert "video_id=task_7" in seen["url"]
        assert "model_name=agnes-video-2.5-flash" in seen["url"]
        assert "/v1/agnesapi" not in seen["url"]

    def test_wait_until_completed(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        states = [
            {"status": "queued", "progress": 0},
            {"status": "in_progress", "progress": 50},
            {"status": "completed", "progress": 100, "url": VIDEO_URL},
        ]
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None:
                            FakeResponse(states.pop(0)))
        m = make_model(tmp_path)
        m.poll_interval = 0
        out = m.wait("task_1", max_wait=10)
        assert out["status"] == "completed"
        assert out["url"] == VIDEO_URL
        assert out["timed_out"] is False

    def test_wait_timeout_keeps_task(self, tmp_path, monkeypatch):
        """超时不丢任务：timed_out=True 且仍能拿到 video_id 供稍后查询。"""
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None:
                            FakeResponse({"status": "in_progress", "progress": 30}))
        m = make_model(tmp_path)
        m.poll_interval = 0
        out = m.wait("task_1", max_wait=0)
        assert out["timed_out"] is True
        assert out["status"] == "in_progress"

    def test_wait_reports_failure(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None:
                            FakeResponse({"status": "failed", "error": "内容审核未通过"}))
        m = make_model(tmp_path)
        m.poll_interval = 0
        out = m.wait("task_1", max_wait=5)
        assert out["timed_out"] is False
        assert out["status"] == "failed"
        assert "审核" in out["error"]

    def test_wait_survives_transient_429(self, tmp_path, monkeypatch):
        """回归：轮询被限流不能判任务死刑。

        实测（2026-09-17）6 秒一次的查询被上游拒绝 429 "查询过于频繁"，
        旧实现直接抛错 → 模型重做整条视频，白烧一次配额（500 秒/天）外加
        两分钟等待。现在应退避重试直到拿到结果。
        """
        import models.video_gen as vg
        seq = [
            ("err", 429, '{"error":{"code":429,"message":"查询过于频繁，请稍后重试"}}'),
            ("err", 429, '{"error":{"code":429,"message":"查询过于频繁，请稍后重试"}}'),
            ("ok", 200, {"status": "completed", "progress": 100, "url": VIDEO_URL}),
        ]

        def fake_get(url, headers=None, timeout=None):
            kind, code, payload = seq.pop(0)
            return FakeResponse(payload if kind == "ok" else None,
                                status_code=code, content=str(payload))

        monkeypatch.setattr(vg.httpx, "get", fake_get)
        m = make_model(tmp_path)
        m.poll_interval = 0
        out = m.wait("task_1", max_wait=10)
        assert out["status"] == "completed"
        assert out["url"] == VIDEO_URL
        assert out["timed_out"] is False

    def test_wait_still_fails_fast_on_auth_error(self, tmp_path, monkeypatch):
        """401/404 这类不是"待会儿再问就好"，必须立刻抛出，别无谓重试。"""
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None:
                            FakeResponse(None, status_code=401, content="bad key"))
        m = make_model(tmp_path)
        m.poll_interval = 0
        with pytest.raises(RuntimeError, match="401"):
            m.wait("task_1", max_wait=10)

    def test_transient_classifier(self):
        from models.video_gen import _is_transient_query_error
        assert _is_transient_query_error(RuntimeError("查询失败 HTTP 429: 查询过于频繁"))
        assert _is_transient_query_error(RuntimeError("HTTP 503: bad gateway"))
        assert _is_transient_query_error(RuntimeError("视频任务查询失败: timeout"))
        assert not _is_transient_query_error(RuntimeError("查询失败 HTTP 401: bad key"))
        assert not _is_transient_query_error(RuntimeError("查询失败 HTTP 404: not found"))

    def test_generate_downloads_video(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"video_id": "task_1",
                                                        "status": "queued"}))
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None,
                            follow_redirects=None:
                            FakeResponse({"status": "completed", "url": VIDEO_URL},
                                         content=MP4_BYTES)
                            if "agnesapi" not in url else
                            FakeResponse({"status": "completed", "url": VIDEO_URL}))
        m = make_model(tmp_path)
        m.poll_interval = 0
        r = m.generate("一只猫", max_wait=5)
        assert r["timed_out"] is False
        assert r["url"] == VIDEO_URL
        p = r["local_path"]
        assert p and p.endswith(".mp4")
        assert open(p, "rb").read() == MP4_BYTES

    def test_generate_no_wait_returns_task_id(self, tmp_path, monkeypatch):
        """wait=False：只创建任务立即返回（适合超长视频）。"""
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"video_id": "task_x",
                                                        "status": "queued"}))
        r = make_model(tmp_path).generate("一只猫", wait=False)
        assert r["video_id"] == "task_x"
        assert r["local_path"] is None and r["url"] is None

    def test_download_failure_keeps_url(self, tmp_path, monkeypatch):
        """下载失败不应丢掉生成结果（仍回传 url）。"""
        import models.video_gen as vg
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: FakeResponse({"video_id": "task_1"}))
        monkeypatch.setattr(vg.httpx, "get",
                            lambda url, headers=None, timeout=None,
                            follow_redirects=None:
                            FakeResponse({"status": "completed", "url": VIDEO_URL},
                                         status_code=500)
                            if "agnesapi" not in url else
                            FakeResponse({"status": "completed", "url": VIDEO_URL}))
        m = make_model(tmp_path)
        m.poll_interval = 0
        r = m.generate("一只猫", max_wait=5)
        assert r["url"] == VIDEO_URL
        assert r["local_path"] is None
        assert r.get("download_error")


# ============================================================
# 工具层
# ============================================================

class FakeModel:
    """脚本化假客户端，避免联网。"""

    api_key = "sk-test"
    model = "agnes-video-2.5-flash"

    def __init__(self, gen=None, query_result=None):
        self._gen = gen or {}
        self._query = query_result or {}
        self.downloaded = []
        self.generated = []      # 记录每次 generate 的参数，便于断言"没有误生成"
        self.queried = []        # 记录每次 query 的 task_id

    def generate(self, prompt, seconds=None, size=None, aspect_ratio=None,
                 wait=True, **kw):
        self.generated.append({"prompt": prompt, "seconds": seconds,
                               "size": size, "aspect_ratio": aspect_ratio})
        return {**self._gen, "prompt": prompt}

    def query(self, task_id, model=None):
        self.queried.append(task_id)
        return self._query

    def download(self, url, video_id=""):
        self.downloaded.append(url)
        return f"/tmp/{video_id}.mp4"


class TestVideoGenTool:
    def test_schema_commands(self):
        s = VideoGenTool().schema
        assert s["properties"]["command"]["enum"] == ["generate", "status"]
        assert s["required"] == ["command"]

    def test_empty_prompt_rejected(self):
        r = VideoGenTool(video_model=FakeModel()).execute_json({"command": "generate"})
        assert r.success is False and "描述" in r.error

    def test_generate_success_reports_path(self):
        t = VideoGenTool(video_model=FakeModel(gen={
            "video_id": "task_1", "status": "completed", "url": VIDEO_URL,
            "local_path": "/tmp/vid.mp4", "timed_out": False,
        }))
        r = t.execute_json({"command": "generate", "prompt": "一只猫"})
        assert r.success is True
        assert "task_1" in r.output and "/tmp/vid.mp4" in r.output
        assert r.metadata["video_id"] == "task_1"

    def test_generate_timeout_returns_task_id(self):
        """超时：成功返回但提示可用 status 取结果（不丢任务）。"""
        t = VideoGenTool(video_model=FakeModel(gen={
            "video_id": "task_2", "status": "in_progress", "progress": 60,
            "timed_out": True,
        }))
        r = t.execute_json({"command": "generate", "prompt": "一只猫"})
        assert r.success is True
        assert "仍在生成" in r.output and "task_2" in r.output
        assert "status" in r.output
        assert r.metadata["pending"] is True

    def test_generate_failure_reported(self):
        t = VideoGenTool(video_model=FakeModel(gen={
            "video_id": "task_3", "status": "failed", "error": "内容审核未通过",
            "timed_out": False,
        }))
        r = t.execute_json({"command": "generate", "prompt": "一只猫"})
        assert r.success is False and "审核" in r.error

    def test_status_completed_downloads(self, tmp_path):
        fm = FakeModel(query_result={"status": "completed", "url": VIDEO_URL})
        r = VideoGenTool(video_model=fm).execute_json(
            {"command": "status", "task_id": "task_9"})
        assert r.success is True
        assert fm.downloaded == [VIDEO_URL]
        assert "/tmp/task_9.mp4" in r.output

    def test_status_pending(self):
        r = VideoGenTool(video_model=FakeModel(
            query_result={"status": "in_progress", "progress": 20})).execute_json(
            {"command": "status", "task_id": "task_9"})
        assert r.success is True and "仍在生成" in r.output
        assert r.metadata["pending"] is True

    def test_status_requires_task_id(self):
        r = VideoGenTool(video_model=FakeModel()).execute_json({"command": "status"})
        assert r.success is False and "task_id" in r.error

    def test_command_holding_prompt_is_tolerated(self):
        """回归：模型常把整段提示词塞进 command、漏掉 prompt。

        实测（2026-09-17 动漫漫剧任务）30 次 video_gen 调用里 7 次如此，旧实现
        回一句"未知命令: <两百字提示词>"，模型只能整轮重做——每次白等约 110 秒。
        现在按提示词处理，与字符串入口 execute() 的宽松语义一致。
        """
        fm = FakeModel(gen={"video_id": "t9", "status": "completed",
                            "local_path": "/tmp/x.mp4"})
        r = VideoGenTool(video_model=fm).execute_json(
            {"command": "一只橘猫在窗台上打哈欠，特写，暖色夕阳"})
        assert r.success is True, r.error
        assert "未知命令" not in (r.error or "")
        assert fm.generated and fm.generated[0]["prompt"].startswith("一只橘猫")

    def test_explicit_prompt_wins_over_bogus_command(self):
        """两个字段都在时以 prompt 为准，command 只当噪声忽略。"""
        fm = FakeModel(gen={"video_id": "t9", "status": "completed",
                            "local_path": "/tmp/x.mp4"})
        r = VideoGenTool(video_model=fm).execute_json(
            {"command": "生成一只猫", "prompt": "一只狗"})
        assert r.success is True
        assert fm.generated[0]["prompt"] == "一只狗"

    def test_status_inferred_from_task_id(self):
        """只给了 task_id（command 缺失/写错）也应走 status 而不是误生成。"""
        fm = FakeModel(query_result={"status": "completed", "url": VIDEO_URL})
        r = VideoGenTool(video_model=fm).execute_json({"command": "看看", "task_id": "t7"})
        assert r.success is True
        assert fm.generated == []          # 没有触发新生成
        assert fm.queried == ["t7"]

    def test_generate_without_prompt_still_errors(self):
        r = VideoGenTool(video_model=FakeModel()).execute_json({"command": "generate"})
        assert r.success is False and "prompt" in r.error

    def test_string_entry_generate_and_status(self):
        t = VideoGenTool(video_model=FakeModel(gen={
            "video_id": "t1", "status": "completed", "local_path": "/tmp/a.mp4"}))
        assert t.execute("generate 一只猫").success is True
        assert t.execute("status t1").success is True

    def test_registered_in_tool_manager(self):
        from tools.tool_manager import ToolManager
        assert "video_gen" in ToolManager().list_tools()

class TestSecondsClamp:
    """P2：供应商只接受 [4,12] 秒；越界必须就近 clamp，不能原样发出换一次无效请求。"""

    def test_normalize_seconds_bounds(self):
        from models.video_gen import _normalize_seconds as n
        assert n("3") == "4"          # 3s 实测会被拒（invalid_request）
        assert n("5") == "5"
        assert n("30") == "12"
        assert n("4.5") == "4.5"
        assert n("abc") == "5"        # 非法值回退默认
        assert n("3", 6, 8) == "6"    # 自定义区间

    def test_create_payload_is_clamped(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        seen = {}

        def _post(url, json=None, headers=None, timeout=None):
            seen["payload"] = json
            return FakeResponse({"id": "t1", "video_id": "t1", "status": "queued"})

        monkeypatch.setattr(vg.httpx, "post", _post)
        m = make_model(tmp_path)
        d = m.create("短镜", seconds="3")
        assert seen["payload"]["seconds"] == "4"
        assert d["seconds_clamped_from"] == "3"

    def test_create_keeps_legal_value(self, tmp_path, monkeypatch):
        import models.video_gen as vg
        seen = {}
        monkeypatch.setattr(vg.httpx, "post", lambda url, json=None, headers=None,
                            timeout=None: (seen.update(payload=json),
                                           FakeResponse({"id": "t2", "video_id": "t2"}))[1])
        m = make_model(tmp_path)
        d = m.create("正常", seconds="8")
        assert seen["payload"]["seconds"] == "8"
        assert "seconds_clamped_from" not in d
