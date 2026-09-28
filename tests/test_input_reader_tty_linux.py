"""真实 TTY 下的交互输入（Linux 专用）：用 pty 复现"终端粘贴 20 行中文 / 中途 Ctrl+C"。

Windows 没有 pty，整个文件跳过；Linux（含虚拟机）上会随 `pytest tests -q` 一起跑。
驱动脚本只调 `input_reader.read_goal()`，不启动 Agent，因此不联网、不跑工具。
"""
import os
import sys
import time

import pytest

if sys.platform != "win32":          # ↑ Windows 没有 termios/pty，导入即报错，先挡住
    import pty
else:
    pty = None

pytestmark = pytest.mark.skipif(pty is None or not hasattr(os, "openpty"),
                                reason="需要真实 TTY（POSIX pty）")

DRIVER = """
import sys
sys.path.insert(0, {root!r})
from agent import input_reader
goal = input_reader.read_goal()
print()
print("GOAL>>>" + repr(goal) + "<<<")
"""


def _spawn_driver():
    """在 pty 里启动驱动脚本，返回 (pid, 主端 fd)。"""
    pid, fd = pty.fork()
    if pid == 0:                                  # 子进程：stdin/stdout 都挂到 pty 从端
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.execv(sys.executable, [sys.executable, "-c", DRIVER.format(root=root)])
    return pid, fd


def _read_until(fd, marker: bytes, timeout: float = 10.0) -> bytes:
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if marker in buf:
            break
    return buf


def test_paste_20_chinese_lines_under_real_tty():
    """粘贴 20 行以上中文：必须整体收为一条多行输入，不报错、不退出。"""
    lines = ["第%d行：粘贴进来的中文内容，包含标点，。！" % i for i in range(1, 23)]
    pid, fd = _spawn_driver()
    try:
        time.sleep(0.4)                           # 等驱动脚本进入 read_goal
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
        out = _read_until(fd, b"<<<")
    finally:
        os.close(fd)
        os.waitpid(pid, 0)

    text = out.decode("utf-8", "replace")
    assert "GOAL>>>" in text, "驱动脚本没跑完：%r" % text[-400:]
    assert "UnicodeDecodeError" not in text and "Traceback" not in text
    assert "第22行" in text and "第1行" in text, "粘贴内容必须完整收到"


def test_slow_typing_with_emoji_and_rare_hanzi():
    """逐字间隔输入（每字节之间 15ms）：emoji 与生僻字被拆成多块到达也不许解错。"""
    text = "𠮷 emoji 🐍 生僻字 龘"
    raw = text.encode("utf-8")
    pid, fd = _spawn_driver()
    try:
        time.sleep(0.4)
        for i in range(len(raw)):                     # 一个字节一个字节地"敲"
            os.write(fd, raw[i:i + 1])
            time.sleep(0.015)
        os.write(fd, b"\n")
        out = _read_until(fd, b"<<<")
    finally:
        os.close(fd)
        os.waitpid(pid, 0)

    shown = out.decode("utf-8", "replace")
    assert "Traceback" not in shown and "UnicodeDecodeError" not in shown
    assert repr(text) in shown, "逐字输入的内容必须完整还原：%r" % shown[-300:]


def test_paste_five_times_in_a_row():
    """连做 5 次粘贴：确认无残留字节影响下一轮（每次新起一个进程，模拟真实多轮使用）。"""
    lines = ["第%d行：连续粘贴中文内容" % i for i in range(1, 22)]
    for round_no in range(5):
        pid, fd = _spawn_driver()
        try:
            time.sleep(0.35)
            os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
            out = _read_until(fd, b"<<<")
        finally:
            os.close(fd)
            os.waitpid(pid, 0)
        shown = out.decode("utf-8", "replace")
        assert "Traceback" not in shown, "第 %d 轮崩了：%r" % (round_no + 1, shown[-300:])
        assert repr("\n".join(lines)) in shown, "第 %d 轮内容不完整" % (round_no + 1)


def test_ctrl_c_during_paste_returns_to_prompt():
    """粘贴进行中按 Ctrl+C：中断本次输入（read_goal 返回 None），进程不崩。"""
    pid, fd = _spawn_driver()
    try:
        time.sleep(0.4)
        os.write(fd, "第一行中文".encode("utf-8"))   # 只给半行，不给换行
        time.sleep(0.2)
        os.write(fd, b"\x03")                        # Ctrl+C
        out = _read_until(fd, b"<<<")
    finally:
        os.close(fd)
        os.waitpid(pid, 0)

    text = out.decode("utf-8", "replace")
    assert "GOAL>>>None<<<" in text, "Ctrl+C 应让 read_goal 返回 None，实际：%r" % text[-400:]
    assert "Traceback" not in text
