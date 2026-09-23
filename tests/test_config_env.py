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
