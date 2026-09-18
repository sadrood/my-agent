"""
TodoTool 测试：会话级待办清单（add/done/delete/clear/list、会话隔离、事件）。
"""
import pytest

from tools.todo import TodoTool


@pytest.fixture()
def tool(tmp_path, monkeypatch):
    import tools.todo as mod
    monkeypatch.setattr(mod, "_TODO_DIR", str(tmp_path / "todos"))
    t = TodoTool()
    t.set_session_key("conv-test-001")
    return t


def test_add_done_list(tool):
    r = tool.execute_json({"operation": "add", "title": "写代码"})
    assert r.success
    assert "写代码" in r.output

    r2 = tool.execute_json({"operation": "add", "title": "写测试"})
    assert r2.success
    assert "待办清单（2 项）" in r2.output

    tid = _first_id(tool)
    r3 = tool.execute_json({"operation": "done", "id": tid})
    assert r3.success
    assert "✓" in r3.output


def _first_id(tool):
    import json, os
    from tools.todo import _TODO_DIR
    path = os.path.join(_TODO_DIR, "conv-test-001.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)[0]["id"]


def test_delete_and_clear(tool):
    tool.execute_json({"operation": "add", "title": "a"})
    tool.execute_json({"operation": "add", "title": "b"})
    tid = _first_id(tool)
    r = tool.execute_json({"operation": "delete", "id": tid})
    assert r.success
    assert "待办清单（1 项）" in r.output

    r2 = tool.execute_json({"operation": "clear"})
    assert r2.success
    assert "已清空" in r2.output

    r3 = tool.execute_json({"operation": "list"})
    assert "为空" in r3.output


def test_session_isolation(tmp_path, monkeypatch):
    import tools.todo as mod
    monkeypatch.setattr(mod, "_TODO_DIR", str(tmp_path / "todos"))
    t1 = TodoTool(); t1.set_session_key("conv-a")
    t2 = TodoTool(); t2.set_session_key("conv-b")
    t1.execute_json({"operation": "add", "title": "会话A的事"})
    r2 = t2.execute_json({"operation": "list"})
    assert "为空" in r2.output   # 会话 B 看不到 A 的清单


def test_emits_todo_event(tmp_path, monkeypatch):
    import tools.todo as mod
    monkeypatch.setattr(mod, "_TODO_DIR", str(tmp_path / "todos"))
    t = TodoTool()
    t.set_session_key("conv-evt")
    events = []
    t.set_emit(lambda ev, data: events.append((ev, data)))
    t.execute_json({"operation": "add", "title": "x"})
    assert events and events[0][0] == "todo"
    assert events[0][1]["todos"][0]["title"] == "x"


def test_todo_dir_is_anchored_to_project_root():
    """清单目录必须是绝对路径。

    原实现是硬编码相对路径 `memory/todos`，open() 按 cwd 解析——agent 在
    output/qici_toonflow_ep1 下跑时清单就落到那儿去了，本机实测同一个会话的
    待办被拆成三份（memory/todos、output/memory/todos、output/qici_.../memory/todos）。
    """
    import os

    import tools.todo as mod
    from config import resolve_under_root

    assert os.path.isabs(mod._TODO_DIR)
    assert mod._TODO_DIR == resolve_under_root(os.path.join("memory", "todos"))


def test_pending_lists_only_unfinished(tool):
    """完成度闸门依赖它判断"还有活没干完"。"""
    tool.execute_json({"operation": "add", "title": "还没做的"})
    tool.execute_json({"operation": "add", "title": "已经做的"})
    import json
    import os

    import tools.todo as mod
    with open(os.path.join(mod._TODO_DIR, "conv-test-001.json"), encoding="utf-8") as f:
        todos = json.load(f)
    tool.execute_json({"operation": "done", "id": todos[1]["id"]})

    assert [t["title"] for t in tool.pending()] == ["还没做的"]


def test_pending_is_empty_without_any_list(tool):
    assert tool.pending() == []
