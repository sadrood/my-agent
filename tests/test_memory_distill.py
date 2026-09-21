# -*- coding: utf-8 -*-
"""经验压缩（memory distill）测试：把零散记录总结成高层经验，**原始记录不丢**。

设计底线（干跑时踩到过真 bug）：
- 只有**真的提炼出条目**的类别才允许替换；解析失败/调用失败的类别必须原样保留
  （第一版按"尝试过的类别"替换，7 类里 4 类解析失败时 159 条会变成 5 条）；
- 归档失败就整个放弃压缩——压缩不能以丢历史为代价；
- dry-run 一个字节都不写。
"""
import json
import os
import shutil
import tempfile

import pytest

from agent.memory import Memory, ExperienceEntry


def _mem():
    tmp = tempfile.mkdtemp(prefix="mem_distill_")
    return Memory(db_path=tmp), tmp


def _add(m, goal, category, n=1):
    for i in range(n):
        m.experiences.append(ExperienceEntry(
            goal=f"{goal}{i}", plan_steps=["s"], success=True, total_steps=1,
            completed_steps=1, failed_steps=0, summary=f"{goal} 的摘要",
            task_category=category, tool_usage={"terminal": 1}, errors=[],
            timestamp="2026-09-20T10:00:00"))


class FakeLLM:
    """按类别返回脚本化输出；可指定某类别抛错。"""

    def __init__(self, script=None, boom=None):
        self.script = script or {}
        self.boom = boom or set()
        self.calls = []

    def chat(self, messages, **kw):
        user = messages[-1]["content"]
        self.calls.append(user)
        for cat in self.boom:
            if f"任务类别：{cat}" in user:
                raise RuntimeError("模拟限流 429")
        for cat, out in self.script.items():
            if f"任务类别：{cat}" in user:
                return out
        return '[{"title": "默认经验", "do": "默认做法", "dont": "默认坑"}]'


_OK = '[{"title": "当排查 profile 丢失时", "do": "把路径锚定到项目根", "dont": "别用相对路径", "evidence": "来自 5 条记录"}]'


class TestDistill:
    def test_successful_group_is_compressed(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 6)
        res = m.distill_experiences(FakeLLM(script={"coding": _OK}), min_group=3)
        assert res["before"] == 6 and res["after"] == 1
        assert m.experiences[0].goal == "当排查 profile 丢失时"
        assert m._is_distilled(m.experiences[0])
        assert "锚定到项目根" in m.experiences[0].summary
        shutil.rmtree(tmp)

    def test_small_group_is_untouched(self):
        m, tmp = _mem()
        _add(m, "小类别", "tiny", 2)
        res = m.distill_experiences(FakeLLM(), min_group=3)
        assert res["groups"] == [] and "无需压缩" in res.get("note", "")
        assert len(m.experiences) == 2
        shutil.rmtree(tmp)

    def test_unparseable_group_keeps_its_records(self):
        """回归：解析失败的类别**绝不能**被替换掉（曾把 159 条压成 5 条）。"""
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 5)
        _add(m, "general 闲聊", "general", 8)
        llm = FakeLLM(script={"coding": _OK, "general": "这些记录说明了要注意路径。"})
        res = m.distill_experiences(llm, min_group=3)
        assert res["after"] == 1 + 8, "general 的 8 条必须原样保留"
        assert sum(1 for e in m.experiences if e.task_category == "general") == 8
        assert "general" in res["error"] and "保持原样" in res["error"]
        shutil.rmtree(tmp)

    def test_llm_failure_keeps_that_group(self):
        m, tmp = _mem()
        _add(m, "a", "coding", 4)
        _add(m, "b", "general", 4)
        res = m.distill_experiences(FakeLLM(boom={"general"}), min_group=3)
        assert sum(1 for e in m.experiences if e.task_category == "general") == 4
        assert sum(1 for e in m.experiences if e.task_category == "coding") == 1
        assert "总结失败" in res["error"]
        shutil.rmtree(tmp)

    def test_raw_records_are_archived(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 5)
        res = m.distill_experiences(FakeLLM(), min_group=3)
        assert res["archived"] and os.path.isfile(res["archived"])
        with open(res["archived"], encoding="utf-8") as f:
            raw = json.load(f)
        assert len(raw) == 5, "归档必须是压缩前的**全部**原始记录"
        assert raw[0]["goal"].startswith("coding 任务")
        shutil.rmtree(tmp)

    def test_archive_failure_aborts_the_compression(self):
        """写不进归档就什么都别动（宁可没压成，也不能丢）。"""
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 5)

        def boom(name, data, chat_dir=None):
            if str(name).startswith("experiences_raw_"):
                raise OSError("磁盘满了")
            return Memory._save_json(m, name, data, chat_dir)

        m._save_json = boom
        res = m.distill_experiences(FakeLLM(), min_group=3)
        assert len(m.experiences) == 5, "归档失败后原库必须原封不动"
        assert "已放弃压缩" in res["error"]
        shutil.rmtree(tmp)

    def test_dry_run_writes_nothing(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 5)
        before_file = os.path.join(m.chat_db_path, "experiences.json")
        res = m.distill_experiences(FakeLLM(), min_group=3, dry_run=True)
        assert res["dry_run"] is True and res["preview"]
        assert len(m.experiences) == 5, "dry-run 不能改内存里的库"
        assert not os.path.exists(before_file), "dry-run 不该落盘"
        assert not res["archived"]
        shutil.rmtree(tmp)

    def test_secrets_are_not_written_into_distilled_text(self):
        """密钥必须被**替换掉**（只加一句"已省略"而留着原文等于没防）。"""
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        leak = ('[{"title": "配置 API", "do": "用 sk-abcdefghijklmnopqrstuvwxyz012345 '
                '作为 key", "dont": "别硬编码"}]')
        m.distill_experiences(FakeLLM(script={"coding": leak}), min_group=3)
        text = m.experiences[0].summary
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text
        assert "<已省略>" in text and "疑似密钥" in text
        shutil.rmtree(tmp)

    def test_stats_reports_distilled_vs_raw(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        _add(m, "另一个", "other", 1)
        m.distill_experiences(FakeLLM(), min_group=3)
        st = m.experience_stats()
        assert st["distilled"] == 1 and st["raw"] == 1 and st["total"] == 2
        shutil.rmtree(tmp)

    def test_covered_only_counts_successful_groups(self):
        """只有成功的类别会从原库消失——这条盯住上面那个数据丢失 bug 的根因。"""
        m, tmp = _mem()
        _add(m, "ok", "coding", 3)
        _add(m, "bad", "general", 3)
        m.distill_experiences(FakeLLM(script={"general": "不是 JSON"}), min_group=3)
        cats = [e.task_category for e in m.experiences]
        assert cats.count("coding") == 1 and cats.count("general") == 3
        shutil.rmtree(tmp)


class TestEmptyResponseHandling:
    """上游配额耗尽时除了 429 还会回**空正文**（实测商汤）：要重试一次，不是当"没经验"。"""

    class EmptyThenOk:
        def __init__(self, ok=_OK):
            self.ok = ok
            self.n = 0

        def chat(self, messages, **kw):
            self.n += 1
            return "" if self.n == 1 else self.ok

    class AlwaysEmpty:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kw):
            self.calls += 1
            return "   "

    def test_empty_then_content_succeeds(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        llm = self.EmptyThenOk()
        res = m.distill_experiences(llm, min_group=3)
        assert llm.n == 2 and res["after"] == 1
        assert "无法解析" not in res["error"]
        shutil.rmtree(tmp)

    def test_persistent_empty_keeps_records(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda *_: None)
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        llm = self.AlwaysEmpty()
        res = m.distill_experiences(llm, min_group=3)
        assert llm.calls == 2, "空正文应重试一次"
        assert len(m.experiences) == 4, "两次都空 → 原样保留"
        assert "空内容" in res["error"]
        shutil.rmtree(tmp)


class TestDistillModelSelection:
    """压缩可以用独立模型（离线维护任务，避开主模型限流）——但不动主循环。"""

    def test_defaults_to_main_llm(self, monkeypatch):
        from agent.memory import build_distill_llm
        monkeypatch.setitem(__import__("config").LEARN_CONFIG, "distill_model", "")
        sentinel = object()
        assert build_distill_llm(sentinel) is sentinel

    def test_builds_dedicated_model(self, monkeypatch):
        import config
        import models.llm as llm_mod
        from agent.memory import build_distill_llm

        seen = {}

        class FakeLLM:
            def __init__(self, api_key=None, base_url=None, model=None):
                seen.update({"key": api_key, "base": base_url, "model": model})

        monkeypatch.setattr(llm_mod, "LLM", FakeLLM)
        monkeypatch.setitem(config.LEARN_CONFIG, "distill_model", "agnes-3.0-flash")
        monkeypatch.setitem(config.LEARN_CONFIG, "distill_base_url", "https://a.example/v1")
        monkeypatch.setitem(config.LEARN_CONFIG, "distill_api_key", "k-1")
        build_distill_llm(object())
        assert seen == {"key": "k-1", "base": "https://a.example/v1",
                        "model": "agnes-3.0-flash"}

    def test_config_knobs_exist(self):
        from config import LEARN_CONFIG
        for k in ("distill_model", "distill_base_url", "distill_api_key"):
            assert k in LEARN_CONFIG


class TestMemoryTool:

    def _tool(self, m=None, llm=None):
        from tools.memory_tool import MemoryTool
        return MemoryTool(memory=m, llm=llm)

    def test_status_lists_library_state(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        out = self._tool(m).execute_json({"operation": "status"})
        assert out.success and "经验库" in out.output and "coding 4" in out.output
        shutil.rmtree(tmp)

    def test_distill_dry_run_via_tool(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        out = self._tool(m, FakeLLM()).execute_json({"operation": "distill", "dry_run": True})
        assert out.success and "预览" in out.output and "未写盘" in out.output
        assert out.metadata["dry_run"] is True
        assert len(m.experiences) == 4
        shutil.rmtree(tmp)

    def test_distill_requires_llm(self, monkeypatch):
        import tools.memory_tool as mt
        monkeypatch.setattr(mt.MemoryTool, "_get_llm", lambda self: None)
        m, tmp = _mem()
        out = self._tool(m).execute_json({"operation": "distill"})
        assert not out.success and "LLM" in out.error
        shutil.rmtree(tmp)

    def test_unknown_operation(self):
        out = self._tool(_mem()[0]).execute_json({"operation": "explode"})
        assert not out.success and "explode" in out.error

    def test_text_protocol(self):
        m, tmp = _mem()
        _add(m, "coding 任务", "coding", 4)
        tool = self._tool(m, FakeLLM())
        assert tool.execute("status").success
        assert tool.execute("distill --dry-run").metadata["dry_run"] is True
        assert tool.execute("").success        # 无参 = 帮助
        shutil.rmtree(tmp)

    def test_tool_is_registered_and_injectable(self):
        from tools.tool_manager import ToolManager
        tm = ToolManager()
        t = tm.get_tool("memory")
        assert t is not None and hasattr(t, "set_memory")
        sentinel = object()
        t.set_memory(sentinel)
        assert t._get_memory() is sentinel
