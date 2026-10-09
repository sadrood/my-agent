"""零驻留上下文：大负载只留确定性指针，原文可按句柄逐字取回；压缩不再调模型。"""
import os

import pytest

from agent import context_store as store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "store_root", lambda: str(tmp_path / "ctx"))
    from config import CONTEXT_STORE_CONFIG
    monkeypatch.setitem(CONTEXT_STORE_CONFIG, "enabled", True)
    monkeypatch.setitem(CONTEXT_STORE_CONFIG, "threshold_chars", 200)
    monkeypatch.setitem(CONTEXT_STORE_CONFIG, "compact_mode", "pointer")
    yield


class TestStore:
    def test_handle_round_trip_and_exact_recall(self):
        handle = store.handle_for("run-1-001", 7)
        assert handle == "run-1-001-0007"
        payload = "第一行摘要\n" + "正文" * 500
        assert store.save(handle, payload)
        assert store.load(handle) == payload, "必须逐字一致（零驻留的核心承诺）"

    def test_missing_handle_returns_none(self):
        assert store.load("run-1-001-9999") is None

    def test_bad_handle_is_rejected(self):
        assert store.path_for("") == ""
        assert store.path_for("../../escape-0001") == ""
        assert store.path_for("nosep") == ""

    def test_pointer_is_deterministic_and_carries_size(self):
        text = "标题行\n" + "x" * 4000
        first = store.pointer("run-1-001-0001", text)
        assert first == store.pointer("run-1-001-0001", text)
        assert "#run-1-001-0001" in first and "KB" in first and "标题行" in first
        assert "recall" in first, "指针要告诉模型怎么取回"

    def test_fold_only_above_threshold(self):
        short = "短结果"
        assert store.fold("run-1-001-0002", short) == (short, False)
        long_text = "长结果\n" + "y" * 500
        folded, done = store.fold("run-1-001-0003", long_text)
        assert done and store.is_pointer(folded)
        assert store.load("run-1-001-0003") == long_text

    def test_disabled_store_keeps_text(self, monkeypatch):
        from config import CONTEXT_STORE_CONFIG
        monkeypatch.setitem(CONTEXT_STORE_CONFIG, "enabled", False)
        long_text = "z" * 500
        assert store.fold("run-1-001-0004", long_text) == (long_text, False)

    def test_stats_counts_entries(self):
        store.save("run-1-001-0005", "a" * 1000)
        data = store.stats("run-1-001")
        assert data["entries"] == 1 and data["bytes"] >= 1000
        assert data["saved_tokens_estimate"] > 0


class TestContextTool:
    def _tool(self):
        from tools.context_tool import ContextTool
        return ContextTool()

    def test_recall_returns_original(self):
        tool = self._tool()
        handle = "run-2-001-0001"
        payload = "原始内容\n" + "q" * 3000
        store.save(handle, payload)
        out = tool.execute_json({"action": "recall", "handle": handle})
        assert out.success and out.output == payload

    def test_recall_hash_prefix_accepted(self):
        tool = self._tool()
        store.save("run-2-001-0002", "abc" * 100)
        assert tool.execute_json({"action": "recall", "handle": "#run-2-001-0002"}).success

    def test_recall_missing_reports_clearly(self):
        out = self._tool().execute_json({"action": "recall", "handle": "run-2-001-9999"})
        assert not out.success and "取不回" in out.error

    def test_list_and_stats(self):
        tool = self._tool()
        store.save("run-3-001-0001", "首行\n" + "b" * 500)
        assert "run-3-001-0001" in tool.execute_json({"action": "list"}).output
        stats = tool.execute_json({"action": "stats"})
        assert stats.success and stats.metadata["entries"] >= 1

    def test_string_entry_point(self):
        tool = self._tool()
        store.save("run-4-001-0001", "正文" * 400)
        assert tool.execute("recall run-4-001-0001").output == "正文" * 400

    def test_unknown_action(self):
        out = self._tool().execute_json({"action": "explode"})
        assert not out.success and "未知 action" in out.error

    def test_tool_is_core(self):
        from tools.tool_manager import GATED_TOOLS, ToolManager
        assert "context" in ToolManager().list_tools()
        assert "context" not in GATED_TOOLS


class TestExecutorFolding:
    def _executor(self):
        from agent.executor import Executor
        ex = Executor.__new__(Executor)
        ex._events = []
        ex._emit = lambda kind, data=None: ex._events.append((kind, data or {}))
        return ex

    def test_large_result_is_folded_and_recoverable(self):
        ex = self._executor()
        payload = "截图结果\n" + "p" * 5000
        folded = ex._fold_result(payload)
        assert store.is_pointer(folded) and "5000" not in folded
        handle = folded.split("#")[1].split(" ")[0]
        assert store.load(handle) == payload, "折叠后原文必须还能逐字取回"

    def test_small_result_untouched(self):
        ex = self._executor()
        assert ex._fold_result("小结果") == "小结果"

    def test_old_messages_become_pointers_without_llm(self):
        ex = self._executor()
        old = [
            {"role": "user", "content": "把页面打开"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "browser", "arguments": '{"command":"snapshot"}'}}]},
            {"role": "tool", "content": "region\n" + "e" * 4000},
            {"role": "assistant", "content": "已经打开并读取了页面。"},
        ]
        folded, saved = ex._fold_old_messages(old)
        assert "[用户] 把页面打开" in folded
        assert "browser" in folded
        assert store.is_pointer(folded) or "recall" in folded
        assert saved > 3000, "省下的字符数要如实统计"

    def test_pointer_compaction_replaces_summary_call(self, monkeypatch):
        ex = self._executor()
        called = []
        monkeypatch.setattr(ex, "_summarize_old", lambda old: called.append(1) or "摘要")
        monkeypatch.setattr(ex, "_compact_threshold", lambda: 1)
        monkeypatch.setattr(ex, "_estimate_tokens", lambda msgs: 10 ** 6)
        from config import COMPACT_CONFIG
        monkeypatch.setitem(COMPACT_CONFIG, "keep_last", 2)
        messages = [{"role": "system", "content": "系统提示"}] + [
            {"role": "user", "content": f"第 {n} 步"} for n in range(6)
        ] + [
            {"role": "assistant", "tool_calls": [{"function": {"name": "file", "arguments": "{}"}}]},
            {"role": "tool", "content": "x" * 4000},
            {"role": "assistant", "content": "结论"},
        ]
        out = ex._maybe_compact(messages)
        assert out[0] == {"role": "system", "content": "系统提示"}, "system 头必须原样保留"
        assert "零驻留指针" in out[1]["content"]
        assert called == [], "指针模式下不该调用 LLM 摘要"

    def test_summary_mode_still_available(self, monkeypatch):
        from config import COMPACT_CONFIG, CONTEXT_STORE_CONFIG
        monkeypatch.setitem(CONTEXT_STORE_CONFIG, "compact_mode", "summary")
        monkeypatch.setitem(COMPACT_CONFIG, "keep_last", 2)
        ex = self._executor()
        monkeypatch.setattr(ex, "_compact_threshold", lambda: 1)
        monkeypatch.setattr(ex, "_estimate_tokens", lambda msgs: 10 ** 6)
        monkeypatch.setattr(ex, "_summarize_old", lambda old: "旧摘要")
        messages = [{"role": "system", "content": "S"}] + [{"role": "user", "content": "u"}] * 6
        out = ex._maybe_compact(messages)
        assert "## 之前的执行摘要\n旧摘要" in out[1]["content"]
