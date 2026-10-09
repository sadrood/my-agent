"""会话内临时切换主模型（/model）时，视觉主模型同步跟随；备用链仍是 .env 的。"""
from types import SimpleNamespace

import pytest

from models import vision as vision_mod
from models.vision import VisionModel

INTRANET = "http://172.16.10.242:3000/v1"


@pytest.fixture(autouse=True)
def _reset_override():
    vision_mod.set_temporary_primary()
    yield
    vision_mod.set_temporary_primary()


class TestTemporaryPrimary:
    def test_override_applies_to_new_instances(self):
        vision_mod.set_temporary_primary(model="deepseek-flash", base_url=INTRANET,
                                         api_key="sk-temp")
        m = VisionModel()
        assert m.vision_model == "deepseek-flash"
        assert str(m.client.base_url).startswith(INTRANET)
        assert m.client.api_key == "sk-temp"

    def test_explicit_args_win_over_override(self):
        """显式传参（测试/特殊调用）优先于会话级覆盖。"""
        vision_mod.set_temporary_primary(model="temp", base_url=INTRANET, api_key="sk-temp")
        m = VisionModel(vision_model="explicit", base_url="http://explicit/v1",
                        api_key="sk-explicit")
        assert m.vision_model == "explicit" and "explicit" in str(m.client.base_url)

    def test_fallback_chain_still_from_env(self):
        """关键：临时端点只是主用，视觉失败时仍回落 .env 的备用链。"""
        vision_mod.set_temporary_primary(model="deepseek-flash", base_url=INTRANET,
                                         api_key="sk-temp")
        entries = VisionModel()._fallback_clients()
        assert entries, "备用链不该为空（.env 里配了 VISION_FALLBACK_MODELS）"
        bases = [str(client.base_url) for _, client in entries]
        assert not any("172.16.10.242" in b for b in bases), "临时主端点不该再出现在备用链里"

    def test_partial_override_keeps_env_for_rest(self):
        """只给模型名（没给端点）时，端点仍取 .env。"""
        vision_mod.set_temporary_primary(model="deepseek-flash")
        m = VisionModel()
        assert m.vision_model == "deepseek-flash"
        assert "172.16" not in str(m.client.base_url)

    def test_clearing_restores_env(self):
        vision_mod.set_temporary_primary(model="x", base_url=INTRANET, api_key="k")
        vision_mod.set_temporary_primary()
        assert vision_mod.temporary_primary() == {"model": "", "base_url": "", "api_key": ""}
        assert "172.16" not in str(VisionModel().client.base_url)


class TestSwitchModelSyncsVision:
    def _fake_agent(self):
        from agent.agent import Agent
        tools = {"see": SimpleNamespace(_vision_model=object()),
                 "computer": SimpleNamespace(_vision_model=object())}

        class _TM:
            def get_tool(self, name):
                return tools.get(name)

        class _Exec:
            def __init__(self):
                self._vision_model = object()
                self.tool_manager = _TM()

        fake = SimpleNamespace(
            llm=SimpleNamespace(
                default_model="deepseek-flash",
                client=SimpleNamespace(base_url=INTRANET, api_key="sk-intranet")),
            executor=_Exec(),
        )
        return Agent, fake, tools

    def test_sync_sets_override_and_clears_caches(self):
        Agent, fake, tools = self._fake_agent()
        Agent._sync_vision_to_temporary(fake)
        assert vision_mod.temporary_primary() == {
            "model": "deepseek-flash", "base_url": INTRANET, "api_key": "sk-intranet"}
        assert fake.executor._vision_model is None, "执行器的视觉缓存要失效"
        assert tools["see"]._vision_model is None, "see 工具的视觉缓存要失效"
        assert tools["computer"]._vision_model is None

    def test_switch_model_signature_is_opt_out(self):
        """sync_vision=False 时不碰视觉（给测试/特殊调用留出退出通道）。"""
        import inspect
        from agent.agent import Agent
        sig = inspect.signature(Agent.switch_model)
        assert sig.parameters["sync_vision"].default is True
