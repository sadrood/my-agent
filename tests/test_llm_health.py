"""LLM 健康遥测测试：限流类型、等待、恢复时长与落盘累计。"""
import json
import time

from agent.llm_health import LLMHealth, render

from tests.test_llm_fallback import (                      # noqa: F401 — 复用假客户端
    _Client, _make_llm, _Resp,
)


def _health(tmp_path, **kw):
    return LLMHealth(path=str(tmp_path / "llm_health.json"), **kw)


def test_aggregates_latency_limits_and_recovery(tmp_path):
    h = _health(tmp_path)
    base = {"label": "主端点", "model": "deepseek-flash", "base_url": "https://a/v1"}
    h.record(dict(base, event="call_ok", seconds=1.5))
    h.record(dict(base, event="call_ok", seconds=0.5))
    h.record(dict(base, event="quota_limited", wait=60.0,
                  message="Error code: 429 - inference exceeds tpm/rpm limit"))
    h.record(dict(base, event="quota_limited", wait=42.0,
                  message="Error code: 429 - {'code': 'insufficient_quota'}"))
    time.sleep(0.01)
    h.record(dict(base, event="call_ok", seconds=2.0))

    rec = h.labels["主端点"]
    assert rec["calls"] == 3 and rec["ok"] == 3
    assert rec["latency_seconds"]["min"] == 0.5 and rec["latency_seconds"]["max"] == 2.0
    assert rec["quota_limited"] == {"rpm_tpm_limit": 1, "tpm_exhausted": 1, "total": 2}
    assert rec["wait_seconds_total"] == 102.0
    samples = rec["limited"]["recovered_after_seconds"]
    assert len(samples) == 1 and samples[0] >= 0
    assert rec["limited"]["longest_seconds"] >= 0
    assert any(e["event"] == "recovered" for e in h.events)


def test_flush_and_reload_accumulates(tmp_path):
    path = str(tmp_path / "llm_health.json")
    h = LLMHealth(path=path)
    h.record({"label": "备用", "event": "call_ok", "seconds": 1.0})
    assert h.flush() is True, "普通事件不会自动落盘，flush 负责写"
    h.record({"label": "备用", "event": "switch", "reason": "限流"})

    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["labels"]["备用"]["calls"] == 1

    again = LLMHealth(path=path)
    again.record({"label": "备用", "event": "call_ok", "seconds": 3.0})
    assert again.labels["备用"]["calls"] == 2, "跨进程累计必须保留历史"
    assert again.labels["备用"]["switches_in"] == 1


def test_events_ring_buffer_capped(tmp_path):
    h = _health(tmp_path, max_events=5)
    for i in range(20):
        h.record({"label": "主端点", "event": "quota_limited", "wait": 1.0,
                  "message": "429 rate limit"})
    assert len(h.events) == 5, "事件环形缓冲必须有上限（否则文件无限增长）"
    h.flush()                                  # 限流事件已自动落盘，这里只是保证写完
    assert len(json.load(open(h.path, encoding="utf-8"))["events"]) == 5


def test_hard_rejection_recorded_as_error(tmp_path):
    h = _health(tmp_path)
    h.record({"label": "主端点", "event": "call_error", "message": "401 无效的令牌"})
    rec = h.labels["主端点"]
    assert rec["errors"] == 1 and rec["quota_limited"]["total"] == 0


def test_render_shows_recovery_and_kinds(tmp_path):
    h = _health(tmp_path)
    h.record({"label": "主端点", "event": "call_ok", "seconds": 1.2})
    h.record({"label": "主端点", "event": "quota_limited", "wait": 60.0,
              "message": "429 tpm exhausted"})
    h.record({"label": "主端点", "event": "call_ok", "seconds": 1.1})

    text = render(h.snapshot())
    assert "主端点" in text and "限流" in text
    assert "0/1" in text                        # 分钟窗口/额度空 计数（这里吃的是 tpm exhausted）


def test_llm_emits_health_events(monkeypatch, tmp_path):
    """真跑一遍失败路径：429 → 记 quota_limited（带类型与等待）→ 成功 → 记 call_ok。"""
    llm = _make_llm(monkeypatch, primary="429", backup="备用回答")
    events = []
    llm.health_notifier = events.append

    assert llm.chat([{"role": "user", "content": "hi"}]) == "备用回答"

    kinds = [e["event"] for e in events]
    assert "quota_limited" in kinds and "call_ok" in kinds and "switch" in kinds
    limited = next(e for e in events if e["event"] == "quota_limited")
    assert limited["label"] == "主端点" and limited["attempt"] >= 1
    assert limited["message"] and limited["wait"] >= 0
    ok = [e for e in events if e["event"] == "call_ok"]
    assert ok and all(e["seconds"] >= 0 for e in ok)


def test_agent_handler_writes_and_rolls_up(monkeypatch, tmp_path):
    """Agent 的回调把事件写进 JSON，并把关键时刻记进 rollout。"""
    from agent.agent import Agent

    class _Rollout:
        def __init__(self):
            self.events = []

        def emit(self, name, data):
            self.events.append((name, data))

    class _Stub:
        llm_health = LLMHealth(path=str(tmp_path / "llm_health.json"))
        rollout = _Rollout()

    stub = _Stub()
    Agent._on_llm_health(stub, {"event": "quota_limited", "label": "主端点",
                                "wait": 60.0, "message": "429 rate limit"})
    Agent._on_llm_health(stub, {"event": "call_ok", "label": "主端点", "seconds": 1.0})

    assert stub.llm_health.labels["主端点"]["quota_limited"]["total"] == 1
    assert [n for n, _ in stub.rollout.events] == ["llm_health"], "每次调用都记 rollout 会刷屏"
    assert stub.rollout.events[0][1]["event"] == "quota_limited"


class _ScriptedClient:
    """按脚本返回：字符串=成功回答，"401"=突发的无效令牌，其他=抛对应异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = type("C", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else "兜底回答"
        if item == "401":
            from openai import AuthenticationError
            raise AuthenticationError("无效的令牌", response=_Resp(401), body=None)
        return __import__("tests.test_llm_fallback", fromlist=["x"])._Completion(item)


def test_401_after_success_is_retried_not_switched(monkeypatch):
    """成功过的端点遇 401（突发保护）→ 退避重试，不换端点。"""
    llm = _make_llm(monkeypatch, primary="429", backup="不该用到")
    llm.auth_retry_max = 2
    llm.auth_retry_delay = 0.0
    llm.providers[0]["client"] = _ScriptedClient(["第一次成功", "401", "重试成功"])
    llm._activate_provider(0, "")

    assert llm.chat([{"role": "user", "content": "a"}]) == "第一次成功"
    assert llm.chat([{"role": "user", "content": "b"}]) == "重试成功"
    assert llm.provider_index == 0, "401 突发保护不该触发切换"
    assert len(llm.providers[0]["client"].calls) == 3


def test_401_without_prior_success_switches(monkeypatch):
    """没成功过就 401（真 key 错）→ 直接换端点，不浪费退避。"""
    llm = _make_llm(monkeypatch, primary="429", backup="备用接住")
    llm.auth_retry_max = 2
    llm.auth_retry_delay = 0.0
    llm.providers[0]["client"] = _ScriptedClient(["401", "401", "401"])
    llm._activate_provider(0, "")

    assert llm.chat([{"role": "user", "content": "hi"}]) == "备用接住"
    assert llm.provider_index == 1
    assert len(llm.providers[0]["client"].calls) == 1, "没成功过就不该退避重试"


def test_401_retries_exhausted_then_switches(monkeypatch):
    """退避次数用尽仍 401 → 才换端点。"""
    llm = _make_llm(monkeypatch, primary="429", backup="备用接住")
    llm.auth_retry_max = 1
    llm.auth_retry_delay = 0.0
    llm.providers[0]["client"] = _ScriptedClient(["先成功", "401", "401"])
    llm._activate_provider(0, "")

    assert llm.chat([{"role": "user", "content": "a"}]) == "先成功"
    assert llm.chat([{"role": "user", "content": "b"}]) == "备用接住"
    assert llm.provider_index == 1


def test_recovery_survives_process_restart(tmp_path):
    """限流开始时刻落盘、重启后仍能算出"等了多久"——每次运行都是新进程。"""
    path = str(tmp_path / "llm_health.json")
    first = LLMHealth(path=path)
    first.record({"label": "主端点", "event": "quota_limited", "wait": 60.0,
                  "message": "429 limit"})
    assert first.labels["主端点"]["limited"]["since"]

    second = LLMHealth(path=path)          # 模拟下一次运行（新进程）
    second.record({"label": "主端点", "event": "call_ok", "seconds": 1.0})

    samples = second.labels["主端点"]["limited"]["recovered_after_seconds"]
    assert len(samples) == 1, "恢复时长必须跨进程累计，否则永远抓不到"
    assert samples[0] >= 0
