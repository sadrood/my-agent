"""配置中心的解析健壮性回归（此前没有测试文件）。

实测故障（2026-09-22 审计）：`.env` 里把数字项**留空**（`LLM_DEFAULT_TEMPERATURE=`）
会让 `import config` 直接崩 —— `os.getenv(key, "默认")` 在变量被设成空串时返回 `""`
而不是默认值，`float("")` 抛 `ValueError`。而 `.env.example` 自己就用
`VISION_API_KEY=` 这种留空写法引导用户按需填值，照抄给数字项留空太自然了。

表现是 `python main.py` 连欢迎界面都出不来，只有一行裸 traceback。
"""
import os
import subprocess
import sys

import pytest

from config import PROJECT_ROOT


def _import_config_with(env_overrides: dict):
    """在子进程里 import config（bug 发生在导入期，必须另起进程才测得到）。"""
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import config; print('IMPORT_OK')"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


@pytest.mark.parametrize("key", [
    "LLM_DEFAULT_TEMPERATURE",          # float
    "LLM_DEFAULT_MAX_OUTPUT_TOKENS",    # int
    "LOOP_HARD_CAP",                    # int（本文件里写了空值保护的那两处之一）
    "MAX_LOOP_OPS",
    "IMAGE_GEN_SIZE",
    "BROWSER_VIEWPORT_WIDTH",
])
def test_empty_value_does_not_crash_import(key):
    p = _import_config_with({key: ""})
    assert p.returncode == 0, (
        f"{key}= 让 import config 崩了：\n{(p.stderr or '')[-800:]}")
    assert "IMPORT_OK" in p.stdout


def test_empty_value_falls_back_to_default():
    """留空 = 用默认值，而不是变成 ""。"""
    p = subprocess.run(
        [sys.executable, "-c",
         "from config import LLM_CONFIG; print(LLM_CONFIG['default_temperature'])"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "LLM_DEFAULT_TEMPERATURE": ""},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    assert p.returncode == 0, (p.stderr or "")[-500:]
    assert p.stdout.strip() == "0.7"


def test_zero_value_is_not_treated_as_empty():
    """`0` 是合法取值，不能被"空值清理"顺手删掉。"""
    p = subprocess.run(
        [sys.executable, "-c",
         "from config import LLM_CONFIG; print(LLM_CONFIG['default_temperature'])"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "LLM_DEFAULT_TEMPERATURE": "0"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    assert p.returncode == 0, (p.stderr or "")[-500:]
    assert p.stdout.strip() == "0.0"


class TestCursorOverlaySwitch:
    """`COMPUTER_CURSOR_OVERLAY` 的取值形式：文档与该键的 docstring 一直写的是 `=1`。

    回归背景：开关从 `os.getenv(...) in ("1","true","on","yes")` 挪进 config.py 时
    只写成 `== "true"`，于是文档里写的 `=1` 静默失效（实测：os.getenv 读到 1，
    config 里却是 False，浮层不显示）。
    """

    def _value_with(self, raw: str) -> str:
        p = subprocess.run(
            [sys.executable, "-c",
             "from config import COMPUTER_USE_CONFIG as c; print(c['cursor_overlay'])"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "COMPUTER_CURSOR_OVERLAY": raw},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        assert p.returncode == 0, (p.stderr or "")[-500:]
        return p.stdout.strip()

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "on", "yes", "  1  "])
    def test_on_forms(self, raw):
        assert self._value_with(raw) == "True"

    @pytest.mark.parametrize("raw", ["0", "false", "", "no", "off"])
    def test_off_forms(self, raw):
        assert self._value_with(raw) == "False"


class TestTestCommandPerPlatform:
    """测试命令的默认值跟着平台选 venv 路径（Windows 是 Scripts、POSIX 是 bin）。

    写死 Windows 那份时，Linux 上会照着一个不存在的解释器跑测试：一路失败，
    且 EDIT_PREFLIGHT 打开时每次 edit 都会被回滚。
    """

    def test_default_follows_platform(self):
        from config import resolve_test_command
        assert resolve_test_command({}, is_windows=True) == \
            ".venv\\Scripts\\python -m pytest tests -q"
        assert resolve_test_command({}, is_windows=False) == \
            ".venv/bin/python -m pytest tests -q"

    def test_explicit_config_wins_on_every_platform(self):
        from config import resolve_test_command
        for win in (True, False):
            assert resolve_test_command({"TEST_COMMAND": "mypy ."},
                                        is_windows=win) == "mypy ."

    def test_blank_value_falls_back_to_platform_default(self):
        """`.env` 里 `TEST_COMMAND=` 留空是常见写法，不能当成"用户填了个空命令"。"""
        from config import resolve_test_command
        assert resolve_test_command({"TEST_COMMAND": "   "},
                                    is_windows=False).startswith(".venv/bin/python")

    def test_import_picks_this_hosts_path(self):
        """真实导入时也按本机平台选 —— 在 Linux 上跑测试会直接暴露漏改。"""
        if os.getenv("TEST_COMMAND"):
            pytest.skip("本机显式设了 TEST_COMMAND，默认值不参与")
        import config
        expected = ".venv\\Scripts\\python" if os.name == "nt" else ".venv/bin/python"
        assert config.TEST_CONFIG["command"].startswith(expected)


class TestLlmConfigProvenance:
    """`.env` 的 LLM_* 被 ANTHROPIC_* 静默顶掉时要能查得出来。

    取值顺序 MY_AGENT_* → ANTHROPIC_* → LLM_*：环境里带着 ANTHROPIC_* 时，
    `.env` 的端点/模型会被忽略 —— 看配置是一个端点、实际打的是另一个。
    """

    def test_no_shadowing_when_only_llm_vars(self):
        from config import llm_config_provenance
        p = llm_config_provenance({
            "LLM_BASE_URL": "https://token.sensenova.cn/v1",
            "LLM_DEFAULT_MODEL": "deepseek-v4-flash",
        })
        assert p["shadowed"] == []
        assert p["base_url_source"] == "LLM_BASE_URL"
        assert p["model_source"] == "LLM_DEFAULT_MODEL"
        assert p["base_url"] == "https://token.sensenova.cn/v1"

    def test_anthropic_vars_shadow_env_file(self):
        from config import llm_config_provenance
        p = llm_config_provenance({
            "LLM_BASE_URL": "https://token.sensenova.cn/v1",
            "LLM_DEFAULT_MODEL": "deepseek-v4-flash",
            "ANTHROPIC_BASE_URL": "http://172.16.10.242:3000",
            "ANTHROPIC_MODEL": "deepseek-flash",
        })
        shadowed = {s["set"]: s for s in p["shadowed"]}
        assert set(shadowed) == {"LLM_BASE_URL", "LLM_DEFAULT_MODEL"}
        assert shadowed["LLM_BASE_URL"]["overridden_by"] == "ANTHROPIC_BASE_URL"
        assert shadowed["LLM_BASE_URL"]["effective"] == "http://172.16.10.242:3000"
        assert p["base_url"] == "http://172.16.10.242:3000"

    def test_my_agent_vars_win_over_anthropic(self):
        from config import llm_config_provenance
        p = llm_config_provenance({
            "MY_AGENT_BASE_URL": "https://my.example/v1",
            "ANTHROPIC_BASE_URL": "http://gateway:3000",
            "LLM_BASE_URL": "https://env.example/v1",
        })
        shadowed = {s["set"]: s["overridden_by"] for s in p["shadowed"]}
        assert shadowed["LLM_BASE_URL"] == "MY_AGENT_BASE_URL"
        assert p["base_url"] == "https://my.example/v1"

    def test_alias_alone_is_not_reported_as_shadowing(self):
        """只设了 ANTHROPIC_*、没设 LLM_*：这是别名在**填坑**，不是顶掉谁。"""
        from config import llm_config_provenance
        p = llm_config_provenance({"ANTHROPIC_BASE_URL": "http://gateway:3000"})
        assert p["shadowed"] == []

    def test_api_key_value_is_never_returned(self):
        """告警只该报变量名与端点，绝不能把密钥带进返回值（会被打进日志/终端）。"""
        from config import llm_config_provenance
        secret = "sk-super-secret-value"
        p = llm_config_provenance({
            "LLM_API_KEY": secret,
            "ANTHROPIC_API_KEY": "sk-anthropic-value",
            "ANTHROPIC_BASE_URL": "http://gateway:3000",
        })
        key_entry = [s for s in p["shadowed"] if s["set"] == "LLM_API_KEY"]
        assert len(key_entry) == 1
        assert key_entry[0]["overridden_by"] == "ANTHROPIC_API_KEY"
        assert key_entry[0]["effective"] == ""
        assert secret not in str(p) and "sk-anthropic-value" not in str(p)

    def test_key_shadowing_only_when_both_set(self):
        from config import llm_config_provenance
        assert llm_config_provenance({"ANTHROPIC_API_KEY": "k"})["shadowed"] == []
