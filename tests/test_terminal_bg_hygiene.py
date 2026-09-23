"""
批次2 回归：后台任务的文件句柄与临时日志不再泄漏。

实测故障（2026-09-17 审计）：`_start_background` 打开的日志文件句柄只由
`self._jobs` 里的 Popen 间接持有，`_bg_kill` 从不关闭，`%TEMP%\\my_agent_bg_*.log`
也永不删除；且 `self._jobs` 无上限、`_bg_output` 用 `readlines()` 全量读入
（一个刷屏任务能把内存打满）。
"""
import glob
import os
import tempfile
import time

import pytest

from tools.terminal import TerminalTool


@pytest.fixture
def tool():
    return TerminalTool()


def bg_logs() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "my_agent_bg_*.log")))


def start_bg(tool, cmd: str) -> str:
    r = tool.execute_json({"command": cmd, "background": True})
    assert r.success, r.error
    return [k for k in tool._jobs][-1]


class TestBackgroundJobHygiene:
    def test_handle_is_tracked(self, tool):
        jid = start_bg(tool, "echo bg-handle-test")
        time.sleep(1.0)
        assert "handle" in tool._jobs[jid], "必须保存日志句柄，否则无法关闭"

    def test_kill_closes_handle_but_keeps_log(self, tool):
        """kill 关闭句柄（修 fd 泄漏），但保留日志供事后排查。"""
        jid = start_bg(tool, "echo bg-kill-test")
        time.sleep(1.0)
        path = tool._jobs[jid]["out_path"]
        assert os.path.exists(path)
        r = tool.execute_json({"command": f"bg kill {jid}"})
        assert r.success
        assert "handle" not in tool._jobs.get(jid, {}), "句柄应已关闭并摘除"
        assert os.path.exists(path), "日志应保留，便于 kill 后查输出"
        out = tool.execute_json({"command": f"bg output {jid} 5"})
        assert out.success, "kill 后仍应能查看输出"

    def test_eviction_deletes_log_and_entry(self, tool):
        """任务表超限时，被淘汰的老任务其日志文件也要删掉（总量有界）。"""
        jid = start_bg(tool, "echo bg-evict-test")
        time.sleep(0.8)
        path = tool._jobs[jid]["out_path"]
        assert os.path.exists(path)
        for i in range(25):                     # 触发淘汰
            start_bg(tool, f"echo filler-{i}")
        time.sleep(1.5)
        assert len(tool._jobs) <= 21, "任务表未淘汰"
        assert jid not in tool._jobs, "最老的任务应已被淘汰"
        assert not os.path.exists(path), "被淘汰任务的临时日志应删除"

    def test_output_tail_is_bounded(self, tool):
        """刷屏任务只能回读尾部，不能把整个日志读进内存。"""
        jid = start_bg(tool, "for /L %i in (1,1,3000) do @echo line-%i-flood")
        time.sleep(2.5)
        r = tool.execute_json({"command": f"bg output {jid} 5"})
        assert r.success, r.error
        assert r.output.count("\n") <= 8, "只应返回请求的尾部行数"
        tool.execute_json({"command": f"bg kill {jid}"})

    def test_jobs_map_is_bounded(self, tool):
        """连续起任务不能无上限堆积（每个都握着一个文件句柄）。"""
        for i in range(25):
            start_bg(tool, f"echo job-{i}")
        time.sleep(1.5)
        assert len(tool._jobs) <= 21, f"任务表未淘汰: {len(tool._jobs)}"

    def test_release_is_idempotent(self, tool):
        jid = start_bg(tool, "echo idem")
        time.sleep(0.8)
        job = tool._jobs[jid]
        tool._release_job(job)
        tool._release_job(job)          # 重复调用不得抛错
        assert "handle" not in job


class TestBackgroundStartFailureCleansUp:
    """Popen 抛错时必须关句柄、删日志。

    实测故障（2026-09-22 审计）：`_start_background` 先 open 日志再 Popen，异常分支
    只 return —— 句柄不 close、文件也没进 `_jobs`（后续 `_prune_jobs` 永远碰不到它），
    每失败一次就漏一个句柄 + 一个空日志文件。
    """

    def test_failed_spawn_leaves_no_handle_or_file(self, monkeypatch, tmp_path):
        import glob
        import os
        import tempfile

        import tools.terminal as t

        tool = t.TerminalTool()
        opened = []
        real_open = open

        def _tracking_open(path, *a, **k):
            f = real_open(path, *a, **k)
            opened.append(f)
            return f

        def _boom(*a, **k):
            raise OSError("shell 拉不起来")

        monkeypatch.setattr("builtins.open", _tracking_open)
        monkeypatch.setattr(t.subprocess, "Popen", _boom)

        pattern = os.path.join(tempfile.gettempdir(), "my_agent_bg_*.log")
        before = set(glob.glob(pattern))
        r = tool.execute_json({"command": "whatever", "background": True})

        assert r.success is False and "后台启动失败" in r.error
        assert all(f.closed for f in opened), "日志句柄没关"
        assert set(glob.glob(pattern)) == before, "失败时留下了空日志文件"
