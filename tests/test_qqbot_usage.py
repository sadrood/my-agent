import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent.metrics import RunMetrics
from agent.qqbot import QQBotBridge, QQBotConfig, UserSession
from agent.qqbot_usage import ActiveMessageLimitReached, ActiveMessageUsage


def test_active_limit_survives_restart_and_isolated_by_bot_and_target(tmp_path):
    path = str(tmp_path / "usage.sqlite3")
    usage = ActiveMessageUsage(path, "bot", 1)
    reservation = usage.reserve("c:user")
    usage.finish(reservation, True)
    restarted = ActiveMessageUsage(path, "bot", 1)
    with pytest.raises(ActiveMessageLimitReached):
        restarted.reserve("c:user")
    assert restarted.summary("c:user")["sent"] == 1
    assert restarted.reserve("g:group")
    assert ActiveMessageUsage(path, "another-bot", 1).reserve("c:user")


def test_active_month_rollover_and_failed_attempts(tmp_path, monkeypatch):
    usage = ActiveMessageUsage(str(tmp_path / "usage.sqlite3"), "bot", 1)
    monkeypatch.setattr(usage, "_month", lambda: "2026-11")
    reservation = usage.reserve("c:user")
    usage.finish(reservation, False)
    assert usage.summary("c:user")["failed"] == 1
    with pytest.raises(ActiveMessageLimitReached):
        usage.reserve("c:user")
    monkeypatch.setattr(usage, "_month", lambda: "2026-12")
    assert usage.summary("c:user")["attempts"] == 0
    assert usage.reserve("c:user")


def test_concurrent_reservations_cannot_exceed_limit(tmp_path):
    usage = ActiveMessageUsage(str(tmp_path / "usage.sqlite3"), "bot", 3)

    def reserve(_):
        try:
            return usage.reserve("c:user")
        except ActiveMessageLimitReached:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(12)))
    assert sum(value is not None for value in results) == 3
    assert usage.summary("c:user")["pending"] == 3


def test_ledger_contains_no_message_or_raw_openid(tmp_path):
    path = tmp_path / "usage.sqlite3"
    usage = ActiveMessageUsage(str(path), "bot", 0)
    usage.reserve("c:private-user-openid")
    assert b"private-user-openid" not in path.read_bytes()


def test_all_send_outcomes_count_without_charging_passive(tmp_path):
    config = QQBotConfig({"app_id": "1", "app_secret": "fake", "allow_from": "me",
                          "active_usage_file": str(tmp_path / "usage.sqlite3"),
                          "active_monthly_limit": 2})
    bridge = QQBotBridge(config)
    calls = []

    class Api:
        async def post_c2c_message(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("content") == "failure":
                raise RuntimeError("platform rejected")

    async def send():
        await bridge._post_message("c:me", Api(), msg_id="incoming", content="passive")
        assert bridge._active_usage.summary("c:me")["attempts"] == 0
        await bridge._post_message("c:me", Api(), content="active")
        with pytest.raises(RuntimeError):
            await bridge._post_message("c:me", Api(), content="failure")
        with pytest.raises(ActiveMessageLimitReached):
            await bridge._post_message("c:me", Api(), content="blocked")

    asyncio.run(send())
    assert len(calls) == 3
    summary = bridge._active_usage.summary("c:me")
    assert summary["sent"] == summary["failed"] == 1


def test_status_and_new_report_real_metrics_without_inventing_price(tmp_path):
    bridge = QQBotBridge(QQBotConfig({"allow_from": "me",
                        "active_usage_file": str(tmp_path / "usage.sqlite3")}))
    session = bridge._session_for("c:me")
    session._record_usage(RunMetrics(input_tokens=120, output_tokens=30, cached_tokens=60))
    session._record_usage(RunMetrics(input_tokens=20, output_tokens=5))
    status = bridge.handle_text("me", "/status")
    assert "输入 20 / 输出 5 token" in status
    assert "输入 140 / 输出 35 token" in status
    assert "本月主动发送" in status
    assert "元" not in status
    bridge.handle_text("me", "/new")
    status = bridge.handle_text("me", "/status")
    assert "暂无供应商用量数据" in status
    assert "输入 0 / 输出 0" in status
    assert "/help" in bridge.handle_text("me", "/not-a-command")


def test_task_and_light_chat_collect_only_their_own_usage():
    task = UserSession("c:task")

    class Agent:
        metrics = None

        def run(self, *args, **kwargs):
            self.metrics = RunMetrics(input_tokens=100, output_tokens=20, cached_tokens=80)
            return "done"

    task.agent = Agent()
    assert task.run("goal") == "done"
    assert task.last_usage["input_tokens"] == 100
    assert task.total_usage["cached_tokens"] == 80

    class LLM:
        metrics = RunMetrics(input_tokens=999)

        def chat(self, *args, **kwargs):
            self.metrics.add_llm_call(0.1, input_tokens=40, output_tokens=10, cached_tokens=20)
            return "reply"

    llm = LLM()
    previous = llm.metrics
    chat = UserSession("c:chat")
    chat.agent = SimpleNamespace(llm=llm)
    assert chat.chat("hi") == "reply"
    assert chat.last_usage == {"input_tokens": 40, "output_tokens": 10, "cached_tokens": 20}
    assert llm.metrics is previous and previous.input_tokens == 999
