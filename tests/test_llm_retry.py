"""
LLM 自动重试测试（OpenRouter 限流场景）。
"""
import pytest
from openai import RateLimitError, BadRequestError

from models.llm import LLM


class _FakeResponse:
    """openai 异常构造函数只存储 response，不校验类型。"""
    status_code = 429
    headers = {}
    request = None

    def __init__(self, status_code=429):
        self.status_code = status_code


def make_429():
    return RateLimitError("rate limited", response=_FakeResponse(429), body=None)


class FakeMessage:
    def __init__(self, content="hello", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, content="hello", finish_reason="stop", tool_calls=None):
        self.message = FakeMessage(content, tool_calls)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content="hello", finish_reason="stop", tool_calls=None):
        self.choices = [FakeChoice(content, finish_reason, tool_calls)]


class FakeCompletions:
    """按脚本顺序弹出异常或响应。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def make_llm(script):
    llm = LLM()
    llm.client = FakeClient(FakeCompletions(script))
    llm.max_retries = 2
    llm.retry_base_delay = 0.01
    return llm


def test_default_endpoint_and_timeout():
    llm = LLM()
    assert llm.client.api_key
    assert llm.client.base_url
    # 超时保护存在（默认 300s），防止上游挂起无限等待
    assert llm.timeout > 0
    assert llm.client.timeout is not None


def test_retry_on_429_then_success():
    llm = make_llm([make_429(), make_429(), FakeResponse(content="最终成功")])
    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out == "最终成功"
    assert len(llm.client.chat.completions.calls) == 3


def test_exhausted_retries_raise():
    llm = make_llm([make_429(), make_429(), make_429()])
    with pytest.raises(RateLimitError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert len(llm.client.chat.completions.calls) == 3


def test_non_retryable_error_raises_immediately():
    llm = make_llm([BadRequestError("bad request", response=_FakeResponse(400), body=None)])
    with pytest.raises(BadRequestError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert len(llm.client.chat.completions.calls) == 1


def test_chat_with_tools_retry():
    class FakeTC:
        def __init__(self):
            self.id = "c1"
            self.function = type("F", (), {"name": "add", "arguments": '{"a":1,"b":2}'})()

    llm = make_llm([
        make_429(),
        FakeResponse(content="", finish_reason="tool_calls", tool_calls=[FakeTC()]),
    ])
    resp = llm.chat_with_tools(
        [{"role": "user", "content": "算一下"}],
        tools=[{"type": "function", "function": {"name": "add", "parameters": {"type": "object"}}}],
    )
    assert resp.has_tool_calls
    assert resp.tool_calls[0].name == "add"
    assert resp.tool_calls[0].arguments == {"a": 1, "b": 2}
    assert len(llm.client.chat.completions.calls) == 2


# ============================================================
# 400 参数自动降级（通用机制）
# ============================================================

def make_bad_request(param, message="unsupported param"):
    """构造带 param 字段的 400 错误（OpenAI 标准错误体）。"""
    return BadRequestError(
        message,
        response=_FakeResponse(400),
        body={"error": {"param": param, "message": message, "type": "invalid_request_error"}},
    )


def test_temperature_only_1_self_heal_via_message():
    """message 无 param 字段时，靠正则识别 temperature 特例并固定为 1。"""
    llm = make_llm([
        BadRequestError(
            "Error code: 400 - only 1 is allowed for this model",
            response=_FakeResponse(400), body=None,
        ),
        FakeResponse(content="ok"),
    ])
    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert llm.fixed_temperature == 1.0
    assert llm.client.chat.completions.calls[1]["temperature"] == 1


def test_max_tokens_downgraded_to_max_completion_tokens():
    """max_tokens 被拒 → 自动转为 max_completion_tokens 并记住。"""
    llm = make_llm([
        make_bad_request("max_tokens"),
        FakeResponse(content="ok"),
    ])
    out = llm.chat([{"role": "user", "content": "hi"}])
    assert out == "ok"
    calls = llm.client.chat.completions.calls
    assert "max_tokens" not in calls[1]
    assert calls[1]["max_completion_tokens"] > 0
    assert "max_tokens" in llm.param_blacklist
    # 实例级记忆：后续请求直接转换，不再 400
    llm.client.chat.completions.script.append(FakeResponse(content="ok"))
    llm.chat([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in llm.client.chat.completions.calls[-1]
    assert "max_completion_tokens" in llm.client.chat.completions.calls[-1]


def test_top_p_removed_after_400():
    llm = make_llm([
        make_bad_request("top_p"),
        FakeResponse(content="ok"),
    ])
    out = llm.chat([{"role": "user", "content": "hi"}], top_p=0.9)
    assert out == "ok"
    calls = llm.client.chat.completions.calls
    assert calls[0]["top_p"] == 0.9
    assert "top_p" not in calls[1]


def test_extract_param_from_message_fallback():
    """body 无 param 时，从 message 文本提取参数名。"""
    llm = make_llm([
        BadRequestError(
            "Error code: 400 - unexpected field 'top_p'",
            response=_FakeResponse(400), body=None,
        ),
        FakeResponse(content="ok"),
    ])
    out = llm.chat([{"role": "user", "content": "hi"}], top_p=0.5)
    assert out == "ok"
    assert "top_p" not in llm.client.chat.completions.calls[1]


def test_chat_with_tools_param_downgrade():
    llm = make_llm([
        make_bad_request("top_p"),
        FakeResponse(content="ok"),
    ])
    resp = llm.chat_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        top_p=0.5,
    )
    assert resp.content == "ok"
    assert "top_p" not in llm.client.chat.completions.calls[1]


def test_stream_connection_downgrade():
    """流式建立连接阶段 400 → 移除参数重试（流尚未开始，可安全重试）。"""
    llm = make_llm([
        make_bad_request("top_p"),
        [],  # 空流：第二次 create 成功，迭代立即结束
    ])
    events = list(llm.chat_with_tools_stream(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        top_p=0.9,
    ))
    calls = llm.client.chat.completions.calls
    assert calls[0]["top_p"] == 0.9
    assert "top_p" not in calls[1]
    assert calls[1]["stream"] is True
    assert events == []


def test_unparseable_400_raises():
    """无法识别参数的 400 直接抛出，不无限重试。"""
    llm = make_llm([
        BadRequestError("Error code: 400 - gibberish", response=_FakeResponse(400), body=None),
    ])
    with pytest.raises(BadRequestError):
        llm.chat([{"role": "user", "content": "hi"}])
    assert len(llm.client.chat.completions.calls) == 1


class TestQuota429Variants:
    """429 变体（包装成 invalid_request_error 的配额错误）应转重试而非直接失败。"""

    def _make_make_429_invalid_request(self):
        # 真实网关：inference tpm exhausted 以 invalid_request_error 类型到达
        return BadRequestError(
            "Error code: 429 - {'error': {'message': 'inference tpm exhausted',"
            " 'type': 'invalid_request_error'}}",
            response=_FakeResponse(400), body=None,
        )

    def test_429_variant_retries_then_success(self):
        llm = make_llm([self._make_make_429_invalid_request(), FakeResponse(content="ok")])
        resp = llm.chat([{"role": "user", "content": "hi"}])
        assert resp == "ok"
        assert len(llm.client.chat.completions.calls) == 2  # 失败一次后重试成功

    def test_429_variant_exhausts_retries(self):
        llm = make_llm([
            self._make_make_429_invalid_request(),
            self._make_make_429_invalid_request(),
            self._make_make_429_invalid_request(),
        ])
        with pytest.raises(BadRequestError):
            llm.chat([{"role": "user", "content": "hi"}])
        assert len(llm.client.chat.completions.calls) == 3  # 初试 + 2 次重试

    def test_quota_delay_longer_than_normal(self):
        """限流类退避起点更长（分钟级配额窗口）。"""
        from unittest.mock import patch
        llm = make_llm([make_429(), FakeResponse(content="ok")])
        with patch("time.sleep") as mock_sleep:
            llm.chat([{"role": "user", "content": "hi"}])
        quota_delay = mock_sleep.call_args[0][0]
        # 非限流基数 * (3) = 0.03；上次用 0.01 * 2^0 = 0.01
        assert quota_delay >= 0.03

    def test_retry_delay_helper(self):
        llm = make_llm([])
        normal = llm._retry_delay(0, quota=False)
        quota = llm._retry_delay(0, quota=True)
        assert quota == normal * 3
        assert llm._retry_delay(1, quota=True) == quota * 2  # 指数退避
