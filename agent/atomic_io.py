"""原子落盘：临时文件 + os.replace，中断/并发都不留半截文件。

memory / session / context_store 原先各写一份，这里统一到一处，避免实现漂移
（例如某一份忘了 fsync、或临时名不唯一导致并发写互相截断）。
"""
import contextlib
import json
import os
import threading
import time
from typing import Any, Optional


def _replace_with_retry(tmp: str, path: str, retries: int) -> None:
    """os.replace：Windows 上目标被占用会抛 PermissionError，重试几次。"""
    last: Optional[OSError] = None
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.05 * (attempt + 1))
    raise last if last else OSError(f"写入失败: {path}")


@contextlib.contextmanager
def _atomic(tmp: str, path: str, lock: Optional[threading.Lock], retries: int):
    """写好临时文件并原子替换；任何失败都清掉临时文件后原样抛出。"""
    with (lock or contextlib.nullcontext()):
        try:
            yield tmp
            _replace_with_retry(tmp, path, retries)
        except BaseException:
            # 必须捕 BaseException：真实的中断是 Ctrl+C（KeyboardInterrupt），
            # 它不是 Exception 的子类——只捕 Exception 会把临时文件留在数据目录里。
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise


def _tmp_name(path: str) -> str:
    # 临时名必须**每个写入者唯一**：多个会话/进程会同时写同一目录，
    # 固定名会让它们共用同一个 .tmp，互相截断成半截内容。
    return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def atomic_write_json(path: str, data: Any, *, compact: bool = True,
                      indent: int = 2, lock: Optional[threading.Lock] = None,
                      retries: int = 8) -> None:
    """原子写 JSON。compact=True 用紧凑分隔符（会话文件体积/耗时更小），否则按 indent 美化。"""
    _ensure_parent(path)
    tmp = _tmp_name(path)
    with _atomic(tmp, path, lock, retries) as target:
        with open(target, "w", encoding="utf-8") as f:
            if compact:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())


def atomic_write_text(path: str, text: str, *,
                      lock: Optional[threading.Lock] = None,
                      retries: int = 8) -> None:
    """原子写文本（避免读到/留下半截内容）。"""
    _ensure_parent(path)
    tmp = _tmp_name(path)
    with _atomic(tmp, path, lock, retries) as target:
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
