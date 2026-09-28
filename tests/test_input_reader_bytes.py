"""字节级 stdin 读取：修 Linux 交互粘贴崩 UnicodeDecodeError 的回归测试。

真机 TTY 粘贴只能在 Linux 上手工验证；这里用**真管道 + 逐字节切开多字节字符**
把根因（两个读取者 + 逐块独立解码）钉死在测试里。
"""
import os
import sys

import pytest

from agent import input_reader


class _PipeStdin:
    """把管道读端包装成 stdin：只提供 fileno()/isatty()，不碰真实终端。"""

    def __init__(self, fd, is_tty=True):
        self._fd = fd
        self._is_tty = is_tty

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._is_tty


def _pipe(chunks, close_write=True):
    """把 chunks 依次写进管道，返回 (读端 fd, 写端 fd)。"""
    r, w = os.pipe()
    for chunk in chunks:
        os.write(w, chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    if close_write:
        os.close(w)
    return r, w


def test_multibyte_split_across_reads_is_not_corrupted():
    """根因回归：汉字被读取块从中间切开，也必须完整还原（以前逐块 decode 会留半截字节）。"""
    text = "中文字符串"
    raw = text.encode("utf-8")
    r, w = _pipe([raw[:1], raw[1:2], raw[2:] + b"\n"])

    reader = input_reader._StdinReader(_PipeStdin(r))
    try:
        assert reader.read_line() == text
    finally:
        os.close(r)


def test_paste_20_lines_becomes_one_goal_and_never_uses_input(monkeypatch):
    """粘贴 20+ 行中文 → 合并成一条多行输入；且读取路径里不能再出现 input()/readline。"""
    lines = ["第%d行：这是一段粘贴进来的中文内容" % i for i in range(1, 22)]
    r, _ = _pipe(["\n".join(lines) + "\n"])
    stdin = _PipeStdin(r, is_tty=True)

    called = []
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: called.append(1) or "不该走这里")

    try:
        goal = input_reader._read_goal_posix(stdin, lambda: None, lambda: None)
    finally:
        os.close(r)

    assert goal.split("\n") == lines
    assert called == [], "读取不能再走 input()——那正是与 os.read 抢同一个 stdin 的根源"


def test_emoji_and_rare_hanzi_byte_by_byte():
    """逐字节喂入 emoji（4 字节）与生僻汉字：跨块增量解码不能出错。"""
    text = "𠮷 emoji 🐍 与生僻字 龘"
    raw = text.encode("utf-8")
    chunks = [raw[i:i + 1] for i in range(len(raw) - 1)] + [raw[-1:] + b"\n"]
    r, _ = _pipe(chunks)

    reader = input_reader._StdinReader(_PipeStdin(r))
    try:
        assert reader.read_line() == text
    finally:
        os.close(r)


def test_read_goal_default_path_on_posix_uses_single_reader(monkeypatch):
    """POSIX 默认路径必须走单一读取者：把 input() 换成会炸的实现，仍应正常读到内容。"""
    monkeypatch.setattr(sys, "platform", "linux")
    r, _ = _pipe(["第一行中文\n第二行中文\n"])
    stdin = _PipeStdin(r, is_tty=True)
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不许用 input()")))
    try:
        goal = input_reader.read_goal(stdin=stdin)
    finally:
        os.close(r)
    assert goal == "第一行中文\n第二行中文"


def test_ctrl_c_during_paste_returns_none(monkeypatch):
    """粘贴进行中按 Ctrl+C：中断本次输入、回到主提示符，而不是崩溃。"""
    r, _ = _pipe(["第一行中文\n"], close_write=False)

    def _boom(self, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(input_reader._StdinReader, "_fill", _boom)
    try:
        assert input_reader._read_goal_posix(_PipeStdin(r), lambda: None, lambda: None) is None
    finally:
        os.close(r)


def test_repeated_paste_five_times_is_stable():
    """连做 5 次：确认没有残余字节影响下一轮输入。"""
    for round_no in range(5):
        lines = ["第%d轮第%d行" % (round_no + 1, i) for i in range(1, 22)]
        r, _ = _pipe(["\n".join(lines) + "\n"])
        try:
            goal = input_reader._read_goal_posix(_PipeStdin(r), lambda: None, lambda: None)
        finally:
            os.close(r)
        assert goal.split("\n") == lines


@pytest.mark.skipif(sys.platform == "win32",
                    reason="select 在 Windows 上不支持管道（该函数是 POSIX 专用）")
def test_legacy_drain_posix_decodes_incrementally():
    """旧排干函数也必须增量解码（管道半行场景仍在用它）。"""
    text = "中文尾巴"
    raw = text.encode("utf-8")
    r, _ = _pipe([raw[:2], raw[2:]])
    try:
        lines = input_reader._drain_posix(_PipeStdin(r))
    finally:
        os.close(r)
    assert "".join(lines) == text, "不得出现替换字符（U+FFFD）或半截字节"
    assert "\ufffd" not in "".join(lines)
