"""切换临时模型前的探测：只验当前端点，不走重试/备用链。

背景：`/model name@url#key` 原先切完不做任何校验，key 写错要等到下一轮对话
才炸——而且那时可能已经被备用链悄悄顶掉，看不出是临时端点的问题。
"""
from types import SimpleNamespace

from models.llm import LLM


class _Completions:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))])


class _Client:
    def __init__(self, completions):
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)

    def with_options(self, **kwargs):
        return self


def _llm(behavior):
    llm = LLM.__new__(LLM)                 # 绕开真实客户端
    completions = _Completions(behavior)
    llm.client = _Client(completions)
    llm.default_model = "glm-5.2"
    return llm, completions


def test_probe_ok_sends_current_model_without_temperature():
    llm, completions = _llm("ok")
    assert llm.probe() == ""
    sent = completions.calls[0]
    assert sent["model"] == "glm-5.2"
    assert "temperature" not in sent, "thinking 模型常只接受 1，探测别带 temperature"


def test_probe_reports_auth_error():
    llm, _ = _llm(RuntimeError("Error code: 401 - invalid api key"))
    err = llm.probe()
    assert "invalid api key" in err


def test_probe_reports_empty_choices():
    """200 但没有 choices（网关限流伪装）也要判不可用。"""
    class _Empty:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[])

    llm = LLM.__new__(LLM)
    llm.client = _Client(_Empty())
    llm.default_model = "glm-5.2"
    assert llm.probe() != ""


def test_probe_restores_client_after_call():
    llm, _ = _llm("ok")
    original = llm.client
    llm.probe()
    assert llm.client is original
