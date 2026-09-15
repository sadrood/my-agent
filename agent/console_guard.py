"""
控制台防卡保护（**仅旧版 conhost**；Windows Terminal 不受影响）。

症状：my-agent 跑着跑着"卡死"——进程活着、CPU 为 0、没有子进程也没有
网络连接，rollout 事件停在 tool_call。根因：**旧版 conhost** 处于 QuickEdit
文本选择状态时，进程写 stdout 会被永久阻塞（用户点选/误触窗口即可触发）；
Ctrl+S（XOFF）同样会暂停输出（Ctrl+Q 恢复，程序无法拦截）。

重要：Windows Terminal（WT_SESSION）等现代终端使用独立渲染器，选区
**不会**阻塞程序 stdout，因此无需禁用 QuickEdit——禁用只会让用户失去
鼠标选择/复制粘贴能力。本模块检测到 WT_SESSION 时直接跳过。

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


def is_modern_terminal() -> bool:
    """是否运行在现代终端（选区不会阻塞 stdout，无需禁用 QuickEdit）。

    判据（任一命中即可）：
    - WT_SESSION：Windows Terminal（含 VS Code / Windows 终端宿主）
    - TERM_PROGRAM=VSCode：VS Code 集成终端（基于 xterm.js，独立渲染）
    - ConEmuANSI=ON / TERM 存在：ConEmu、mintty（Git Bash）等
    """
    if os.environ.get("WT_SESSION"):
        return True
    if os.environ.get("TERM_PROGRAM") == "vscode":
        return True
    if os.environ.get("ConEmuANSI") == "ON":
        return True
    return False


def disable_quickedit_if_enabled() -> bool:
    """按需关闭 conhost QuickEdit，防止选区状态阻塞 stdout。返回是否生效。

    现代终端（Windows Terminal 等）直接跳过：那里选区不阻塞输出，
    禁用 QuickEdit 只会让用户无法用鼠标选择/复制。
    """
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
