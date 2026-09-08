"""
上下文压缩阈值解析测试（窗口比例制）：
- 网关 /models 窗口查询 + 缓存
- 阈值 = 显式 env 优先 > 窗口×比例 > 兜底
- 执行器/展示共用同一解析，显示与实际不再脱节
"""
import json

from config import COMPACT_CONFIG


def _make_llm():
    from models.llm import LLM
    llm = LLM(api_key="test-key", base_url="http://test-gw/v1")
    llm._window_cache = {}
    return llm


def test_fetch_context_window_from_gateway(monkeypatch):
    """从网关 /models 读取 context_length，并按 (base, model) 缓存。"""
    from models.llm import LLM
    llm = _make_llm()
    calls = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [
                {"id": "deepseek-v4-flash", "context_length": 1048576},
                {"id": "sensenova-6.8-flash-lite", "context_length": 262144},
            ]}).encode()

    def fake_urlopen(req, timeout=None):
        calls.append(str(req.full_url))
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert llm.fetch_context_window("deepseek-v4-flash") == 1048576
    assert llm.fetch_context_window("sensenova-6.8-flash-lite") == 262144
    assert llm.fetch_context_window("missing-model") is None
    # 缓存生效：不再发网络请求
    assert llm.fetch_context_window("deepseek-v4-flash") == 1048576
    assert len(calls) == 3


def test_compact_threshold_ratio_and_explicit(monkeypatch):
    """阈值：显式 COMPACT_TOKEN_THRESHOLD >0 优先；否则窗口×window_ratio。"""
    from models.llm import LLM
    llm = _make_llm()

    monkeypatch.setitem(COMPACT_CONFIG, "token_threshold", 0)
    monkeypatch.setitem(COMPACT_CONFIG, "window_ratio", 0.75)
    monkeypatch.setattr(llm, "fetch_context_window", lambda m=None: 1048576)
    assert llm.compact_threshold_tokens() == 786432  # 1M × 0.75

    monkeypatch.setitem(COMPACT_CONFIG, "token_threshold", 45000)
    assert llm.compact_threshold_tokens() == 45000   # 显式覆盖


def test_compact_threshold_fallback_when_window_unknown(monkeypatch):
    """窗口查询失败时回退保守兜底（不回到过早压缩的小数字）。"""
    from models.llm import LLM
    llm = _make_llm()
    monkeypatch.setitem(COMPACT_CONFIG, "token_threshold", 0)
    monkeypatch.setattr(llm, "fetch_context_window", lambda m=None: None)
    assert llm.compact_threshold_tokens() == 240_000


def test_executor_threshold_explicit_and_fallback():
    """执行器 _compact_threshold：显式模块级配置优先；无 LLM 支持时兜底。"""
    import agent.executor as ex
    from agent.executor import Executor
    from tools.tool_manager import ToolManager
    from agent.approval import ApprovalPolicy

    class NoThresholdLLM:
        pass

    old_cfg = dict(ex.COMPACT_CONFIG)
    try:
        ex.COMPACT_CONFIG = {"enabled": True, "token_threshold": 0,
                             "keep_last": 3, "window_ratio": 0.75}
        e = Executor(
            tool_manager=ToolManager(), llm=NoThresholdLLM(),
            approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False),
            guardian=None, rollout=None, instructions_text="", max_step_ops=10, llm_retry_delay=0,
        )
        assert e._compact_threshold() == 240_000  # 无 LLM 能力 → 兜底

        ex.COMPACT_CONFIG = {"enabled": True, "token_threshold": 7777,
                             "keep_last": 3, "window_ratio": 0.75}
        assert e._compact_threshold() == 7777      # 显式优先
    finally:
        ex.COMPACT_CONFIG = old_cfg
