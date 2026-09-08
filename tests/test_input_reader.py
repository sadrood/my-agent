"""
agent/input_reader.py：多行粘贴读取 / 续行合并的单元测试。

不依赖真实终端：prompt 用脚本化 callable，排干用注入的 fake stdin/drain。
"""
import pytest

from agent import input_reader


class FakeStdin:
    """带 isatty 与 readline 的假输入流（POSIX 排干路径用）。"""

    def __init__(self, lines=None, is_tty=True):
        self._lines = list(lines or [])
        self.is_tty_val = is_tty

    def isatty(self):
        return self.is_tty_val

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class TestResolveContinuation:
    def test_trailing_backslash_merges(self):
        assert input_reader.resolve_continuation(["第一行\\", "第二行"]) == ["第一行\n第二行"]

    def test_escaped_backslash_is_literal(self):
        # C:\\ 这种 Windows 路径行尾（两个反斜杠）不应触发续行
        assert input_reader.resolve_continuation(["C:\\\\", "下一行"]) == ["C:\\\\", "下一行"]

    def test_multiple_blocks(self):
        assert input_reader.resolve_continuation(["a\\", "b", "c"]) == ["a\nb", "c"]

    def test_trailing_continuation_keeps_partial(self):
        assert input_reader.resolve_continuation(["a\\"]) == ["a"]


class TestReadGoal:
    def _read(self, scripted, extra=None, is_tty=True):
        prompts = iter(scripted)

        def prompt_primary():
            return next(prompts)

        def prompt_continuation():
            return next(prompts)

        stdin = FakeStdin(extra or [], is_tty=is_tty)

        def pending_fn():
            return bool(stdin._lines)

        def drain_fn():
            out, stdin._lines = stdin._lines, []
            return out

        return input_reader.read_goal(
            prompt_primary=prompt_primary,
            prompt_continuation=prompt_continuation,
            stdin=stdin, pending_fn=pending_fn, drain_fn=drain_fn,
        )

    def test_single_line(self):
        assert self._read(["写个测试"]) == "写个测试"

    def test_paste_multiline_joined(self):
        # 粘贴 "第一行\n第二行\n第三行"：第一行进主提示符，其余被排干合并
        assert self._read(["第一行"], extra=["第二行", "第三行"]) == "第一行\n第二行\n第三行"

    def test_continuation_with_backslash(self):
        assert self._read(["第一行\\", "第二行"]) == "第一行\n第二行"

    def test_empty_first_line_paste(self):
        # 粘贴以空行开头：空行不丢弃剩余内容
        assert self._read([""], extra=["第二行"]) == "第二行"

    def test_paste_with_continuation_char(self):
        # 粘贴内容本身以 \ 结尾：续行提示符直接读到缓冲里的下一行
        assert self._read(["第一行\\", "第二行"]) == "第一行\n第二行"

    def test_blank_input_stays_empty(self):
        assert self._read([""]) == ""

    def test_eof_returns_none(self):
        def raise_eof():
            raise EOFError

        goal = input_reader.read_goal(
            prompt_primary=raise_eof, prompt_continuation=raise_eof,
            stdin=FakeStdin(), pending_fn=lambda: False, drain_fn=lambda: [],
        )
        assert goal is None

    def test_keyboard_interrupt_returns_none(self):
        def raise_int():
            raise KeyboardInterrupt

        goal = input_reader.read_goal(
            prompt_primary=raise_int, prompt_continuation=raise_int,
            stdin=FakeStdin(), pending_fn=lambda: False, drain_fn=lambda: [],
        )
        assert goal is None

    def test_non_tty_no_drain(self):
        # 管道/CI：不排干，保持逐行行为
        assert self._read(["第一行"], extra=["第二行"], is_tty=False) == "第一行"

    def test_trailing_whitespace_stripped(self):
        assert self._read(["  你好  "]) == "你好"


class TestSplitLines:
    def test_crlf_and_cr(self):
        assert input_reader._split_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"]


class TestDrainWindows:
    """_drain_windows 的 Windows 控制台读取路径（monkeypatch ctypes/msvcrt）。"""

    @pytest.fixture
    def fake_console(self, monkeypatch):
        import sys as _sys
        import types as _types

        state = {"chars": [65], "hits": [True, False]}

        class FakeFn:
            """模拟 ctypes CDLL 函数：restype 被设置后按 ctypes 语义返回 str。"""

            def __init__(self):
                self.restype = None

            def __call__(self):
                if not state["chars"]:
                    return 0
                v = state["chars"].pop(0)
                if v == "ERR":
                    raise RuntimeError("fake console read failure")
                if self.restype is not None:
                    return chr(v)      # 旧代码设 restype=c_wchar 时的行为
                return v

        class FakeLib:
            def __init__(self):
                self._getwch = FakeFn()

        fake_ctypes = _types.ModuleType("ctypes")
        fake_ctypes.c_wchar = object()
        fake_ctypes.CDLL = lambda name: FakeLib()

        fake_msvcrt = _types.ModuleType("msvcrt")
        fake_msvcrt.kbhit = lambda: bool(state["hits"] and state["hits"].pop(0))
        fake_msvcrt.getwch = lambda: "X"

        monkeypatch.setitem(_sys.modules, "ctypes", fake_ctypes)
        monkeypatch.setitem(_sys.modules, "msvcrt", fake_msvcrt)
        return state

    def test_drain_returns_chars(self, fake_console, monkeypatch):
        # 回归：修复前 restype=c_wchar 使 _getwch 返回 str，chr(str) → TypeError
        import io
        monkeypatch.setattr("sys.stdout", io.StringIO())
        assert input_reader._drain_windows() == ["A"]

    def test_weof_treated_as_eof(self, fake_console):
        fake_console["chars"] = [0xFFFF]
        assert input_reader._drain_windows() == [""]

    def test_ctrl_c_propagates(self, fake_console):
        fake_console["chars"] = [3]
        with pytest.raises(KeyboardInterrupt):
            input_reader._drain_windows()

    def test_read_error_keeps_partial(self, fake_console, monkeypatch):
        # 读取中途出错：不崩溃，保留已收到的字符
        import io
        monkeypatch.setattr("sys.stdout", io.StringIO())
        fake_console["chars"] = [65, "ERR"]
        fake_console["hits"] = [True, True, False]
        assert input_reader._drain_windows() == ["A"]
