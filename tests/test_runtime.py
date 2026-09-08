"""
外部 Agent Runtime 测试：custom 命令桥接事件、停止、错误处理。
"""
import os
import sys

import pytest

from agent.runtime import run_external, detect_available, _build_argv


def test_custom_runtime_bridges_events():
    events = []
    # 用临时脚本模拟外部 CLI 输出
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write("print('hello from runtime')\nprint('second')\n")
        script = f.name
    try:
        out = run_external("custom", "任务", cwd=os.getcwd(), on_event=lambda t, d: events.append((t, d)),
                           cfg={"command": f"python {script}"})
    finally:
        os.unlink(script)
    types = [e[0] for e in events]
    assert types[0] == "run_start"
    assert "stream_delta" in types
    assert types[-2] == "answer"
    assert types[-1] == "run_end"
    assert "hello from runtime" in out
    assert events[-1][1]["status"] == "completed"


def test_build_argv_claude_and_codex():
    argv, shell = _build_argv("claude", "写代码", None)
    assert argv[0] == "claude" and "-p" in argv and not shell
    argv2, shell2 = _build_argv("codex", "写代码", None)
    assert argv2[0] == "codex" and not shell2


def test_custom_missing_command_errors():
    events = []
    out = run_external("custom", "x", cwd=os.getcwd(), on_event=lambda t, d: events.append((t, d)), cfg={})
    assert events[-1][1]["status"] == "failed"
    assert "命令" in out or "command" in out


def test_detect_returns_bools():
    d = detect_available()
    assert isinstance(d.get("claude"), bool)
    assert isinstance(d.get("codex"), bool)
