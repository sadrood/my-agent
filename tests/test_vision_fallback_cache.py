"""备用视觉端点客户端必须复用（之前每次 analyze 都新建一批 httpx 连接池）。"""

import pytest

from config import VISION_CONFIG
from models.vision import VisionModel


def _has_fallbacks() -> bool:
    return bool(VISION_CONFIG.get("fallback_models") or [])


@pytest.mark.skipif(not _has_fallbacks(), reason="未配置备用视觉端点")
class TestFallbackClientsAreCached:
    def test_same_instances_across_calls(self):
        m = VisionModel()
        first = [id(c) for _, c in m._fallback_clients()]
        second = [id(c) for _, c in m._fallback_clients()]
        assert first == second, "每次调用都在新建客户端（连接池泄漏）"

    def test_config_change_invalidates_cache(self):
        """改了配置要能重建（否则换端点后还在用旧的）。"""
        m = VisionModel()
        first = m._fallback_clients()
        assert m._fallback_cache_key is not None
        old = VISION_CONFIG.get("fallback_timeout")
        try:
            VISION_CONFIG["fallback_timeout"] = (old or 60) + 1
            assert m._fallback_clients() is not first, "配置变了仍返回旧缓存"
        finally:
            VISION_CONFIG["fallback_timeout"] = old
