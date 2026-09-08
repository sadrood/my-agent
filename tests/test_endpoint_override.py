"""
独立端点覆盖测试：视觉 / Guardian 可使用与主 LLM 不同的 API 端点。
"""
import pytest

from models.llm import LLM


def test_llm_explicit_endpoint():
    llm = LLM(api_key="k-test", base_url="https://example.com/v1")
    assert llm.client.api_key == "k-test"
    assert "example.com" in str(llm.client.base_url)
    # 默认参数仍来自 LLM_CONFIG
    assert llm.default_model


def test_llm_default_endpoint():
    llm = LLM()
    assert llm.client.api_key  # .env 里有 key
    assert llm.client.base_url


def test_llm_model_override():
    llm = LLM(api_key="k", base_url="https://example.com/v1", model="my-custom-model")
    assert llm.default_model == "my-custom-model"
    assert llm.client.api_key == "k"


def test_agent_temporary_model_override(tmp_path):
    """AgentConfig 的 llm_model 临时覆盖 → 主 LLM 使用覆盖模型（不写 .env）。"""
    from agent import Agent, AgentConfig
    from agent.memory import Memory

    config = AgentConfig(
        verbose=False,
        llm_model="temp-model-xyz",
        guardian_enabled=False,
        snapshot_enabled=False,
        checkpoint_per_tool=False,
        instructions_enabled=False,
    )
    agent = Agent(config=config, memory=Memory(db_path=str(tmp_path)))
    assert agent.llm.default_model == "temp-model-xyz"


def test_env_alias_anthropic_names():
    """ANTHROPIC_MODEL/BASE_URL/API_KEY 别名（兼容主流 CLI 用户习惯）。"""
    from config import resolve_llm_config
    cfg = resolve_llm_config({
        "ANTHROPIC_MODEL": "alias-model",
        "ANTHROPIC_BASE_URL": "https://alias.example.com/v1",
        "ANTHROPIC_API_KEY": "alias-key",
    })
    assert cfg["default_model"] == "alias-model"
    assert cfg["base_url"] == "https://alias.example.com/v1"
    assert cfg["api_key"] == "alias-key"


def test_env_alias_priority():
    """优先级：MY_AGENT_* > ANTHROPIC_* > LLM_*。"""
    from config import resolve_llm_config
    cfg = resolve_llm_config({
        "MY_AGENT_MODEL": "mine",
        "ANTHROPIC_MODEL": "anthropic",
        "LLM_DEFAULT_MODEL": "legacy",
    })
    assert cfg["default_model"] == "mine"


def test_minimal_mode_env():
    from config import resolve_minimal_mode
    assert resolve_minimal_mode({"MY_AGENT_MINIMAL": "1"}) is True
    assert resolve_minimal_mode({"MY_AGENT_MINIMAL": "true"}) is True
    assert resolve_minimal_mode({}) is False


def test_restore_conversation_model_switches(monkeypatch):
    """恢复对话时自动切回其绑定的模型（一对话一模型）——用户未显式配置默认模型时。"""
    # 清除显式模型配置环境变量：模拟"用户没配默认模型，用程序默认值"
    for k in ("MY_AGENT_MODEL", "ANTHROPIC_MODEL", "LLM_DEFAULT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    from agent import Agent, AgentConfig
    config = AgentConfig(
        verbose=False, llm_model="model-default",
        guardian_enabled=False, snapshot_enabled=False,
        checkpoint_per_tool=False, instructions_enabled=False,
    )
    agent = Agent(config=config)
    assert agent.llm.default_model == "model-default"

    agent._restore_conversation_model({"model": "model-bound", "base_url": ""})
    assert agent.llm.default_model == "model-bound"

    # 相同模型 → 无操作
    agent._restore_conversation_model({"model": "model-bound"})
    assert agent.llm.default_model == "model-bound"


def test_restore_conversation_model_respects_explicit_env(monkeypatch):
    """用户显式配置默认模型（环境变量）时，恢复对话**不**覆盖（回归：
    .env 设 glm-5.2 却被旧对话绑定模型覆盖）。"""
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "glm-5.2")
    from agent import Agent, AgentConfig
    config = AgentConfig(
        verbose=False, llm_model="model-default",
        guardian_enabled=False, snapshot_enabled=False,
        checkpoint_per_tool=False, instructions_enabled=False,
    )
    agent = Agent(config=config)

    agent._restore_conversation_model({"model": "model-bound", "base_url": ""})
    assert agent.llm.default_model == "model-default"   # 不被覆盖


def test_agent_switch_model():
    from agent import Agent, AgentConfig
    config = AgentConfig(
        verbose=False, llm_model="model-a",
        guardian_enabled=False, snapshot_enabled=False,
        checkpoint_per_tool=False, instructions_enabled=False,
    )
    agent = Agent(config=config)
    assert agent.llm.default_model == "model-a"

    info = agent.switch_model(model="model-b")
    assert agent.llm.default_model == "model-b"
    assert agent.executor.llm.default_model == "model-b"    # 执行器同步
    assert agent.planner.llm.default_model == "model-b"     # 规划器同步
    assert info["model"] == "model-b"


def test_vision_model_uses_own_endpoint():
    import config as cfg
    from models.vision import VisionModel

    saved = dict(cfg.VISION_CONFIG)
    try:
        cfg.VISION_CONFIG["api_key"] = "vision-key"
        cfg.VISION_CONFIG["base_url"] = "https://vision.example.com/v1"
        cfg.VISION_CONFIG["vision_model"] = ""

        v = VisionModel(vision_model="v-model")
        assert v.client.api_key == "vision-key"
        assert "vision.example.com" in str(v.client.base_url)
        assert v.vision_model == "v-model"
    finally:
        cfg.VISION_CONFIG.clear()
        cfg.VISION_CONFIG.update(saved)


def test_vision_model_auto_detect_with_ox_alpha():
    import config as cfg
    from models.vision import VisionModel

    saved_vision = dict(cfg.VISION_CONFIG)
    saved_model = cfg.LLM_CONFIG.get("default_model")
    try:
        cfg.VISION_CONFIG["vision_model"] = ""
        # 独立于 .env：显式把默认模型设为 ox-alpha 验证自动检测
        cfg.LLM_CONFIG["default_model"] = "stealth/ox-alpha"
        v = VisionModel()
        # 默认模型命中 RECOMMENDED_MODELS → 视觉也用同一模型
        assert "ox-alpha" in v.vision_model
    finally:
        cfg.VISION_CONFIG.clear()
        cfg.VISION_CONFIG.update(saved_vision)
        cfg.LLM_CONFIG["default_model"] = saved_model
