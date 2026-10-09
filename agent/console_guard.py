"""控制台防卡保护（**仅旧版 conhost**；Windows Terminal 不受影响）。
Ctrl+S（XOFF）同样会暂停输出（Ctrl+Q 恢复，程序无法拦截）。

另外负责**关窗兜底**：Windows 关闭控制台时进程只剩约 5 秒，`atexit` 不一定跑得到，
所以注册 `SetConsoleCtrlHandler`，在这个窗口里把会话与日志刷盘（见 register_exit_hook）。
"""
import atexit
import os
import time

try:
    import ctypes
except ImportError:  # pragma: no cover - 非 Windows 平台
    ctypes = None

ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080  # 关闭 QuickEdit 时需一并置位
STD_INPUT_HANDLE = -10

#: SetConsoleCtrlHandler 的事件号
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

#: 需要抢时间刷盘的事件（Ctrl+C 不在其中：那个交给 Python 的 KeyboardInterrupt）
FLUSH_EVENTS = {
    CTRL_CLOSE_EVENT: "控制台被关闭",
    CTRL_LOGOFF_EVENT: "用户注销",
    CTRL_SHUTDOWN_EVENT: "系统关机",
}

_EXIT_HOOKS: list = []
_HANDLER_REF = None           # 必须持有引用：否则回调被 GC 后系统会崩溃
_INSTALLED = False
_HOOK_BUDGET_SECONDS = 4.0    # 关窗只有约 5 秒，留一点余量


def register_exit_hook(fn, name: str = "") -> None:
    """注册"进程即将消失"时要跑的回调（幂等；按注册顺序执行）。"""
    if not callable(fn):
        return
    for existing, _ in _EXIT_HOOKS:
        if existing == fn:
            return
    _EXIT_HOOKS.append((fn, name or getattr(fn, "__qualname__", "hook")))


def run_exit_hooks(reason: str = "", budget: float = _HOOK_BUDGET_SECONDS) -> list:
    """执行所有退出回调，返回 [(名字, 是否成功)]；单个失败不影响其它。"""
    results = []
    deadline = time.time() + max(0.5, float(budget))
    for hook, name in list(_EXIT_HOOKS):
        if time.time() > deadline:
            results.append((name, False))
            continue
        try:
            hook(reason)
            results.append((name, True))
        except Exception:                            # noqa: BLE001
            results.append((name, False))
    return results


def _console_handler(event: int) -> bool:
    """控制台事件回调：只接管"进程要没了"的那几种，Ctrl+C 原样放行。"""
    if event in FLUSH_EVENTS:
        reason = FLUSH_EVENTS[event]
        try:
            from agent import run_log
            run_log.log(f"收到控制台事件：{reason}（开始刷盘）", "warn")
        except Exception:                            # noqa: BLE001
            pass
        run_exit_hooks(reason)
        try:
            from agent import run_log
            run_log.finish(reason)
        except Exception:                            # noqa: BLE001
            pass
        return True                                  # 已处理：别再等默认处理
    return False                                     # Ctrl+C / Ctrl+Break 交给 Python


def install_close_handler() -> bool:
    """安装关窗兜底（仅 Windows；重复调用只装一次）。返回是否已装。"""
    global _HANDLER_REF, _INSTALLED
    if os.name != "nt" or ctypes is None:
        atexit.register(lambda: run_exit_hooks("进程退出"))
        return False
    if _INSTALLED:
        return True
    try:
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        _HANDLER_REF = proto(_console_handler)
        kernel32 = ctypes.windll.kernel32
        if not kernel32.SetConsoleCtrlHandler(_HANDLER_REF, True):
            return False
        _INSTALLED = True
        atexit.register(lambda: run_exit_hooks("进程退出"))
        return True
    except Exception:                                # noqa: BLE001
        return False


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
