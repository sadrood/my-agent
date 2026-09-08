"""
控制台防卡保护（Windows conhost）。

症状：my-agent 跑着跑着"卡死"——进程活着、CPU 为 0、没有子进程也没有
网络连接，rollout 事件停在 tool_call。根因：conhost 处于 QuickEdit
文本选择状态时，进程写 stdout 会被永久阻塞（用户点选/误触窗口即可触发）；
Ctrl+S（XOFF）同样会暂停输出（Ctrl+Q 恢复，程序无法拦截）。

本模块在启动时按需关闭 QuickEdit 模式（保留回显、行输入与 VT 处理），
退出时恢复原控制台模式。仅 Windows 且环境变量
MY_AGENT_DISABLE_QUICKEDIT=true 时启用。

注意：ENABLE_QUICK_EDIT_MODE 属于**输入**控制台模式，必须设置到
**输入句柄**（STD_INPUT_HANDLE）上；设置到输出句柄无效
（SetConsoleMode 对输出句柄不接受该标志、静默失败），这会留下
QuickEdit 仍开启的假象，选区卡死依旧会发生。
"""
import atexit
import os

try:
    import ctypes
except ImportError:  # pragma: no cover - 非 Windows 平台
    ctypes = None

ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080  # 关闭 QuickEdit 时需一并置位
STD_INPUT_HANDLE = -10


def disable_quickedit_if_enabled() -> bool:
    """按需关闭 conhost QuickEdit，防止选区状态阻塞 stdout。返回是否生效。"""
    if os.name != "nt" or ctypes is None:
        return False
    if os.getenv("MY_AGENT_DISABLE_QUICKEDIT", "").lower() not in ("1", "true", "yes"):
        return False
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
