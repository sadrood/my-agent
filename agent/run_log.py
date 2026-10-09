"""运行日志：把进程级生命周期写进 `logs/run-<时间>.log`，供"终端突然关闭"这类事后排查。

事件流（rollout JSONL）记的是对话与工具；这里补的是**进程级**信息：启动环境、
未捕获异常（含子线程）、退出原因、以及控制台被关闭的记录。
不使用替换 `sys.stdout` 的 tee：rich 在导入时就持有流引用，替换后富文本输出不会进日志。
"""
import atexit
import os
import sys
import threading
import time
import traceback
from typing import Optional

_LOCK = threading.RLock()
_PATH = ""
_STARTED = 0.0
_FH = None


def _root() -> str:
    from config import PROJECT_ROOT
    return PROJECT_ROOT


def log_dir() -> str:
    from config import RUN_LOG_CONFIG
    raw = str(RUN_LOG_CONFIG.get("dir") or "logs")
    return raw if os.path.isabs(raw) else os.path.join(_root(), raw)


def current_path() -> str:
    return _PATH


def enabled() -> bool:
    try:
        from config import RUN_LOG_CONFIG
        return bool(RUN_LOG_CONFIG.get("enabled", True))
    except Exception:                                # noqa: BLE001
        return True


def log(message: str, level: str = "info") -> None:
    """写一行（未启动时静默：日志不该影响主流程）。"""
    if _FH is None:
        return
    try:
        with _LOCK:
            _FH.write(f"{time.strftime('%H:%M:%S')} [{level}] {message}\n")
            _FH.flush()
    except Exception:                                # noqa: BLE001
        pass


def _install_hooks() -> None:
    previous = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        """未捕获异常：这是"终端自己关闭"最常见的原因，必须留在日志里。"""
        log("未捕获异常：" + "".join(traceback.format_exception(exc_type, exc, tb)), "fatal")
        try:
            previous(exc_type, exc, tb)
        except Exception:                            # noqa: BLE001
            pass

    sys.excepthook = _excepthook

    def _thread_hook(args):
        """子线程崩溃默认只打印一行、且不终止进程：记全栈便于复现。"""
        log(f"线程 {getattr(args.thread, 'name', '?')} 崩溃："
            + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            "error")

    try:
        threading.excepthook = _thread_hook
    except Exception:                                # noqa: BLE001
        pass


def start(tag: str = "", path: Optional[str] = None, extra: Optional[dict] = None) -> str:
    """启动运行日志（重复调用直接返回已有路径）。"""
    global _PATH, _STARTED, _FH
    if _PATH and _FH is not None:
        return _PATH
    if not enabled() and not path:
        return ""
    target = path or os.path.join(
        log_dir(), f"run-{time.strftime('%Y%m%d-%H%M%S')}{('-' + tag) if tag else ''}.log")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _FH = open(target, "a", encoding="utf-8", buffering=1)
    except OSError:
        _FH = None
        return ""
    _PATH, _STARTED = target, time.time()
    _install_hooks()
    atexit.register(finish, "正常退出/解释器关闭")
    log(f"启动 pid={os.getpid()} cwd={os.getcwd()} argv={' '.join(sys.argv[1:])[:200]}")
    for key, value in (extra or {}).items():
        log(f"{key}={value}")
    return target


def finish(reason: str = "") -> None:
    """记一条退出原因并关闭文件（可重复调用）。"""
    global _FH
    if _FH is None:
        return
    log(f"退出：{reason or '未注明'}（运行 {time.time() - _STARTED:.0f}s）")
    try:
        with _LOCK:
            _FH.close()
    except Exception:                                # noqa: BLE001
        pass
    _FH = None
