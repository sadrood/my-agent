"""
定时任务调度器（借鉴同类实现的周期任务）。

后台线程定期检查到期任务，通过 on_run(task) 回调执行（由 server 层跑 agent）。
任务持久化到 memory/scheduled_tasks.json。支持按分钟间隔（interval_minutes）。
"""
import json
import os

import threading
import time

from config import resolve_under_root

# 锚定项目根：裸相对路径按 **cwd** 解析，从别的目录启动时定时任务会写到启动目录，
# 用户看到的是"定时任务凭空消失"。同仓库 tasks.py/session.py/todo.py 都已用
# resolve_under_root 收敛过这个坑（tests/test_path_anchoring.py 就是为它写的），
# 调度器是漏网的那个（2026-09-22 审计）。
_SCHED_FILE = resolve_under_root(os.path.join("memory", "scheduled_tasks.json"))
_lock = threading.Lock()
_stop = threading.Event()
_thread = None


def _load() -> list:
    global _tasks
    try:
        with open(_SCHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(tasks: list) -> None:
    os.makedirs(os.path.dirname(_SCHED_FILE), exist_ok=True)
    with open(_SCHED_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def list_tasks() -> list:
    with _lock:
        return _load()


def add_task(goal: str, interval_minutes: int = 10, runtime: str = "myagent") -> dict:
    interval = max(1, int(interval_minutes or 0))
    with _lock:
        tasks = _load()
        task = {
            "id": f"sch{int(time.time() * 1000)}{len(tasks)}",
            "goal": goal[:500], "runtime": runtime or "myagent",
            "interval_minutes": interval,
            "last_run": None,
            "next_run": time.time() + interval * 60,
        }
        tasks.append(task)
        _save(tasks)
    return task


def remove_task(tid: str) -> bool:
    with _lock:
        tasks = _load()
        before = len(tasks)
        tasks = [t for t in tasks if t.get("id") != tid]
        if len(tasks) != before:
            _save(tasks)
            return True
    return False


def _tick():
    """检查到期任务：更新 next_run 并 yield 到期任务（持锁内完成持久化）。"""
    with _lock:
        tasks = _load()
        now = time.time()
        due = []
        changed = False
        for t in tasks:
            if t.get("next_run") and now >= t["next_run"]:
                t["last_run"] = now
                t["next_run"] = now + int(t.get("interval_minutes", 10)) * 60
                due.append(t)
                changed = True
        if changed:
            _save(tasks)
    return due


def start(on_run) -> None:
    """启动调度线程。到期任务在后台线程回调 on_run(task)。"""
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return

    def _loop():
        while not _stop.is_set():
            try:
                for t in _tick():
                    try:
                        on_run(t)
                    except Exception:
                        pass
            except Exception:
                pass
            _stop.wait(30)

    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop() -> None:
    """停止调度线程。

    必须 clear()：`start()` 里的循环条件是 `while not _stop.is_set()`，只 set 不清
    会让下一次 start() 起来的线程立刻退出、`_thread` 记下一个死线程，之后所有
    start() 都被"已在运行"挡掉 —— 定时任务再也不触发（2026-09-22 审计）。
    """
    global _thread
    _stop.set()
    t = _thread
    _thread = None
    if t is not None and t.is_alive():
        try:
            t.join(timeout=5)
        except Exception:
            pass
