"""残留浏览器进程清理：两个平台各一条取进程表的实现。

背景（2026-09-24 审计）：`_force_cleanup_residual()` 原来只有 PowerShell + taskkill
一条路 —— Linux 上 `powershell: not found` → 静默返回 0，残留的 Chromium 与
Playwright 的 node 会一直累积，而 `reset()` 的注释还写着"必须真正清掉残留进程"。

这里钉住两段**平台无关**的逻辑（在本机就能测，不需要真 Linux）：
  · 从进程表里挑出"命令行确实指向本工具 profile 目录"的进程；
  · 杀进程的语义（先 TERM、还活着再 KILL；已经没了也算清掉）。
关键是不能误伤：前缀相似的目录（`/tmp/p` vs `/tmp/p2`）必须区分开。
"""
import os
import signal
import time
from types import SimpleNamespace

import pytest

from tools.browser import BrowserTool

PROFILE = "/tmp/myagent-profile"


def _tool(profile: str = PROFILE) -> BrowserTool:
    """只装出需要的属性：__init__ 会去连浏览器，这里绕开。"""
    t = BrowserTool.__new__(BrowserTool)
    t._profile_dir = profile
    return t


class _FakeRun:
    """假的 subprocess.run：记录每次调用，按预设返回 stdout 或抛异常。"""

    def __init__(self, stdout: str = "", exc: Exception = None):
        self.stdout, self.exc, self.calls = stdout, exc, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.exc:
            raise self.exc
        return SimpleNamespace(stdout=self.stdout, returncode=0, stderr="")


PS_HEADER = "    PID COMMAND\n"


class TestPosixResidualPids:
    def test_matches_only_our_profile_dir(self, monkeypatch):
        ps = _FakeRun(
            "  123 /usr/lib/chromium/chrome --user-data-dir=/tmp/myagent-profile --headless\n"
            "  124 /usr/lib/chromium/chrome --user-data-dir=/tmp/somebody-else\n"
            "  125 /usr/lib/chromium/chrome --no-user-data-dir-at-all\n"
            "  126 /usr/bin/node /usr/lib/playwright/driver.js\n"
        )
        monkeypatch.setattr("subprocess.run", ps)
        assert _tool()._residual_pids_posix() == ["123"]

    def test_prefix_similar_dir_is_not_matched(self, monkeypatch):
        """`/tmp/p` 不能误伤 `/tmp/p2` —— 这正是 Windows 侧 `-like '*...*'` 的隐患。"""
        ps = _FakeRun(
            "  200 /usr/lib/chromium/chrome --user-data-dir=/tmp/myagent-profile2\n"
        )
        monkeypatch.setattr("subprocess.run", ps)
        assert _tool("/tmp/myagent-profile")._residual_pids_posix() == []

    def test_quoted_value_is_accepted(self, monkeypatch):
        ps = _FakeRun("  300 /usr/lib/chromium/chrome --user-data-dir=\"/tmp/myagent-profile\"\n")
        monkeypatch.setattr("subprocess.run", ps)
        assert _tool()._residual_pids_posix() == ["300"]

    def test_multiple_matches_all_returned(self, monkeypatch):
        ps = _FakeRun(
            "  1 /usr/lib/chromium/chrome --user-data-dir=/tmp/myagent-profile --renderer\n"
            "  2 /usr/lib/chromium/chrome --user-data-dir=/tmp/myagent-profile --gpu\n"
        )
        monkeypatch.setattr("subprocess.run", ps)
        assert _tool()._residual_pids_posix() == ["1", "2"]

    def test_ps_missing_returns_empty_not_crash(self, monkeypatch):
        monkeypatch.setattr("subprocess.run",
                            _FakeRun(exc=FileNotFoundError("ps 不存在")))
        assert _tool()._residual_pids_posix() == []


class TestPosixKill:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda _s: None)   # 别真等 0.5s

    def test_terminated_process_returns_true(self, monkeypatch):
        """TERM 之后探活发现没了 —— 正常路径，应立即返回且不发 KILL。"""
        sent = []

        def fake_kill(pid, sig):
            sent.append(sig)
            if sig == 0:
                raise ProcessLookupError("已经退出")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert BrowserTool._kill_residual_posix("42") is True
        assert sent == [signal.SIGTERM, 0]

    def test_stubborn_process_gets_sigkill(self, monkeypatch):
        """探活一直成功（赖着不走）→ 最后必须升级到 SIGKILL。"""
        sent = []

        def fake_kill(pid, sig):
            sent.append(sig)

        monkeypatch.setattr(os, "kill", fake_kill)
        assert BrowserTool._kill_residual_posix("42") is True
        assert sent[0] == signal.SIGTERM
        # 用数字 9 而不是 signal.SIGKILL：后者在 Windows 上**不存在**（AttributeError），
        # 而这条测试要能在本机（Windows）跑起来。SIGKILL 在所有 POSIX 上都是 9。
        assert sent[-1] == 9

    def test_failed_sigkill_is_reported_as_failure(self, monkeypatch):
        """升级 KILL 也没杀掉时必须返回 False —— 不能把失败报成"已清理"。"""
        def fake_kill(pid, sig):
            if sig == signal.SIGTERM or sig == 0:
                return None                       # TERM 无效、探活说还活着
            raise PermissionError("不许杀")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert BrowserTool._kill_residual_posix("42") is False

    def test_already_gone_counts_as_cleaned(self, monkeypatch):
        def fake_kill(pid, sig):
            raise ProcessLookupError("不存在")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert BrowserTool._kill_residual_posix("42") is True

    def test_permission_error_is_a_failure(self, monkeypatch):
        def fake_kill(pid, sig):
            raise PermissionError("不是我的进程")

        monkeypatch.setattr(os, "kill", fake_kill)
        assert BrowserTool._kill_residual_posix("42") is False


class TestWindowsResidualPids:
    def test_parses_powershell_pid_list(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", _FakeRun("1234\n5678\n"))
        assert _tool()._residual_pids_win() == ["1234", "5678"]

    def test_powershell_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr("subprocess.run",
                            _FakeRun(exc=FileNotFoundError("powershell 不存在")))
        assert _tool()._residual_pids_win() == []


class TestDispatchAndCount:
    def test_dispatch_picks_by_platform(self, monkeypatch):
        """分派必须跟着 os.name 走 —— 这正是当初漏掉的一半。"""
        t = _tool()
        monkeypatch.setattr(BrowserTool, "_residual_pids_win", lambda self: ["win"])
        monkeypatch.setattr(BrowserTool, "_residual_pids_posix", lambda self: ["posix"])
        monkeypatch.setattr(os, "name", "nt")
        assert t._residual_pids() == ["win"]
        monkeypatch.setattr(os, "name", "posix")
        assert t._residual_pids() == ["posix"]

    def test_cleanup_counts_only_successful_kills(self, monkeypatch):
        t = _tool()
        monkeypatch.setattr(BrowserTool, "_residual_pids",
                            lambda self: ["1", "2", "3"])
        monkeypatch.setattr(BrowserTool, "_kill_residual",
                            lambda self, pid: pid != "2")     # 2 号杀不掉
        assert t._force_cleanup_residual() == 2

    def test_cleanup_with_no_residual_is_zero(self, monkeypatch):
        t = _tool()
        monkeypatch.setattr(BrowserTool, "_residual_pids", lambda self: [])
        assert t._force_cleanup_residual() == 0
