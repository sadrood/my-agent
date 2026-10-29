"""长窗口限流时立刻回切主端点（B）：别在链尾的限流端点上蹲着。

现场：主端点 glm-5.2 偶尔 429 → fallback_after=1 立刻降级 → 链尾是免费/限流端点 →
回切要等 probe_seconds（300s）→ 实测卡在 agnes 上 8.8 小时 / 1456 次限流重试。
"""
import time


import models.llm as llm_mod

CN_LONG = ("Error code: 429 - {'error': {'message': '今日文本额度已用完，"
           "当前处于限速模式：每 3 分钟最多 1 次'}}")


class _Err(Exception):
    def __init__(self, msg, headers=None):
        super().__init__(msg)
        if headers is not None:
            class _R:
                pass
            r = _R()
            r.headers = headers
            self.response = r


def _llm():
    instance = llm_mod.LLM(api_key="k", base_url="http://primary/v1", model="primary-m")
    instance.providers = [
        {"model": "primary-m", "base_url": "http://primary/v1", "api_key": "k",
         "label": "主端点", "client": object()},
        {"model": "backup-m", "base_url": "http://backup/v1", "api_key": "k",
         "label": "备用", "client": object()},
    ]
    instance.provider_index = 1
    instance.client = instance.providers[1]["client"]
    instance.default_model = "backup-m"
    instance.long_throttle_seconds = 60.0
    return instance


class TestLongThrottleDetection:
    def test_cn_window_counts_as_long(self):
        llm = _llm()
        assert llm._note_long_throttle(_Err(CN_LONG)) is True
        assert llm.provider_index in llm._provider_throttled_at

    def test_retry_after_counts_as_long(self):
        llm = _llm()
        assert llm._note_long_throttle(_Err("429", headers={"retry-after": "120"})) is True

    def test_short_throttle_is_not_long(self):
        llm = _llm()
        assert llm._note_long_throttle(_Err("rate limit exceeded")) is False
        assert llm._provider_throttled_at == {}


class TestProbeBack:
    def test_switches_back_to_primary_immediately(self):
        llm = _llm()
        kwargs = {"model": "backup-m", "temperature": 0.7}
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is True
        assert llm.provider_index == 0, "长窗口限流应立即回试主端点"
        assert kwargs["model"] == "primary-m", "模型名必须跟着换"
        assert llm.default_model == "primary-m"

    def test_no_probe_for_short_throttle(self):
        llm = _llm()
        kwargs = {"model": "backup-m"}
        assert llm._probe_primary_if_throttled(_Err("rate limit exceeded"), kwargs) is False
        assert llm.provider_index == 1

    def test_no_bouncing_when_primary_also_long_throttled(self):
        llm = _llm()
        llm._provider_throttled_at[0] = time.time()      # 主端点刚被长窗口限过
        kwargs = {"model": "backup-m"}
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is False
        assert llm.provider_index == 1, "主端点也刚被限，别来回弹"

    def test_on_primary_nothing_to_switch_to(self):
        llm = _llm()
        llm.provider_index = 0
        llm.client = llm.providers[0]["client"]
        kwargs = {"model": "primary-m"}
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is False
        assert llm.provider_index == 0

    def test_probe_allowed_after_primary_throttle_window_passes(self):
        llm = _llm()
        llm._provider_throttled_at[0] = time.time() - 3600   # 一小时前的主端点限流，早该过去了
        kwargs = {"model": "backup-m"}
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is True
        assert llm.provider_index == 0

    def test_repeated_probes_are_bounded(self):
        """回切成功→主端点也长窗口限流→再回到备用时不能立刻再弹（有记账守卫）。"""
        llm = _llm()
        kwargs = {"model": "backup-m"}
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is True   # 回主端点
        # 主端点也限流了：记账
        llm._note_long_throttle(_Err(CN_LONG))
        # 再降级到备用，再遇长窗口 → 守卫拦住（主端点刚被限）
        llm.provider_index = 1
        llm.client = llm.providers[1]["client"]
        kwargs["model"] = "backup-m"
        assert llm._probe_primary_if_throttled(_Err(CN_LONG), kwargs) is False

    def test_threshold_comes_from_config(self):
        llm = _llm()
        assert llm.long_throttle_seconds == float(
            llm_mod.LLM_CONFIG.get("fallback_long_throttle_seconds", 60))
