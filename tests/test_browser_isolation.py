"""崩溃隔离：会话重建时作废在飞任务，并清理残留浏览器进程。"""
import threading
import time

import pytest

from tools.browser import BrowserTool


class _AliveThread:
    def is_alive(self):
        return True


class _CaptureQueue:
    """不执行任务，只把它扣下来，便于测试里精确控制"何时跑完"。"""

    def __init__(self):
        self.job = None

    def put(self, job):
        self.job = job


def _bare_tool(tmp_path, monkeypatch):
    tool = BrowserTool.__new__(BrowserTool)
    tool._gen = 0
    tool._state_lock = threading.RLock()
    tool._worker = _AliveThread()
    tool._worker_broken = False
    tool._worker_queue = _CaptureQueue()
    tool._playwright = None
    tool._browser = None
    tool._context = None
    tool._pages = []
    tool._current_page_idx = 0
    tool._profile_dir = str(tmp_path)
    monkeypatch.setattr(BrowserTool, "_is_browser_alive", lambda self: True)
    monkeypatch.setattr(BrowserTool, "_is_context_alive", lambda self: True)
    return tool


class TestStaleJob:
    def test_job_from_old_session_is_dropped(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        ran = []
        outcome = {}

        def _call():
            try:
                outcome["value"] = tool._dispatch(lambda: ran.append(1) or "ran")
            except Exception as e:                   # noqa: BLE001
                outcome["error"] = str(e)

        caller = threading.Thread(target=_call)
        caller.start()
        time.sleep(0.05)
        assert tool._worker_queue.job is not None, "任务应已投递"
        tool._gen += 1                               # 会话重建（作废在飞任务）
        tool._worker_queue.job()                     # 旧 worker 现在才跑到它
        caller.join(2)
        assert not caller.is_alive()
        assert ran == [], "旧会话的任务不该被执行"
        assert "会话已重建" in outcome.get("error", "")

    def test_current_session_job_runs(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        outcome = {}

        def _call():
            outcome["value"] = tool._dispatch(lambda: "ran")

        caller = threading.Thread(target=_call)
        caller.start()
        time.sleep(0.05)
        tool._worker_queue.job()
        caller.join(2)
        assert outcome.get("value") == "ran"


class TestInvalidate:
    def test_bumps_generation_and_cleans_residual(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        cleaned = []
        monkeypatch.setattr(BrowserTool, "_force_cleanup_residual",
                            lambda self: cleaned.append(1) or 0)
        tool._pages = ["page"]
        tool._context = object()                 # 有会话才算"启动过"
        tool._invalidate()
        assert tool._gen == 1 and cleaned == [1]
        assert tool._worker is None and tool._pages == []
        assert tool._worker_broken is True

    def test_never_launched_skips_residual_cleanup(self, tmp_path, monkeypatch):
        """从没启动过浏览器就别去 shell 出进程表（生产与测试都会被拖慢）。"""
        tool = _bare_tool(tmp_path, monkeypatch)
        cleaned = []
        monkeypatch.setattr(BrowserTool, "_force_cleanup_residual",
                            lambda self: cleaned.append(1) or 0)
        tool._invalidate()
        tool.reset()
        assert cleaned == []

    def test_del_on_never_launched_tool_does_nothing(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        called = []
        monkeypatch.setattr(BrowserTool, "_close", lambda self, _args="", _wait=None:
                            called.append(1))
        tool.__del__()
        assert called == [], "半初始化实例被回收时不该走关闭/清理流程"

    def test_cleanup_can_be_skipped(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        called = []
        monkeypatch.setattr(BrowserTool, "_force_cleanup_residual",
                            lambda self: called.append(1) or 0)
        tool._invalidate(cleanup_residual=False)
        assert called == []

    def test_concurrent_invalidate_is_safe(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        monkeypatch.setattr(BrowserTool, "_force_cleanup_residual", lambda self: 0)
        threads = [threading.Thread(target=tool._invalidate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
        assert tool._gen == 5


class TestReset:
    def test_reset_invalidates_inflight_jobs(self, tmp_path, monkeypatch):
        tool = _bare_tool(tmp_path, monkeypatch)
        monkeypatch.setattr(BrowserTool, "_force_cleanup_residual", lambda self: 0)
        before = tool._gen
        tool.reset()
        assert tool._gen == before + 1
