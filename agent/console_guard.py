"""控制台防卡保护（**仅旧版 conhost**；Windows Terminal 不受影响）。
Ctrl+S（XOFF）同样会暂停输出（Ctrl+Q 恢复，程序无法拦截）。"""
import atexit
import os

try:
    import ctypes
except ImportError:  # pragma: no cover - 非 Windows 平台
    ctypes = None

ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080  # 关闭 QuickEdit 时需一并置位
STD_INPUT_HANDLE = -10


def is_modern_terminal() -> bool:
    """是否运行在现代终端（选区不会阻塞 stdout，无需禁用 QuickEdit）。"""
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("TERM_PROGRAM") == "vscode":
        return True
    if os.environ.get("ConEmuANSI") == "ON":
        return True
    return False


def disable_quickedit_if_enabled() -> bool:
    """按需关闭 conhost QuickEdit，防止选区状态阻塞 stdout。返回是否生效。
    禁用 QuickEdit 只会让用户无法用鼠标选择/复制。"""
    if os.name != "nt" or ctypes is None:
        return False
    if os.getenv("MY_AGENT_DISABLE_QUICKEDIT", "").lower() not in ("1", "true", "yes"):
        return False
    if is_modern_terminal():
        return False   # 现代终端无需处理，保留鼠标选择/复制能力
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle in (-1, 0):   # 无控制台（管道/重定向）
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        original = mode.value
        if not (original & ENABLE_QUICK_EDIT_MODE):
            return False  # QuickEdit 本来就没开
        # 关闭 QuickEdit（需同时置 ENABLE_EXTENDED_FLAGS），保留其余输入模式
        new_mode = (original & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        if not kernel32.SetConsoleMode(handle, new_mode):
            return False
        atexit.register(kernel32.SetConsoleMode, handle, original)
        return True
    except Exception:
        return False
