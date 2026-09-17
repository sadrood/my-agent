"""
批次2 回归：记忆持久化的原子性与损坏可见性。

实测故障（2026-09-17 审计）：`Memory._save_json` 直接 `open(path,"w")` 就地
截断，而 `_load_json` 解析失败时静默 `return []`。把 memory.json 截断一半后
`Memory()` 会加载到 0 条**且不报错**，下一次 `remember()` 只留下新条目——
长期记忆/经验库静默全丢。agent/session.py 早就是原子写，这里补齐。

存储布局：Memory(db_path=X) → 数据落在 X/default/（chat_db_path）。
"""
import json
from pathlib import Path

import pytest

from agent.memory import Memory


@pytest.fixture
def mem(tmp_path):
    return Memory(db_path=str(tmp_path))


def store(mem) -> Path:
    return Path(mem.chat_db_path)


def test_save_is_atomic_no_tmp_left(mem):
    mem.remember("要活下来的内容", role="user")
    p = store(mem) / "memory.json"
    assert p.exists()
    assert not list(store(mem).glob("*.tmp")), "原子写不应残留临时文件"
    assert json.loads(p.read_text(encoding="utf-8"))


def test_failed_write_keeps_original(mem, monkeypatch):
    """写入中途失败（模拟 Ctrl+C/断电）不得破坏原文件。"""
    mem.remember("原始内容", role="user")
    p = store(mem) / "memory.json"
    before = p.read_text(encoding="utf-8")

    def boom(*a, **kw):
        raise KeyboardInterrupt("模拟写入中断")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(BaseException):
        mem.remember("新内容", role="user")

    assert p.read_text(encoding="utf-8") == before, "原文件被写坏了"
    assert not list(store(mem).glob("*.tmp")), "失败后应清掉临时文件"


def test_corrupt_file_is_preserved_and_warned(mem, capsys):
    """损坏的 JSON 必须保留现场并告警，而不是静默清空。"""
    p = store(mem) / "memory.json"
    p.write_text('{"broken": ', encoding="utf-8")

    m = Memory(db_path=str(store(mem).parent))
    assert m.long_term_memory == []
    out = capsys.readouterr().out
    assert "警告" in out and "memory.json" in out
    assert (store(mem) / "memory.json.corrupt").exists(), "损坏文件应改名保留"


def test_corrupt_backup_survives_next_save(mem):
    """损坏现场的副本不能被后续写入覆盖掉。"""
    p = store(mem) / "memory.json"
    p.write_text("not json at all", encoding="utf-8")

    m = Memory(db_path=str(store(mem).parent))
    m.remember("新的一条", role="user")

    backup = store(mem) / "memory.json.corrupt"
    assert backup.read_text(encoding="utf-8") == "not json at all"


def test_experiences_roundtrip(mem):
    """正常路径不受影响：存了还能读回来。"""
    mem.save_experience(goal="做个测试", plan_steps=["a"], success=True,
                        summary="完成", tool_usage={"terminal": 1}, errors=[])
    again = Memory(db_path=str(store(mem).parent))
    assert any("做个测试" in e.goal for e in again.experiences)
