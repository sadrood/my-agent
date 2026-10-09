"""模型能力目录：本地实测覆盖 > 学习缓存 > models.dev 目录；全查不到返回 None。"""
import json
import time

import pytest

from models import model_catalog as mc


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """目录与学习缓存都落到临时目录，且默认不联网。"""
    monkeypatch.setattr(mc, "catalog_path", lambda: str(tmp_path / "catalog.json"))
    monkeypatch.setattr(mc, "learned_path", lambda: str(tmp_path / "learned.json"))
    monkeypatch.setattr(mc, "_FETCH_ATTEMPTED_AT", 0.0, raising=False)
    yield


def _fake_catalog():
    """models.dev 形状的假目录（provider → models → spec）。"""
    return {
        "deepseek": {"models": {
            "deepseek-v4-flash": {
                "id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash",
                "limit": {"context": 524288, "output": 32768},
                "modalities": {"input": ["text", "image"]}, "reasoning": True,
            },
        }},
        "zhipu": {"models": {
            "glm-5-air": {
                "id": "glm-5-air", "name": "GLM-5 Air",
                "limit": {"context": 131072, "output": 8192},
                "modalities": {"input": ["text"]}, "reasoning": False,
            },
        }},
    }


class TestFlattenAndMatch:
    def test_flatten_reads_limit_and_modalities(self):
        flat = mc._flatten(_fake_catalog())
        entry = flat["deepseek/deepseek-v4-flash"]
        assert entry["context"] == 524288 and entry["output"] == 32768
        assert entry["vision"] is True and entry["reasoning"] is True
        assert flat["zhipu/glm-5-air"]["vision"] is False

    def test_match_exact_and_token_subset(self):
        entry = {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "canonical": ""}
        assert mc._match(entry, "DeepSeek-V4-Flash")
        assert mc._match(entry, "deepseek-v4-flash")
        assert not mc._match(entry, "glm-5-air")

    def test_match_ignores_provider_prefix(self):
        entry = {"id": "deepseek-ai/DeepSeek-V3.1", "name": "", "canonical": ""}
        assert mc._match(entry, "DeepSeek-V3.1")


class TestLookupPriority:
    def test_endpoint_scoped_override_wins(self, monkeypatch):
        """实测值按端点作用域：商汤的 1M 窗口不能被套到别的端点。"""
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        got = mc.lookup("deepseek-flash", "https://token.sensenova.cn/v1")
        assert got["context"] == 1_048_576 and got["output"] == 65_536
        assert got["vision"] is True and got["source"] == "override"
        other = mc.lookup("deepseek-flash", "https://other.example/v1")
        assert other.get("context") != 1_048_576, "别的端点不该继承商汤实测的窗口"

    def test_learned_cache_fills_what_override_lacks(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        mc.note_capability("http://intranet/v1", "gpt-5.6-terra", vision=True, context=999)
        got = mc.lookup("gpt-5.6-terra", "http://intranet/v1")
        assert got["vision"] is True and got["context"] == 999
        assert got["source"] in ("override", "learned")

    def test_catalog_used_when_endpoint_silent(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        mc.load_catalog(force=True)          # 首次装好目录（异步语义下要先预热）
        assert mc.context_window("deepseek-v4-flash", "http://x/v1") == 524288
        assert mc.max_output("deepseek-v4-flash", "http://x/v1") == 32768
        assert mc.supports_vision("deepseek-v4-flash", "http://x/v1") is True
        assert mc.supports_vision("glm-5-air", "http://x/v1") is False

    def test_unknown_model_returns_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        mc.load_catalog(force=True)
        assert mc.context_window("some-brand-new-model", "http://x/v1") == 0
        assert mc.supports_vision("some-brand-new-model", "http://x/v1") is None

    def test_learned_cache_is_scoped_by_endpoint(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: {})
        mc.note_capability("http://a/v1", "m1", vision=False)
        assert mc.supports_vision("m1", "http://a/v1") is False
        assert mc.supports_vision("m1", "http://b/v1") is None


class TestCatalogCache:
    def test_fetch_sends_user_agent(self, monkeypatch):
        """上游默认拒 python-urllib 的 UA（403），请求必须自带 UA。"""
        seen = {}

        class _Resp:
            status = 200

            def read(self):
                return json.dumps(_fake_catalog()).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return _Resp()

        monkeypatch.setattr(mc.urllib.request, "urlopen", _urlopen)
        mc.fetch_catalog()
        assert seen.get("ua"), "请求必须带 User-Agent"

    def test_expired_cache_returns_old_data_without_blocking(self, monkeypatch):
        """缓存过期时先返回旧数据，刷新走后台（5MB 目录同步拉会卡住整轮对话）。"""
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        mc.load_catalog(force=True)
        assert mc.context_window("glm-5-air", "http://x/v1") == 131072   # 命中缓存
        data = json.load(open(mc.catalog_path(), encoding="utf-8"))
        data["fetched_at"] = time.time() - mc.CATALOG_TTL_SECONDS - 10
        json.dump(data, open(mc.catalog_path(), "w", encoding="utf-8"))
        monkeypatch.setattr(mc, "_FETCH_ATTEMPTED_AT", 0.0, raising=False)
        monkeypatch.setattr(mc, "_REFRESH_THREAD", None, raising=False)

        def _slow_fetch(*a, **k):
            time.sleep(1.5)
            return _fake_catalog()

        monkeypatch.setattr(mc, "fetch_catalog", _slow_fetch)
        start = time.time()
        got = mc.load_catalog()
        elapsed = time.time() - start
        assert got.get("models"), "应立刻返回旧缓存"
        assert elapsed < 0.5, f"不该同步等刷新（用了 {elapsed:.2f}s）"

    def test_force_true_fetches_synchronously(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        assert mc.load_catalog(force=True).get("models")

    def test_tighter_match_rejects_wrong_version(self, monkeypatch):
        """glm-5-air 不该匹配到 glm-4.5-air（版本号不同就是不同模型）。"""
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: {
            "zai": {"models": {"glm-4.5-air": {
                "id": "glm-4.5-air", "limit": {"context": 131072, "output": 8192},
                "modalities": {"input": ["text"]}}}}})
        mc.load_catalog(force=True)
        assert mc.context_window("glm-5-air", "http://x/v1") == 0
        assert mc.context_window("glm-4.5-air", "http://x/v1") == 131072

    def test_fresh_cache_avoids_network(self, monkeypatch):
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: _fake_catalog())
        mc.load_catalog(force=True)
        calls = []
        monkeypatch.setattr(mc, "fetch_catalog", lambda *a, **k: calls.append(1) or _fake_catalog())
        mc.load_catalog()
        assert calls == [], "TTL 内不该再联网"


class TestVisionLearningSignal:
    def test_rejected_image_detection(self):
        from models.vision import _rejected_image
        assert _rejected_image("Error code: 400 - unsupported image format")
        assert _rejected_image("this model does not support image input")
        assert not _rejected_image("Request timed out.")
        assert not _rejected_image("429 rate limit exceeded")

    def test_analyze_success_records_capability(self, monkeypatch):
        from models.vision import VisionModel
        recorded = []
        monkeypatch.setattr(mc, "note_capability",
                            lambda base, model, **kw: recorded.append((base, model, kw)))
        vm = VisionModel(vision_model="m1", base_url="http://x/v1", api_key="k")
        monkeypatch.setattr(vm, "_analyze_once", lambda *a, **k: "看图结果")
        assert vm.analyze("QUJD", "这是什么") == "看图结果"
        assert recorded and recorded[0][2].get("vision") is True

    def test_analyze_rejection_records_not_vision(self, monkeypatch):
        from models.vision import VisionModel
        recorded = []
        monkeypatch.setattr(mc, "note_capability",
                            lambda base, model, **kw: recorded.append((base, model, kw)))
        vm = VisionModel(vision_model="m1", base_url="http://x/v1", api_key="k")
        monkeypatch.setattr(vm, "_fallback_clients", lambda: [])
        monkeypatch.setattr(vm, "_analyze_once",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("400 unsupported image")))
        with pytest.raises(RuntimeError):
            vm.analyze("QUJD", "这是什么")
        assert recorded and recorded[0][2].get("vision") is False
