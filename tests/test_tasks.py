"""
任务中心（TaskTool/ThoughtTool）测试：创建/流转/日志/想法、事件。
"""
import pytest

import agent.tasks as mod


@pytest.fixture()
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(mod, "_THOUGHTS_FILE", str(tmp_path / "thoughts.json"))


def _mk(isolate):
    t = mod.TaskTool()
    events = []
    t.set_emit(lambda ev, data: events.append((ev, data)))
    return t, events


def test_create_and_complete(isolate):
    t, events = _mk(isolate)
    r = t.execute_json({"operation": "create", "title": "调研项目"})
    assert r.success and "调研项目" in r.output
    tid = mod.load_tasks()[0]["id"]
    r2 = t.execute_json({"operation": "complete", "id": tid})
    assert r2.success and "done" in r2.output
    assert mod.load_tasks()[0]["status"] == "done"
    assert any(ev[0] == "tasks" for ev in events)


def test_status_and_log(isolate):
    t, _ = _mk(isolate)
    t.execute_json({"operation": "create", "title": "写报告"})
    tid = mod.load_tasks()[0]["id"]
    r = t.execute_json({"operation": "status", "id": tid, "status": "in_progress"})
    assert r.success and "in_progress" in r.output
    t.execute_json({"operation": "log", "id": tid, "content": "开始写"})
    assert mod.load_tasks()[0]["logs"][0].endswith("开始写")
    t.execute_json({"operation": "status", "id": tid, "status": "failed"})
    assert mod.load_tasks()[0]["status"] == "failed"


def test_invalid_status(isolate):
    t, _ = _mk(isolate)
    t.execute_json({"operation": "create", "title": "x"})
    tid = mod.load_tasks()[0]["id"]
    r = t.execute_json({"operation": "status", "id": tid, "status": "bogus"})
    assert not r.success


def test_thought_add(isolate):
    th = mod.ThoughtTool()
    events = []
    th.set_emit(lambda ev, data: events.append((ev, data)))
    r = th.execute_json({"operation": "add", "content": "一个灵感"})
    assert r.success
    assert mod.load_thoughts()[0]["content"] == "一个灵感"
    assert any(ev[0] == "thoughts" for ev in events)
