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
        from unittest.mock import patch
        llm = make_llm([self._make_make_429_invalid_request(), FakeResponse(content="ok")])
        # tpm 配额会触发"等到下一分钟边界"（生产行为），测试里 patch 掉真实等待
        with patch("models.llm.time.sleep"):
            resp = llm.chat([{"role": "user", "content": "hi"}])
        assert resp == "ok"
        assert len(llm.client.chat.completions.calls) == 2  # 失败一次后重试成功

    def test_429_variant_exhausts_retries(self):
        from unittest.mock import patch
        llm = make_llm([
            self._make_make_429_invalid_request(),
            self._make_make_429_invalid_request(),
            self._make_make_429_invalid_request(),
        ])
        with patch("models.llm.time.sleep"):
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


class TestMinuteQuotaAlignment:
    """TPM/RPM 是**每分钟**窗口重置的配额：供应商不给 retry-after 时，
    单纯指数退避（几秒）会一直在同一分钟窗口内硬怼。

    实测日志：6s/12s 退避的两次重试全部撞在同一分钟内失败、任务中断。
    修复：识别分钟级配额错误 → 等待对齐到下一个分钟边界。
    """

    def _tpm_err(self):
        return BadRequestError(
            "Error code: 429 - {'error': {'message': 'inference tpm exhausted',"
            " 'type': 'invalid_request_error'}}",
            response=_FakeResponse(400), body=None,
        )

    def test_aligns_to_next_minute_boundary(self):
        from unittest.mock import patch
        llm = make_llm([])
        base = 1_700_000_000 - (1_700_000_000 % 60)   # 对齐到某分钟起点
        # 分钟内第 5 秒 → 应等 ~56s（跨窗口），远大于 0.03s 的退避
        with patch("models.llm.time.time", return_value=base + 5):
            w_early = llm._quota_wait(self._tpm_err(), 0)
        # 分钟内第 58 秒 → 距边界仅 3s，短于退避时取退避（不小于 0.03）
        with patch("models.llm.time.time", return_value=base + 58):
            w_late = llm._quota_wait(self._tpm_err(), 0)
        assert w_early >= 50, f"分钟初段应等到窗口结束，实际 {w_early}"
        assert w_late >= 0.03

    def test_rpm_also_aligned(self):
        from unittest.mock import patch
        llm = make_llm([])
        base = 1_700_000_000 - (1_700_000_000 % 60)
        err = BadRequestError(
            "Error code: 429 - {'error': {'message': 'rpm exhausted'}}",
            response=_FakeResponse(400), body=None,
        )
        with patch("models.llm.time.time", return_value=base + 10):
            assert llm._quota_wait(err, 0) >= 45

    def test_retry_after_header_takes_precedence(self):
        """供应商明确给了 retry-after 时以它为准（上限 65s），不做分钟对齐。"""
        llm = make_llm([])
        err = self._tpm_err()
        err.response.headers = {"retry-after": "30"}
        assert llm._quota_wait(err, 0) == 30.0

        err2 = self._tpm_err()
        err2.response.headers = {"retry-after": "120"}
        assert llm._quota_wait(err2, 0) == 65.0   # 上限

        err3 = self._tpm_err()
        err3.response.headers = {"retry-after": "0.001"}
        assert llm._quota_wait(err3, 0) >= 0.03   # 不小于退避

    def test_non_minute_quota_uses_plain_backoff(self):
        """普通限流（非分钟级关键词）不触发分钟对齐，避免无谓长等待。"""
        from unittest.mock import patch
        llm = make_llm([])
        base = 1_700_000_000 - (1_700_000_000 % 60)
        with patch("models.llm.time.time", return_value=base + 5):
            w = llm._quota_wait(Exception("Error code: 429 - too many requests"), 0)
        assert w < 5, f"普通限流不应等分钟边界，实际 {w}"

    def test_is_minute_quota_error_detection(self):
        from models.llm import _is_minute_quota_error
        assert _is_minute_quota_error(Exception("inference tpm exhausted"))
        assert _is_minute_quota_error(Exception("rpm exhausted"))
        assert _is_minute_quota_error(Exception("inference exceeds tpm/rpm limit"))
        assert not _is_minute_quota_error(Exception("too many requests"))
        assert not _is_minute_quota_error(Exception("internal server error"))


class TestRateLimitVisibility:
    """限流等待必须"看得见 + 留得下记录"。

    实测问题（分析 run-20260918-163309 时踩到）：client 层的限流提示只写 stderr，
    桌面端/内嵌 UI/dashboard 渲染的是富文本 stdout，那行没人看得到；而且**等待时长
    没进 rollout**——事后只知道"发生过 5 次 429"，不知道一共等了 10 秒还是 2 分钟。
    配额等待单次上限 65s、默认重试 2 次，最长可达约 2 分钟静默。
    """

    def test_notifier_defaults_to_none(self):
        assert LLM().retry_notifier is None, "默认必须是 None：models 层不依赖 UI 层"

    def test_no_notifier_is_fine(self):
        llm = make_llm([make_429(), FakeResponse(content="ok")])
        assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"

    def test_injected_callback_receives_wait_seconds_and_attempt(self):
        llm = make_llm([make_429(), make_429(), FakeResponse(content="ok")])
        seen = []
        llm.retry_notifier = lambda seconds, attempt: seen.append((seconds, attempt))
        assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"
        assert len(seen) == 2, "两次限流各通知一次"
        assert [a for _, a in seen] == [0, 1], "attempt 从 0 起（展示时 +1）"
        assert all(s > 0 for s, _ in seen), "必须带等待时长，否则复盘仍不知等了多久"

    def test_notifier_exception_never_breaks_retry(self):
        llm = make_llm([make_429(), FakeResponse(content="ok")])

        def boom(seconds, attempt):
            raise RuntimeError("UI 层炸了")

        llm.retry_notifier = boom
        assert llm.chat([{"role": "user", "content": "hi"}]) == "ok", \
            "提示失败不能影响重试"

    def test_long_wait_writes_stderr_and_reports_real_seconds(self, monkeypatch, capsys):
        """≥5s 的长等待：stderr 兜底提示 + 回调拿到真实秒数（不真的睡）。"""
        import models.llm as llm_mod
        slept = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: slept.append(s))

        llm = make_llm([make_429(), FakeResponse(content="ok")])
        llm.retry_base_delay = 10.0          # quota 起步 = 30s，超过 5s 阈值
        seen = []
        llm.retry_notifier = lambda seconds, attempt: seen.append(seconds)
        assert llm.chat([{"role": "user", "content": "hi"}]) == "ok"

        assert "[限流]" in capsys.readouterr().err, "纯 CLI 下 stderr 仍要有兜底提示"
        assert seen and seen[0] >= 30, "回调要拿到真实等待秒数，实际 %s" % seen
        assert slept and slept[0] >= 30, "确实按该时长等待（此处被替换成记录）"
