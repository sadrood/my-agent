"""
持久目标（GoalStore）测试：按会话持久化、隔离。
"""
import pytest

from agent.goal import GoalStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import agent.goal as mod
    monkeypatch.setattr(mod, "_GOAL_DIR", str(tmp_path / "goals"))
    return GoalStore()


def test_set_get(store):
    store.set("conv-a", "完成小说")
    assert store.get("conv-a") == "完成小说"


def test_session_isolation(store):
    store.set("conv-a", "目标A")
    assert store.get("conv-b") == ""
    store.set("conv-b", "目标B")
    assert store.get("conv-a") == "目标A"
    assert store.get("conv-b") == "目标B"


def test_overwrite(store):
    store.set("conv-a", "旧目标")
    store.set("conv-a", "新目标")
    assert store.get("conv-a") == "新目标"


def test_empty_key(store):
    store.set("", "x")
    assert store.get("") == ""
