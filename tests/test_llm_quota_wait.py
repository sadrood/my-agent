"""限流等待：识别供应商窗口（含中文"每 N 分钟"），别在窗口内反复撞墙。

实测现场：agnes 端点回 429「今日文本额度已用完，当前处于限速模式：每 3 分钟最多 1 次」，
旧实现只认英文 tpm/rpm → 退化成 6/12/24/48s 指数退避，且单次等待硬顶 65s，
结果一整轮都在同一 3 分钟窗口里空转（3338 次限流重试、83% 时间在等）。
"""
import pytest

import models.llm as llm_mod
from config import LLM_CONFIG


CN_THROTTLE = ("Error code: 429 - {'error': {'message': '今日文本额度已用完，"
               "当前处于限速模式：每 3 分钟最多 1 次'}}")


class _Err(Exception):
    """带 response.headers 的假 429（模拟 openai SDK 的 APIStatusError 形状）。"""

    def __init__(self, msg, headers=None):
        super().__init__(msg)
        if headers is not None:
            class _R:
                pass
            r = _R()
            r.headers = headers
            self.response = r


@pytest.fixture
def llm():
    instance = llm_mod.LLM()
    instance.quota_max_wait = 300.0
    return instance


class TestDetectChineseThrottle:
    def test_real_chinese_message_is_recognized(self):
        assert llm_mod._is_minute_quota_error(_Err(CN_THROTTLE)) is True

    def test_english_keywords_still_recognized(self):
        for text in ("requests per minute exceeded", "tpm limit", "429 rate limit exceeded",
                     "something quota_exceeded"):
            assert llm_mod._is_minute_quota_error(_Err(text)) is True, text

    def test_plain_error_is_not_quota(self):
        assert llm_mod._is_minute_quota_error(_Err("connection reset by peer")) is False


class TestParseWindow:
    @pytest.mark.parametrize("text,expected", [
        ("今日文本额度已用完，当前处于限速模式：每 3 分钟最多 1 次", 180.0),
        ("每 30 秒最多 1 次", 30.0),
        ("每 1 小时 100 次", 3600.0),
        ("rate limited: every 5 minutes", 300.0),
        ("限速 2 minutes", 120.0),
        ("没有任何窗口提示", 0.0),
    ])
    def test_parse(self, text, expected):
        assert llm_mod._quota_window_seconds(_Err(text)) == expected


class TestQuotaWait:
    def test_waits_the_provider_window_not_65s(self, llm):
        delay = llm._quota_wait(_Err(CN_THROTTLE), attempt=0)
        assert delay == pytest.approx(185.0), f"应等到 3 分钟窗口末尾，实际 {delay}"

    def test_respects_configurable_cap(self, llm):
        llm.quota_max_wait = 60.0
        assert llm._quota_wait(_Err(CN_THROTTLE), attempt=0) == pytest.approx(60.0)

    def test_retry_after_header_wins_and_is_capped(self, llm):
        ra = _Err("429", headers={"retry-after": "200"})
        assert llm._quota_wait(ra, attempt=0) == pytest.approx(200.0)
        big = _Err("429", headers={"retry-after": "9999"})
        assert llm._quota_wait(big, attempt=0) == pytest.approx(300.0)

    def test_plain_rate_limit_aligns_to_minute_boundary(self, llm):
        # 命中"限流"但没给窗口长度 → 对齐到下一个分钟边界（不短于指数退避，不超上限）
        delay = llm._quota_wait(_Err("rate limit exceeded"), attempt=0)
        assert llm._retry_delay(0, quota=True) <= delay <= llm.quota_max_wait

    def test_unknown_429_falls_back_to_exponential(self, llm):
        # 既不是已知限流措辞、也没有窗口：按普通配额退避（3 倍起步 → 6s）
        assert llm._quota_wait(_Err("429 unknown"), attempt=0) == pytest.approx(
            llm._retry_delay(0, quota=True))

    def test_message_shows_window_and_attempt(self, llm, capfd):
        llm._quota_wait(_Err(CN_THROTTLE), attempt=2)
        err = capfd.readouterr().err
        assert "限流" in err and "180s" in err and "第 3 次" in err

    def test_cap_comes_from_config(self, llm):
        assert llm.quota_max_wait == float(LLM_CONFIG.get("quota_max_wait", 300.0))
