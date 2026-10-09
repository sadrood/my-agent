"""被强杀也不丢：运行日志、关窗兜底、会话增量落盘、从 rollout 恢复。"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from agent import console_guard, recover, run_log


@pytest.fixture(autouse=True)
def _clean_hooks():
    """退出钩子是全局注册表：用例之间必须清空。"""
    saved = list(console_guard._EXIT_HOOKS)
    console_guard._EXIT_HOOKS.clear()
    yield
    console_guard._EXIT_HOOKS[:] = saved


class TestRunLog:
    def test_start_log_and_finish(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_log, "_PATH", "")
        monkeypatch.setattr(run_log, "_FH", None)
        target = tmp_path / "run.log"
        assert run_log.start(path=str(target)) == str(target)
        run_log.log("干了一件事")
        run_log.finish("测试结束")
        text = target.read_text(encoding="utf-8")
        assert "干了一件事" in text and "测试结束" in text
        assert "pid=" in text, "启动环境要记下来（排查'终端自己关闭'用）"
        monkeypatch.setattr(run_log, "_PATH", "")
        monkeypatch.setattr(run_log, "_FH", None)

    def test_log_without_start_is_silent(self, monkeypatch):
        monkeypatch.setattr(run_log, "_FH", None)
        run_log.log("不该炸")          # 未启动时静默

    def test_uncaught_exception_is_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_log, "_PATH", "")
        monkeypatch.setattr(run_log, "_FH", None)
        target = tmp_path / "run.log"
        run_log.start(path=str(target))
        previous = sys.excepthook
        try:
            raise ValueError("boom-测试")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = previous
            run_log.finish("收尾")
            monkeypatch.setattr(run_log, "_PATH", "")
            monkeypatch.setattr(run_log, "_FH", None)
        text = target.read_text(encoding="utf-8")
        assert "未捕获异常" in text and "boom-测试" in text


class TestConsoleClose:
    def test_hooks_run_in_order(self):
        seen = []
        console_guard.register_exit_hook(lambda reason: seen.append(("a", reason)), "a")
        console_guard.register_exit_hook(lambda reason: seen.append(("b", reason)), "b")
        results = console_guard.run_exit_hooks("控制台被关闭")
        assert [name for name, _ in results] == ["a", "b"]
        assert seen == [("a", "控制台被关闭"), ("b", "控制台被关闭")]

    def test_register_is_idempotent(self):
        hook = lambda reason: None
        console_guard.register_exit_hook(hook, "x")
        console_guard.register_exit_hook(hook, "x")
        assert len(console_guard._EXIT_HOOKS) == 1

    def test_one_bad_hook_does_not_block_others(self):
        console_guard.register_exit_hook(lambda reason: (_ for _ in ()).throw(RuntimeError("x")), "bad")
        ok = []
        console_guard.register_exit_hook(lambda reason: ok.append(1), "good")
        results = dict(console_guard.run_exit_hooks("关窗"))
        assert results["bad"] is False and results["good"] is True and ok == [1]

    def test_ctrl_c_is_not_swallowed(self):
        """Ctrl+C 必须留给 Python 的 KeyboardInterrupt：返回 False 让默认处理继续。"""
        called = []
        console_guard.register_exit_hook(lambda reason: called.append(reason), "h")
        assert console_guard._console_handler(console_guard.CTRL_C_EVENT) is False
        assert console_guard._console_handler(console_guard.CTRL_BREAK_EVENT) is False
        assert called == [], "Ctrl+C 不该触发退出刷盘"

    def test_close_event_flushes_and_claims(self):
        called = []
        console_guard.register_exit_hook(lambda reason: called.append(reason), "h")
        assert console_guard._console_handler(console_guard.CTRL_CLOSE_EVENT) is True
        assert called == ["控制台被关闭"]
        assert console_guard._console_handler(console_guard.CTRL_SHUTDOWN_EVENT) is True
        assert called[-1] == "系统关机"


class TestIncrementalSession:
    def _agent_stub(self, store, name="conv-x"):
        from agent.agent import Agent
        stub = SimpleNamespace(
            config=SimpleNamespace(session_name=name),
            llm=SimpleNamespace(default_model="m1", client=SimpleNamespace(base_url="http://e/v1")),
            session_store=store,
        )
        stub._incremental_save = Agent._incremental_save.__get__(stub)
        stub._session_entries_from_messages = Agent._session_entries_from_messages
        return stub

    def test_tool_results_become_readable_entries(self):
        from agent.agent import Agent
        entries = Agent._session_entries_from_messages([
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "干活"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "工具输出"},
            {"role": "assistant", "content": "干完了"},
        ])
        assert [e["role"] for e in entries] == ["user", "assistant", "assistant"]
        assert entries[1]["content"].startswith("【工具结果】")
        assert "系统提示" not in json.dumps(entries, ensure_ascii=False), "系统提示不进会话文件"

    def test_only_new_messages_are_appended(self, monkeypatch):
        appends = []

        class _Store:
            def append_messages(self, conv_id, messages, **fields):
                appends.append(list(messages))

        from config import RUN_LOG_CONFIG
        monkeypatch.setitem(RUN_LOG_CONFIG, "incremental_interval_seconds", 0)
        agent = self._agent_stub(_Store())
        agent._incremental_save([{"role": "user", "content": "一"}])
        agent._incremental_save([{"role": "user", "content": "一"},
                                 {"role": "assistant", "content": "二"}])
        assert [len(batch) for batch in appends] == [1, 1], "第二次只该追加新增的那一条"
        assert appends[1][0]["content"] == "二"

    def test_throttle_skips_but_keeps_latest_for_exit_flush(self, monkeypatch):
        appends = []

        class _Store:
            def append_messages(self, conv_id, messages, **fields):
                appends.append(list(messages))

        from config import RUN_LOG_CONFIG
        monkeypatch.setitem(RUN_LOG_CONFIG, "incremental_interval_seconds", 999)
        agent = self._agent_stub(_Store())
        agent._incremental_save([{"role": "user", "content": "一"}])
        agent._incremental_save([{"role": "user", "content": "一"},
                                 {"role": "user", "content": "二"}])
        assert len(appends) == 1, "节流期内不写盘"
        assert agent._last_messages[-1]["content"] == "二", "但最新一份要留着给关窗兜底"

    def test_disabled_by_config(self, monkeypatch):
        appends = []

        class _Store:
            def append_messages(self, *a, **k):
                appends.append(1)

        from config import RUN_LOG_CONFIG
        monkeypatch.setitem(RUN_LOG_CONFIG, "incremental_session", False)
        agent = self._agent_stub(_Store())
        agent._incremental_save([{"role": "user", "content": "一"}])
        assert appends == []

    def test_store_failure_does_not_raise(self, monkeypatch):
        class _Boom:
            def append_messages(self, *a, **k):
                raise OSError("disk full")

        from config import RUN_LOG_CONFIG
        monkeypatch.setitem(RUN_LOG_CONFIG, "incremental_interval_seconds", 0)
        agent = self._agent_stub(_Boom())
        agent._incremental_save([{"role": "user", "content": "一"}])   # 不该抛


class TestRecover:
    def _rollout(self, tmp_path, with_end: bool):
        path = tmp_path / "run-20260101-000000-1.jsonl"
        events = [
            {"event": "run_start", "data": {"goal": "把小说整理好"}},
            {"event": "model_turn", "data": {"content": "我先看看文件。"}},
            {"event": "tool_call", "data": {"tool": "file", "args": {"operation": "list"}}},
            {"event": "tool_result", "data": {"tool": "file", "success": True, "output": "a.md"}},
            {"event": "model_turn", "data": {"content": "整理完了。"}},
        ]
        if with_end:
            events.append({"event": "run_end", "data": {"status": "completed"}})
        with open(path, "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path

    def test_build_messages_keeps_goal_text_and_tool_trail(self):
        events = [
            {"event": "run_start", "data": {"goal": "目标"}},
            {"event": "tool_call", "data": {"tool": "browser", "args": {"command": "goto"}}},
            {"event": "tool_result", "data": {"success": True, "output": "ok"}},
            {"event": "model_turn", "data": {"content": "做完了"}},
        ]
        messages = recover.build_messages(events)
        assert messages[0] == {"role": "user", "content": "目标"}
        assert messages[1]["content"].startswith("【执行记录】")
        assert "browser" in messages[1]["content"]
        assert messages[-1]["content"] == "做完了"

    def test_long_tool_trail_does_not_crowd_out_text(self):
        events = [{"event": "run_start", "data": {"goal": "g"}}]
        for i in range(60):
            events.append({"event": "tool_result", "data": {"success": True, "output": f"结果{i}"}})
        events.append({"event": "model_turn", "data": {"content": "关键结论"}})
        messages = recover.build_messages(events)
        assert messages[-1]["content"] == "关键结论", "正文不能被执行记录挤掉"
        assert all(len(m["content"]) <= 4100 for m in messages)

    def test_orphan_detection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(tmp_path))
        self._rollout(tmp_path, with_end=False)
        runs = recover.scan_runs(hours=24)
        assert len(runs) == 1 and runs[0]["orphan"] is True
        assert runs[0]["goal"] == "把小说整理好"
        assert recover.find_orphans(hours=24, min_events=1)

    def test_finished_run_is_not_orphan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(tmp_path))
        self._rollout(tmp_path, with_end=True)
        assert recover.scan_runs(hours=24)[0]["orphan"] is False
        assert recover.find_orphans(hours=24, min_events=1) == []

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(tmp_path))
        self._rollout(tmp_path, with_end=False)
        info = recover.recover("run-20260101-000000-1", dry_run=True)
        assert info["ok"] and info["messages"] >= 3

    def test_recover_writes_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(tmp_path))
        self._rollout(tmp_path, with_end=False)
        saved = {}

        class _Store:
            def load_conversation(self, conv_id):
                return {}

            def save_conversation(self, conv_id, messages=None, **fields):
                saved["conv"] = conv_id
                saved["messages"] = messages

        monkeypatch.setattr("agent.session.SessionStore", _Store)
        info = recover.recover("run-20260101-000000-1", conv_id="conv-test")
        assert info["ok"] and saved["conv"] == "conv-test"
        assert saved["messages"][0]["content"] == "把小说整理好"

    def test_unknown_run_reports_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(tmp_path))
        assert recover.recover("run-nope", dry_run=True)["ok"] is False
