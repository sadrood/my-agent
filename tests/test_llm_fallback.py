"""主模型备用链测试：主端点限流时自动切备用端点，过一阵回试主端点。"""
import time

import pytest
from openai import RateLimitError

from models.llm import LLM


class _Resp:
    status_code = 429
    headers = {}
    request = None

    def __init__(self, status_code=429):
        self.status_code = status_code


def _quota_error():
    return RateLimitError("rate limited", response=_Resp(429), body=None)


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Completion:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]


class _Completions:
    """behavior = "429" 表示一直限流，否则直接返回该文本。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.behavior == "429":
            raise _quota_error()
        return _Completion(self.behavior)


class _Chat:
    def __init__(self, completions):
        self.completions = completions


class _Client:
    def __init__(self, behavior, base_url, api_key):
        self.completions = _Completions(behavior)
        self.chat = _Chat(self.completions)
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = 5


def _make_llm(monkeypatch, primary, backup, fallback_models=("backup-model",),
              fallback_after=1, probe_seconds=300):
    from config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "fallback_models", list(fallback_models))
    monkeypatch.setitem(LLM_CONFIG, "fallback_base_url", "http://backup/v1")
    monkeypatch.setitem(LLM_CONFIG, "fallback_api_key", "kb")
    monkeypatch.setitem(LLM_CONFIG, "fallback_presets", {})
    monkeypatch.setitem(LLM_CONFIG, "fallback_after", fallback_after)
    monkeypatch.setitem(LLM_CONFIG, "fallback_probe_seconds", probe_seconds)

    llm = LLM()
    llm.max_retries = 1
    llm.retry_base_delay = 0.01
    llm.providers[0]["client"] = _Client(primary, "http://primary/v1", "ka")
    if len(llm.providers) > 1:
        llm.providers[1]["client"] = _Client(backup, "http://backup/v1", "kb")
    llm._activate_provider(0, "")
    monkeypatch.setattr(llm, "_quota_wait", lambda err, attempt: 0.0)
    return llm


def test_switches_to_backup_when_primary_is_rate_limited(monkeypatch):
    events = []
    llm = _make_llm(monkeypatch, primary="429", backup="备用端点的回答")
    llm.provider_notifier = events.append

    out = llm.chat([{"role": "user", "content": "hi"}])

    assert out == "备用端点的回答"
    assert llm.default_model == "backup-model"
    assert llm.provider_index == 1
    assert llm.providers[0]["client"].completions.calls, "主端点应确实试过"
    fallback_call = llm.providers[1]["client"].completions.calls[-1]
    assert fallback_call["model"] == "backup-model", "切端点后必须换成备用端点的模型名"
    assert events and events[-1]["model"] == "backup-model"
    assert events[-1]["primary"] is False and events[-1]["reason"]


def test_without_backup_chain_behaviour_is_unchanged(monkeypatch):
    llm = _make_llm(monkeypatch, primary="429", backup="不会用到", fallback_models=())

    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}])

    assert len(llm.providers[0]["client"].completions.calls) == 2   # max_retries + 1
    assert len(llm.providers) == 1


def test_probe_returns_to_primary_after_cooldown(monkeypatch):
    events = []
    llm = _make_llm(monkeypatch, primary="429", backup="备用回答", probe_seconds=60)
    llm.provider_notifier = events.append
    assert llm.chat([{"role": "user", "content": "hi"}]) == "备用回答"

    # 主端点恢复 + 冷却时间已过 → 下一次请求先试主端点
    llm.providers[0]["client"] = _Client("主端点恢复", "http://primary/v1", "ka")
    llm.switched_at = time.time() - 120
    out = llm.chat([{"role": "user", "content": "again"}])

    assert out == "主端点恢复"
    assert llm.provider_index == 0
    assert events[-1]["primary"] is True


def test_stays_on_backup_during_cooldown(monkeypatch):
    llm = _make_llm(monkeypatch, primary="429", backup="备用回答", probe_seconds=3600)
    assert llm.chat([{"role": "user", "content": "hi"}]) == "备用回答"

    llm.providers[0]["client"] = _Client("主端点恢复", "http://primary/v1", "ka")
    assert llm.chat([{"role": "user", "content": "again"}]) == "备用回答", \
        "冷却期内不该反复回试主端点"
    assert llm.provider_index == 1


def test_all_providers_limited_raises_bounded(monkeypatch):
    llm = _make_llm(monkeypatch, primary="429", backup="429")

    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}])

    primary_calls = len(llm.providers[0]["client"].completions.calls)
    backup_calls = len(llm.providers[1]["client"].completions.calls)
    assert primary_calls == 2 and backup_calls == 2, "每个端点各给满重试预算，不无限切换"


def test_preset_entries_use_their_own_endpoint(monkeypatch):
    from config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "fallback_base_url", "")
    monkeypatch.setitem(LLM_CONFIG, "fallback_api_key", "")
    monkeypatch.setitem(LLM_CONFIG, "fallback_models", ["agnes-3.0-flash@agnes"])
    monkeypatch.setitem(LLM_CONFIG, "fallback_presets",
                        {"agnes": {"base_url": "https://api.agnes/v1", "api_key": "kp"}})

    entries = LLM._provider_entries(LLM_CONFIG)

    assert entries == [{"model": "agnes-3.0-flash", "base_url": "https://api.agnes/v1",
                        "api_key": "kp", "label": "agnes-3.0-flash@agnes"}]


def test_unreachable_backup_falls_through_to_next(monkeypatch):
    """内网备用端点连不上时，必须继续往下一级走，而不是直接放弃。"""
    from config import LLM_CONFIG
    from openai import APIConnectionError

    monkeypatch.setitem(LLM_CONFIG, "fallback_models", ["backup-a@a", "backup-b@b"])
    monkeypatch.setitem(LLM_CONFIG, "fallback_presets", {
        "a": {"base_url": "http://unreachable/v1", "api_key": "ka"},
        "b": {"base_url": "http://second/v1", "api_key": "kb"},
    })
    monkeypatch.setitem(LLM_CONFIG, "fallback_after", 1)

    llm = LLM()
    llm.max_retries = 0
    llm.retry_base_delay = 0.0
    llm.providers[0]["client"] = _Client("429", "http://primary/v1", "kp")

    class _Dead:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kwargs):
            raise APIConnectionError(request=None)

    llm.providers[1]["client"] = _Dead()
    llm.providers[2]["client"] = _Client("第三级的回答", "http://second/v1", "kb")
    llm._activate_provider(0, "")
    monkeypatch.setattr(llm, "_quota_wait", lambda err, attempt: 0.0)

    assert llm.chat([{"role": "user", "content": "hi"}]) == "第三级的回答"
    assert llm.provider_index == 2


def test_entry_without_preset_follows_primary_endpoint(monkeypatch):
    """同端点同 key 换模型：共享端点留空即跟随主 LLM。"""
    from config import LLM_CONFIG
    monkeypatch.setitem(LLM_CONFIG, "fallback_models", ["deepseek-v4-flash"])
    monkeypatch.setitem(LLM_CONFIG, "fallback_base_url", "")
    monkeypatch.setitem(LLM_CONFIG, "fallback_api_key", "")
    monkeypatch.setitem(LLM_CONFIG, "fallback_presets", {})

    entries = LLM._provider_entries(LLM_CONFIG)

    assert len(entries) == 1
    assert entries[0]["model"] == "deepseek-v4-flash"
    assert entries[0]["base_url"] == LLM_CONFIG["base_url"]
    assert entries[0]["api_key"] == LLM_CONFIG["api_key"]


def test_hard_rejection_switches_endpoint(monkeypatch):
    """404（模型不在该端点）/ 403（不在套餐内）这类重试无用的错误，换端点往往能救。"""
    from openai import NotFoundError

    llm = _make_llm(monkeypatch, primary="429", backup="备用端点接住了")

    class _Rejecting:
        def __init__(self):
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kwargs):
            raise NotFoundError("model is not found", response=_Resp(404), body=None)

    llm.providers[0]["client"] = _Rejecting()
    llm._activate_provider(0, "")
    llm.max_retries = 0

    assert llm.chat([{"role": "user", "content": "hi"}]) == "备用端点接住了"
    assert llm.provider_index == 1


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None


class _StreamChoice:
    def __init__(self, content=None, finish=None):
        self.delta = _Delta(content)
        self.finish_reason = finish


class _Chunk:
    def __init__(self, content=None, finish=None):
        self.usage = None
        self.choices = [] if (content is None and finish is None) else [_StreamChoice(content, finish)]


class _StreamClient:
    """限流客户端：create 一律抛 429（流式连接阶段就该在这里被换掉）。"""

    def __init__(self):
        self.chat = type("C", (), {"completions": self})()
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise _quota_error()


class _OkStreamClient:
    def __init__(self, text="备用端的流式回答"):
        self.text = text
        self.chat = type("C", (), {"completions": self})()
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return iter([_Chunk(content=self.text), _Chunk(finish="stop")])


def test_stream_path_switches_endpoint_on_quota(monkeypatch):
    """主循环走的是流式路径：限额重试耗尽后必须能切备用端点，而不是白等完判死。"""
    llm = _make_llm(monkeypatch, primary="429", backup="备用")
    llm.max_retries = 1
    llm.providers[0]["client"] = _StreamClient()
    llm.providers[1]["client"] = _OkStreamClient()
    llm._activate_provider(0, "")
    events = []
    llm.health_notifier = events.append

    text = "".join(e.text for e in llm.chat_with_tools_stream(
        [{"role": "user", "content": "hi"}], []) if e.type == "text_delta")

    assert text == "备用端的流式回答"
    assert llm.provider_index == 1, "流式路径也必须走备用链"
    assert llm.providers[1]["client"].last_kwargs["model"] == "backup-model"
    kinds = [e["event"] for e in events]
    assert "quota_limited" in kinds and "switch" in kinds and "call_ok" in kinds
