"""
持久终端会话测试：常驻 shell、cwd 跟踪、命令输出干净。
"""
import os
import platform

import pytest

from tools.terminal_session import TerminalSession, TerminalSessionManager

_WIN = platform.system() == "Windows"


def test_session_keeps_cwd(tmp_path):
    s = TerminalSession()
    target = str(tmp_path)
    ok, _ = s.run(f"cd {target}")
    assert ok
    st = s.state()
    assert st["alive"] is True
    # 归一化比较（Windows 大小写/斜杠）
    cwd = os.path.normcase(os.path.normpath(st["cwd"]))
    assert cwd == os.path.normcase(os.path.normpath(target))
    s.close()


def test_session_output_clean_no_sentinel(tmp_path):
    s = TerminalSession()
    ok, out = s.run("echo hello_term")
    assert ok
    assert "hello_term" in out
    assert "__DONE" not in out   # 哨兵行不应泄漏到输出
    s.close()


def test_session_manager_scoped(tmp_path):
    m = TerminalSessionManager()
    a = m.start("sess-a")
    b = m.start("sess-b")
    assert a is not None and b is not None
    # 同 key 复用
    assert m.start("sess-a") is a
    assert m.stop("sess-a") is True
    assert m.get("sess-a") is None
    m.close_all()


def test_session_stop_cleans_up():
    m = TerminalSessionManager()
    m.start("x")
    assert m.stop("x") is True
    assert m.stop("x") is False   # 已停


def test_win_unix_shim_translations():
    """Windows unix 单命令翻译：sleep/tail/grep → PowerShell 等价；其余原样。"""
    from tools.terminal import _win_unix_shim
    cases = [
        ("sleep 3", "Start-Sleep -Seconds 3.0"),
        ("tail -f app.log", "Get-Content 'app.log' -Tail 10 -Wait"),
        ("tail -n 20 out.txt", "-Tail 20"),
        ("tail -5 x.log", "-Tail 5"),
        ("tail -n3 a.log", "-Tail 3"),
        ("grep error app.log", "Select-String"),
        ("head -5 x.log", "-TotalCount 5"),
        ("head -n 8 x.log", "-TotalCount 8"),
        ("head x.log", "-TotalCount 10"),
        ("bg tail -n 10 app.log", "-Tail 10"),   # bg 前缀剥离后仍翻译
        ("sleep 3 &", "Start-Sleep -Seconds 3.0"),  # 尾部 & 剥离
        ("bg python server.py &", "python server.py"),  # 纯后台意图 → 剥离后前台执行
    ]
    for cmd, expect in cases:
        out = _win_unix_shim(cmd)
        assert expect in out, f"{cmd!r} -> {out!r}"
    # 不应翻译的普通命令原样返回
    assert _win_unix_shim("dir /b") == "dir /b"
    assert _win_unix_shim("python run.py") == "python run.py"
