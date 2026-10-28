"""配置启动校验：明显不可用的值要在启动时被指出来。"""
import pytest

import config


def _good() -> dict:
    """一套最小的合法配置（与真实 .env 无关，避免用例受环境影响）。"""
    return {
        "LLM_CONFIG": {
            "base_url": "https://api.example.com/v1",
            "default_temperature": 0.7,
            "default_max_output_tokens": 4096,
            "fixed_temperature": None,
            "max_retries": 2,
            "retry_base_delay": 2.0,
            "timeout": 300,
            "auth_retry_max": 2,
            "auth_retry_delay": 2.0,
            "fallback_after": 2,
            "fallback_probe_seconds": 300,
            "fallback_models": [],
            "fallback_presets": {},
        },
        "COMPACT_CONFIG": {"token_threshold": 0, "window_ratio": 0.75, "keep_last": 20},
        "CONTEXT_STORE_CONFIG": {"threshold_chars": 3000, "compact_mode": "pointer"},
        "RUN_LOG_CONFIG": {"incremental_interval_seconds": 5},
        "QQBOT_CONFIG": {"max_concurrent_tasks": 2, "rate_per_minute": 6,
                         "max_chars": 2000, "task_timeout": 1800, "session_ttl": 7200},
        "SESSION_CONFIG": {"max_sessions": 50},
        "TOOL_CONFIG": {"max_step_ops": 12, "max_parallel_tools": 4,
                        "tool_timeout": 300, "max_loop_ops": 80},
        "VISION_CONFIG": {"timeout": 30, "fallback_timeout": 60,
                          "fallback_models": [], "fallback_presets": {}},
        "BROWSER_CONFIG": {"viewport_width": 1280, "viewport_height": 720,
                           "download_timeout": 30},
        "UPGRADE_CONFIG": {"timeout_seconds": 300},
    }


@pytest.fixture
def cfg(monkeypatch):
    for name, value in _good().items():
        monkeypatch.setattr(config, name, value)
    return config


def test_good_config_passes(cfg):
    assert cfg.validate_config() == []


def test_boundary_values_are_allowed(cfg):
    cfg.LLM_CONFIG["default_temperature"] = 0
    cfg.COMPACT_CONFIG["window_ratio"] = 1
    cfg.QQBOT_CONFIG["task_timeout"] = 0        # 0 = 不限时
    cfg.QQBOT_CONFIG["session_ttl"] = 0         # 0 = 不回收
    cfg.SESSION_CONFIG["max_sessions"] = 0      # 0 = 不清理
    assert cfg.validate_config() == []


def test_negative_or_zero_timeout_flagged(cfg):
    cfg.TOOL_CONFIG["tool_timeout"] = 0
    problems = cfg.validate_config()
    assert any("TOOL_CONFIG.tool_timeout" in p for p in problems)


def test_non_numeric_value_flagged_readably(cfg):
    cfg.LLM_CONFIG["default_temperature"] = "hot"
    problems = cfg.validate_config()
    assert any("应为数字" in p and "default_temperature" in p for p in problems)


def test_ratio_out_of_range_flagged(cfg):
    cfg.COMPACT_CONFIG["window_ratio"] = 1.5
    assert any("window_ratio" in p for p in cfg.validate_config())


def test_base_url_must_be_http(cfg):
    cfg.LLM_CONFIG["base_url"] = "api.example.com/v1"
    assert any("base_url" in p for p in cfg.validate_config())


def test_unknown_compact_mode_flagged(cfg):
    cfg.CONTEXT_STORE_CONFIG["compact_mode"] = "summery"
    assert any("compact_mode" in p for p in cfg.validate_config())


def test_fallback_referencing_undefined_preset_flagged(cfg):
    cfg.LLM_CONFIG["fallback_models"] = ["glm-5.2@sn2"]
    problems = cfg.validate_config()
    assert any("sn2" in p and "LLM_FALLBACK_ENDPOINT_SN2_BASE_URL" in p for p in problems)


def test_fallback_with_defined_preset_passes(cfg):
    cfg.LLM_CONFIG["fallback_models"] = ["glm-5.2@sn2", "kimi-k3"]
    cfg.LLM_CONFIG["fallback_presets"] = {"sn2": {"base_url": "https://x", "api_key": "k"}}
    assert cfg.validate_config() == []


def test_vision_fallback_checked_with_vision_prefix(cfg):
    cfg.VISION_CONFIG["fallback_models"] = ["deepseek-flash@sn2"]
    problems = cfg.validate_config()
    assert any("VISION_FALLBACK_ENDPOINT_SN2_BASE_URL" in p for p in problems)


def test_empty_fallback_model_flagged(cfg):
    cfg.LLM_CONFIG["fallback_models"] = ["ghost", "  "]
    # 空白条目通常已被配置解析过滤；这里直接喂给校验确认不会静默放过空名
    problems = cfg.validate_config()
    assert any("空模型名" in p for p in problems)


def test_main_refuses_to_start_on_invalid_config(monkeypatch, capsys):
    import main

    monkeypatch.setattr(config, "validate_config", lambda: ["LLM_CONFIG.timeout 必须 > 0，当前 0"])
    monkeypatch.setattr(main.sys, "argv", ["main.py", "-p", "任务"])
    called = []
    monkeypatch.setattr(main, "run_once", lambda **kw: called.append(kw))
    assert main.main() == 2
    assert called == [], "配置有误时不能继续执行任务"
    err = capsys.readouterr().err
    assert "配置有误" in err and "LLM_CONFIG.timeout" in err


def test_doctor_is_not_blocked_by_invalid_config(monkeypatch, capsys):
    import main
    from agent import doctor

    monkeypatch.setattr(config, "validate_config", lambda: ["boom"])
    monkeypatch.setattr(doctor, "run_doctor", lambda **kw: [])
    monkeypatch.setattr(doctor, "render_report", lambda report: "自检报告")
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--doctor"])
    main.main()
    assert "自检报告" in capsys.readouterr().out
