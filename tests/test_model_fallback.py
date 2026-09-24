# -*- coding: utf-8 -*-
"""备用模型链测试：主模型/主端点挂了就换备用的（用户要求"超时就换这些"）。

实测背景（本机真机验证）：
- 视觉：默认视觉模型能读图（答对图里的暗号）；
  同端点上 `sensenova-6.7-flash-lite` 对多模态路由 404、`u1.x` 是生图模型（chat 404）。
- 生图：默认供应商的几个生图模型都能出图（b64_json）。
所以默认备用就是"另一家"：备用供应商主 → 默认供应商备。
"""
import pytest

import config as cfg
import models.image_gen as img_mod
import models.vision as vision_mod
from models.image_gen import ImageGenModel
from models.vision import VisionModel


class _FakeCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.fail:
            raise RuntimeError("模拟上游 503 model_not_found")
        msg = type("M", (), {"content": f"回答自 {kwargs['model']}"})()
        choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
        return type("R", (), {"choices": [choice]})()


class _FakeClient:
    """假 LLM 客户端（按 base_url 区分主/备）。"""

    def __init__(self, base_url, fail=False):
        self.base_url = base_url
        self.fail = fail
        self.calls = []
        self.chat = type("Chat", (), {"completions": _FakeCompletions(self)})()


@pytest.fixture()
def vision_cfg(monkeypatch):
    monkeypatch.setitem(cfg.VISION_CONFIG, "vision_model", "primary-vision")
    monkeypatch.setitem(cfg.VISION_CONFIG, "base_url", "https://primary.example/v1")
    monkeypatch.setitem(cfg.VISION_CONFIG, "api_key", "k-primary")
    monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_models", ["backup-vision-a", "backup-vision-b"])
    monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_base_url", "https://backup.example/v1")
    monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_api_key", "k-backup")
    yield cfg.VISION_CONFIG


class TestVisionFallback:
    def _model(self, monkeypatch, primary_fail=True, backup_fail=False):
        """主端点 + 备用端点都用假客户端。"""
        import openai

        clients = []

        def fake_openai(**kwargs):
            base = str(kwargs.get("base_url"))
            primary = "primary" in base
            c = _FakeClient(base, fail=primary_fail if primary else backup_fail)
            clients.append(c)
            return c

        monkeypatch.setattr(vision_mod, "OpenAI", fake_openai)
        vm = VisionModel()
        return vm, clients

    def test_falls_back_when_primary_fails(self, monkeypatch, vision_cfg):
        vm, clients = self._model(monkeypatch, primary_fail=True, backup_fail=False)
        answer = vm.analyze("ZmFrZQ==", "看图")
        assert answer == "回答自 backup-vision-a", answer
        assert vm.last_model == "backup-vision-a"
        assert vm.last_detail.startswith("备用")
        assert "503" in vm.last_fallback_reason or "model_not_found" in vm.last_fallback_reason
        assert "备用模型" in vm.fallback_note()

    def test_primary_success_no_fallback(self, monkeypatch, vision_cfg):
        vm, _ = self._model(monkeypatch, primary_fail=False)
        answer = vm.analyze("ZmFrZQ==", "看图")
        assert answer == "回答自 primary-vision"
        assert vm.last_detail == "主模型"
        assert vm.fallback_note() == "", "主模型正常时不该显示备用提示"

    def test_second_backup_used_when_first_fails(self, monkeypatch, vision_cfg):
        """备用链按顺序试：第一个备用也挂就试第二个。"""
        vm, clients = self._model(monkeypatch, primary_fail=True, backup_fail=True)
        with pytest.raises(RuntimeError) as ei:
            vm.analyze("ZmFrZQ==", "看图")
        assert "全部失败" in str(ei.value)
        # 两个备用都试过了（错误里包含两者）
        assert "backup-vision-a" in str(ei.value) and "backup-vision-b" in str(ei.value)

    def test_all_failed_message_lists_every_attempt(self, monkeypatch, vision_cfg):
        vm, _ = self._model(monkeypatch, primary_fail=True, backup_fail=True)
        with pytest.raises(RuntimeError) as ei:
            vm.analyze("ZmFrZQ==", "看图")
        msg = str(ei.value)
        assert msg.count(":") >= 3, "错误里应能看到每一次尝试的原因"

    def test_identical_fallback_is_skipped(self, monkeypatch):
        """备用与主端点+主模型完全相同 → 跳过（重试同一个东西没意义）。"""
        monkeypatch.setitem(cfg.VISION_CONFIG, "vision_model", "same-model")
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_models", ["same-model"])
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_base_url",
                            cfg.VISION_CONFIG.get("base_url"))
        c = _FakeClient("https://x/v1")
        monkeypatch.setattr(vision_mod, "OpenAI", lambda **kw: c)
        vm = VisionModel()
        assert vm._fallback_clients() == []

    def test_no_fallback_configured(self, monkeypatch):
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_models", [])
        monkeypatch.setattr(vision_mod, "OpenAI", lambda **kw: _FakeClient("https://p/v1", fail=True))
        vm = VisionModel()
        assert vm._fallback_clients() == []
        with pytest.raises(RuntimeError):
            vm.analyze("ZmFrZQ==", "看图")


class TestVisionFallbackChain:
    """备用链支持"每个条目自带端点"（`模型@预设名`），跨厂商兜底必需。

    预设的 key 放在独立变量里，不进模型列表——那份列表会被 `/config`、doctor 打印。
    """

    def _cfg(self, monkeypatch, models, presets=None, **over):
        monkeypatch.setitem(cfg.VISION_CONFIG, "vision_model", "primary-vision")
        monkeypatch.setitem(cfg.VISION_CONFIG, "base_url", "https://primary.example/v1")
        monkeypatch.setitem(cfg.VISION_CONFIG, "api_key", "k-primary")
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_models", list(models))
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_base_url", "https://backup.example/v1")
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_api_key", "k-backup")
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_presets", presets or {})
        monkeypatch.setitem(cfg.VISION_CONFIG, "fallback_timeout", 20.0)
        for key, value in over.items():
            monkeypatch.setitem(cfg.VISION_CONFIG, key, value)
        return cfg.VISION_CONFIG

    def test_plain_entry_uses_shared_endpoint(self, monkeypatch):
        self._cfg(monkeypatch, ["backup-a"])
        entries = vision_mod.parse_fallback_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert (entry["model"], entry["base_url"], entry["api_key"]) == (
            "backup-a", "https://backup.example/v1", "k-backup")
        assert entry["preset"] == "" and entry["preset_known"] is True
        assert entry["timeout"] == 20.0

    def test_preset_entry_uses_its_own_endpoint_and_key(self, monkeypatch):
        self._cfg(monkeypatch, ["backup-a", "other-model@agnes"],
                  presets={"agnes": {"base_url": "https://agnes.example/v1",
                                     "api_key": "k-agnes"}})
        entries = vision_mod.parse_fallback_entries()
        assert [e["model"] for e in entries] == ["backup-a", "other-model"]
        assert entries[0]["base_url"] == "https://backup.example/v1"
        assert entries[1]["base_url"] == "https://agnes.example/v1"
        assert entries[1]["api_key"] == "k-agnes"
        assert entries[1]["preset"] == "agnes" and entries[1]["preset_known"] is True

    def test_preset_name_is_case_insensitive(self, monkeypatch):
        self._cfg(monkeypatch, ["m@AgNeS"],
                  presets={"agnes": {"base_url": "https://agnes.example/v1",
                                     "api_key": "k-agnes"}})
        assert vision_mod.parse_fallback_entries()[0]["preset_known"] is True

    def test_unknown_preset_falls_back_but_is_flagged(self, monkeypatch):
        """预设名写错不能抛错（视觉要 fail-open），但要标记出来让 --doctor 报。"""
        self._cfg(monkeypatch, ["other@typo"], presets={})
        entry = vision_mod.parse_fallback_entries()[0]
        assert entry["base_url"] == "https://backup.example/v1"      # 回落共享端点
        assert entry["preset"] == "typo" and entry["preset_known"] is False

    def test_blank_entries_are_skipped(self, monkeypatch):
        self._cfg(monkeypatch, ["", "  ", "@agnes", "ok"])
        assert [e["model"] for e in vision_mod.parse_fallback_entries()] == ["ok"]

    def test_chain_order_and_per_entry_endpoints(self, monkeypatch):
        """三级链：主 → 同端点备用 → 跨厂商备用；顺序与各自端点都要对。"""
        self._cfg(monkeypatch, ["same-vendor", "cross@agnes"],
                  presets={"agnes": {"base_url": "https://agnes.example/v1",
                                     "api_key": "k-agnes"}})
        created = []

        class _C:
            def __init__(self, base_url, api_key):
                self.base_url, self.api_key = base_url, api_key
                created.append((base_url, api_key))

        monkeypatch.setattr(vision_mod, "OpenAI",
                            lambda **kw: _C(kw.get("base_url"), kw.get("api_key")))
        vm = VisionModel()
        chain = ([(vm.vision_model, str(vm.base_url))]
                 + [(model, str(client.base_url)) for model, client in vm._fallback_clients()])
        assert [model for model, _ in chain] == ["primary-vision", "same-vendor", "cross"]
        assert chain[1][1] == "https://backup.example/v1"
        assert chain[2][1] == "https://agnes.example/v1"
        assert ("https://agnes.example/v1", "k-agnes") in created

    def test_duplicate_entries_are_skipped(self, monkeypatch):
        self._cfg(monkeypatch, ["dup", "dup"])
        monkeypatch.setattr(vision_mod, "OpenAI",
                            lambda **kw: _FakeClient(str(kw.get("base_url"))))
        assert [m for m, _ in VisionModel()._fallback_clients()] == ["dup"]


class TestImageGenFallback:
    def _model(self, monkeypatch, primary_ok=False):
        """桩掉 httpx.post：主端点按需失败，备用端点成功。"""
        calls = []

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload
                self.text = str(payload)

            def json(self):
                return self._payload

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "model": json.get("model"), "timeout": timeout})
            if "primary" in url:
                if primary_ok:
                    return _Resp(200, {"data": [{"b64_json": "UHJpbWFyeQ=="}]})
                return _Resp(502, {"error": "upstream down"})
            return _Resp(200, {"data": [{"b64_json": "QkFDS1VQ"}]})

        monkeypatch.setattr(img_mod.httpx, "post", fake_post)
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "base_url", "https://primary.example/v1")
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "model", "primary-image")
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "api_key", "k1")
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "fallback_models", ["backup-image"])
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "fallback_base_url", "https://backup.example/v1")
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "fallback_api_key", "k2")
        return ImageGenModel(), calls

    def test_falls_back_to_backup_endpoint(self, monkeypatch):
        model, calls = self._model(monkeypatch, primary_ok=False)
        out = model.generate_b64("画一只猫", n=1)
        assert out == ["QkFDS1VQ"]
        assert [c["model"] for c in calls] == ["primary-image", "backup-image"]
        assert model.last_model == "backup-image"
        assert model.last_endpoint == "https://backup.example/v1"
        assert model.last_fallback_reason

    def test_primary_success_no_fallback(self, monkeypatch):
        model, calls = self._model(monkeypatch, primary_ok=True)
        assert model.generate_b64("画一只猫") == ["UHJpbWFyeQ=="]
        assert len(calls) == 1, "主端点成功就不该再打备用"
        assert model.last_model == "primary-image"

    def test_all_endpoints_failed(self, monkeypatch):
        model, calls = self._model(monkeypatch, primary_ok=False)
        monkeypatch.setitem(cfg.IMAGE_GEN_CONFIG, "fallback_models", [])
        with pytest.raises(RuntimeError) as ei:
            model.generate_b64("画一只猫")
        assert "全部失败" in str(ei.value)

    def test_generate_reports_actual_model(self, monkeypatch):
        model, _ = self._model(monkeypatch, primary_ok=False)
        monkeypatch.setattr(ImageGenModel, "_save_image",
                            lambda self, b64, i, sd=None: f"/tmp/img{i}.png")
        res = model.generate("画一只猫", save=True)
        assert res["model"] == "backup-image", "生成的产物必须标实际出图的模型"
        assert res["fallback_from"]

    def test_auth_error_also_tries_the_backup_endpoint(self, monkeypatch):
        """主端点 401（key 问题）时也该试备用——那是**另一把 key**，不是重试同一个。"""
        model, calls = self._model(monkeypatch, primary_ok=False)
        out = model.generate_b64("画一只猫")
        assert out == ["QkFDS1VQ"]
        assert len(calls) == 2 and "backup" in calls[-1]["url"]
