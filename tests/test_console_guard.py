"""
agent/console_guard.py 的门控逻辑测试（不触碰真实控制台）。
"""
import pytest

from agent import console_guard


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MY_AGENT_DISABLE_QUICKEDIT", raising=False)
    assert console_guard.disable_quickedit_if_enabled() is False


def test_disabled_when_false(monkeypatch):
    monkeypatch.setenv("MY_AGENT_DISABLE_QUICKEDIT", "false")
    assert console_guard.disable_quickedit_if_enabled() is False


def test_disabled_when_env_set_but_no_console(monkeypatch):
    # 环境变量开、但 stdout 不是控制台（管道/重定向）→ 安全返回 False
    monkeypatch.setenv("MY_AGENT_DISABLE_QUICKEDIT", "true")
    monkeypatch.setattr(console_guard.os, "name", "posix")  # 模拟非 Windows
    assert console_guard.disable_quickedit_if_enabled() is False


class _FakeKernel32:
    """可脚本化的 kernel32：记录调用、返回预设值。"""

    def __init__(self, input_mode=0x0040 | 0x0001):
        self.calls = []
        self.input_mode = input_mode
        self.set_result = True

    def GetStdHandle(self, n):
        self.calls.append(("GetStdHandle", n))
        return 0x10  # 非空句柄

    def GetConsoleMode(self, handle, byref):
        self.calls.append(("GetConsoleMode", handle))
        byref.value = self.input_mode
        return True

    def SetConsoleMode(self, handle, mode):
        self.calls.append(("SetConsoleMode", handle, mode))
        return self.set_result


class _FakeCtypes:
    def __init__(self, kernel32):
        self.windll = type("windll", (), {"kernel32": kernel32})()
        self._box = _Box()

    def c_uint32(self):
        return self._box

    def byref(self, obj):
        return obj


class _Box:
    value = 0


@pytest.fixture
def fake_win32(monkeypatch):
    """模拟**旧版 conhost**：env 开启 + os.name=nt + 假 ctypes + 无现代终端标记。"""
    monkeypatch.setenv("MY_AGENT_DISABLE_QUICKEDIT", "true")
    monkeypatch.setattr(console_guard.os, "name", "nt")
    # 关键：清除现代终端标记，否则会被跳过（见 TestModernTerminalSkip）
    for k in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI"):
        monkeypatch.delenv(k, raising=False)
    fake = _FakeKernel32()
    monkeypatch.setattr(console_guard, "ctypes", _FakeCtypes(fake))
    return fake


def test_uses_input_handle_not_output_handle(fake_win32):
    # 回归：QuickEdit 是输入模式标志，必须设置在 STD_INPUT_HANDLE(-10) 上。
    # 旧实现误用 STD_OUTPUT_HANDLE(-11)，导致 QuickEdit 从未真正关闭。
    assert console_guard.disable_quickedit_if_enabled() is True
    handles = [c[1] for c in fake_win32.calls if c[0] == "GetStdHandle"]
    assert handles == [console_guard.STD_INPUT_HANDLE]
    assert console_guard.STD_INPUT_HANDLE == -10


def test_clears_quickedit_bit_and_sets_extended_flags(fake_win32):
    assert console_guard.disable_quickedit_if_enabled() is True
    set_call = [c for c in fake_win32.calls if c[0] == "SetConsoleMode"][0]
    mode = set_call[2]
    assert not (mode & console_guard.ENABLE_QUICK_EDIT_MODE)
    assert mode & console_guard.ENABLE_EXTENDED_FLAGS
    # 其余输入模式位（如 0x0001 回显/行输入）保留
    assert mode & 0x0001


def test_noop_when_quickedit_already_off(fake_win32):
    fake_win32.input_mode = 0x0001  # 无 QuickEdit
    assert console_guard.disable_quickedit_if_enabled() is False
    assert not [c for c in fake_win32.calls if c[0] == "SetConsoleMode"]


def test_false_when_setconsolemode_fails(fake_win32):
    fake_win32.set_result = False
    assert console_guard.disable_quickedit_if_enabled() is False


class TestModernTerminalSkip:
    """现代终端（Windows Terminal 等）用独立渲染器，选区不会阻塞 stdout，
    因此不应禁用 QuickEdit——禁用只会让用户失去鼠标选择/复制粘贴能力。"""

    @pytest.mark.parametrize("env_key,env_val", [
        ("WT_SESSION", "22c162a7-0000-0000-0000-000000000000"),
        ("TERM_PROGRAM", "vscode"),
        ("ConEmuANSI", "ON"),
    ])
    def test_skips_in_modern_terminal(self, monkeypatch, env_key, env_val):
        monkeypatch.setenv("MY_AGENT_DISABLE_QUICKEDIT", "true")
        monkeypatch.setattr(console_guard.os, "name", "nt")
        for k in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(env_key, env_val)
        fake = _FakeKernel32()
        monkeypatch.setattr(console_guard, "ctypes", _FakeCtypes(fake))

        assert console_guard.disable_quickedit_if_enabled() is False
        # 完全没触碰控制台模式（保留用户的鼠标选择能力）
        assert not [c for c in fake.calls if c[0] == "SetConsoleMode"]

    def test_is_modern_terminal_detection(self, monkeypatch):
        for k in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI"):
            monkeypatch.delenv(k, raising=False)
        assert console_guard.is_modern_terminal() is False

        monkeypatch.setenv("WT_SESSION", "x")
        assert console_guard.is_modern_terminal() is True

        monkeypatch.delenv("WT_SESSION")
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        assert console_guard.is_modern_terminal() is True

        monkeypatch.delenv("TERM_PROGRAM")
        monkeypatch.setenv("ConEmuANSI", "ON")
        assert console_guard.is_modern_terminal() is True

        monkeypatch.setenv("ConEmuANSI", "OFF")
        assert console_guard.is_modern_terminal() is False
