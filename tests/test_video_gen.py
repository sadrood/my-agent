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

    def generate(self, prompt, seconds=None, size=None, aspect_ratio=None,
                 wait=True, **kw):
        return {**self._gen, "prompt": prompt}

    def query(self, task_id, model=None):
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

    def test_unknown_command(self):
        r = VideoGenTool(video_model=FakeModel()).execute_json({"command": "boom"})
        assert r.success is False and "未知命令" in r.error

    def test_string_entry_generate_and_status(self):
        t = VideoGenTool(video_model=FakeModel(gen={
            "video_id": "t1", "status": "completed", "local_path": "/tmp/a.mp4"}))
        assert t.execute("generate 一只猫").success is True
        assert t.execute("status t1").success is True

    def test_registered_in_tool_manager(self):
        from tools.tool_manager import ToolManager
        assert "video_gen" in ToolManager().list_tools()
