"""
定时任务调度器测试：添加/移除/到期触发。
"""
import time

import pytest

import agent.scheduler as mod


@pytest.fixture()
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_SCHED_FILE", str(tmp_path / "scheduled.json"))
    yield


def test_add_and_list(isolate):
    t = mod.add_task("定时任务A", 5, "myagent")
    assert t["interval_minutes"] == 5
    assert len(mod.list_tasks()) == 1
    assert mod.list_tasks()[0]["goal"] == "定时任务A"


def test_remove(isolate):
    t = mod.add_task("x", 5)
    assert mod.remove_task(t["id"]) is True
    assert mod.list_tasks() == []


def test_add_min_interval(isolate):
    t = mod.add_task("y", 0)
    assert t["interval_minutes"] == 1   # 下限 1 分钟


def test_tick_triggers_due(isolate):
    """next_run 已到期的任务触发 on_run，并重置 next_run。"""
    t = mod.add_task("到期任务", 5)
    t["next_run"] = time.time() - 1   # 已到期
    with mod._lock:
        tasks = mod._load()
        tasks[0] = t
        mod._save(tasks)

    runs = []
    due = mod._tick()   # _tick 内部持锁，外层不要再持
    assert len(due) == 1
    assert due[0]["goal"] == "到期任务"
    # next_run 已推进
    assert mod.list_tasks()[0]["next_run"] > time.time()


def test_restart_after_stop(isolate):
    """stop() 后再 start() 必须真的起新线程（旧实现 _stop 未清 → 新线程立刻退出，重启变空操作）。"""
    mod.stop()
    try:
        mod.start(lambda t: None)
        time.sleep(0.1)               # 留出时间：若 _stop 未清，线程会在毫秒内自行退出
        assert mod._thread is not None and mod._thread.is_alive()
        mod.stop()
        assert mod._thread is None

        mod.start(lambda t: None)     # 第二次启动：若 _stop 未清，线程会立即退出
        time.sleep(0.1)
        assert mod._thread is not None and mod._thread.is_alive()
    finally:
        mod.stop()
