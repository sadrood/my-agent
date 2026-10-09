"""输出闸门：提示符正在等待输入时，别让别的线程把用户那一行覆盖掉。

输入是逐字符读取 + 手动回显，任何并发写都会把光标打到用户正在敲的那一行上
（表现为"刚输入的内容被替换/跑到前面去"）。所以提示符期间把输出**排队**，
用户按下回车后再按原顺序放出来。由 `agent.ui_theme.get_console` 统一拦截。
"""
import threading
from typing import List, Tuple

_LOCK = threading.RLock()
#: 提示符层数：**必须进程级**——写日志的是别的线程，thread-local 它们看不见
_DEPTH = 0
_QUEUE: List[Tuple[str, tuple, dict]] = []


def is_prompt_active() -> bool:
    return _DEPTH > 0


def begin_prompt() -> None:
    """进入提示符等待状态（可重入计数）。"""
    global _DEPTH
    with _LOCK:
        _DEPTH += 1


def end_prompt() -> int:
    """退出提示符等待状态；返回仍未释放的层数（0 表示已退出）。"""
    global _DEPTH
    with _LOCK:
        _DEPTH = max(0, _DEPTH - 1)
        return _DEPTH


def defer(text: str, args: tuple = (), kwargs: dict = None) -> None:
    """把一次输出排队（提示符结束后统一放出来）。"""
    with _LOCK:
        _QUEUE.append((str(text), tuple(args or ()), dict(kwargs or {})))


def pending() -> int:
    with _LOCK:
        return len(_QUEUE)


def drain() -> list:
    """取出并清空队列（返回 [(text, args, kwargs)]，调用方负责真正打印）。"""
    with _LOCK:
        items = list(_QUEUE)
        _QUEUE.clear()
        return items


class DeferringConsole:
    """提示符期间替身：print 排队，其余属性转发给真 console（status/input 等照旧）。"""

    def __init__(self, real):
        self._real = real

    def print(self, *args, **kwargs) -> None:
        text = " ".join(str(a) for a in args)
        defer(text, args, kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)
