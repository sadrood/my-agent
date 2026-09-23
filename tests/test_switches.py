"""各"总开关"的生效性回归（此前一批开关是"定义了却没人读"）。

实测故障（2026-09-22 审计）：
- `IMAGE_GEN_ENABLED` / `VIDEO_GEN_ENABLED` / `TTS_ENABLED` / `VIDEO_EDIT_ENABLED`
  只在 `models/*.py::is_configured()` 里被读，而那个函数**全仓库没有任何调用点**
  —— 设成 false 也照样注册、模型照样调上游接口并计费；
- `MCP_ENABLED` 定义了却无人读，用户在 .env 里关不掉 MCP；
- `SANDBOX_EXECUTION` 没归一化，写 `OFF` 会因为大小写敏感比较被判成"开着"；
- `--max-replans` 解析了却没接进 AgentConfig；`rollout_enabled=not args.no_rollout`
  恒为 bool，把 `.env` 的 `ROLLOUT_ENABLED=false` / `MY_AGENT_MINIMAL=1` 顶掉。
"""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from config import PROJECT_ROOT


def _sub(code: str, env: dict):
    """在子进程里跑（这些开关都在导入期解析）。"""
    return subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT),
                          env={**os.environ, **env}, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


class TestFeatureToggles:
    @pytest.mark.parametrize("flag,tool", [
        ("IMAGE_GEN_ENABLED", "image_gen"),
        ("VIDEO_GEN_ENABLED", "video_gen"),
        ("TTS_ENABLED", "tts"),
        ("VIDEO_EDIT_ENABLED", "video_edit"),
    ])
    def test_disabled_tool_is_not_registered(self, flag, tool):
        code = ("from tools.tool_manager import ToolManager\n"
                f"print('REGISTERED' if '{tool}' in ToolManager().list_tools() else 'ABSENT')")
        off = _sub(code, {flag: "false"})
        assert "ABSENT" in off.stdout, f"{flag}=false 没生效：{off.stdout}{off.stderr[-300:]}"
        on = _sub(code, {flag: "true"})
        assert "REGISTERED" in on.stdout, f"{flag}=true 时工具反而没了"


class TestSandboxToggle:
    @pytest.mark.parametrize("value,expected", [
        ("OFF", "False"), ("off", "False"), ("False", "False"), ("", "False"),
        ("AppContainer", "True"), ("APPCONTAINER", "True"), ("appcontainer", "True"),
    ])
    def test_mode_is_normalized(self, value, expected):
        """写 `OFF` / `False` 必须是"关闭"，不能因为大小写被判成开着。"""
        p = _sub("from agent.sandbox import sandbox_enabled; print(sandbox_enabled())",
                 {"SANDBOX_EXECUTION": value})
        assert p.returncode == 0, p.stderr[-300:]
        assert p.stdout.strip() == expected, f"SANDBOX_EXECUTION={value!r} 解析错了"


class TestApprovalModeNormalization:
    @pytest.mark.parametrize("value,expected", [
        ("Never", "never"), ("NEVER", "never"), ("On-Failure", "on-failure"),
        (" On-Request ", "on-request"), ("Untrusted", "untrusted"),
    ])
    def test_approval_policy_case_tolerated(self, value, expected):
        """`APPROVAL_POLICY=Never` 不该让启动直接 traceback。"""
        code = ("from agent.approval import ApprovalPolicy\n"
                "from config import APPROVAL_CONFIG\n"
                "print(ApprovalPolicy(mode=APPROVAL_CONFIG['approval_policy'],"
                " sandbox_mode='workspace-write').mode)")
        p = _sub(code, {"APPROVAL_POLICY": value})
        assert p.returncode == 0, f"{value} 让配置崩了：{p.stderr[-400:]}"
        assert p.stdout.strip() == expected

    @pytest.mark.parametrize("value,expected", [
        ("Workspace-Write", "workspace-write"), ("READ-ONLY", "read-only"),
        ("Danger-Full-Access", "danger-full-access"),
    ])
    def test_sandbox_mode_case_tolerated(self, value, expected):
        """`SANDBOX_MODE=Workspace-Write` 同理（会撞 SANDBOX_LEVELS 的键）。"""
        code = ("from agent.approval import ApprovalPolicy\n"
                "from config import APPROVAL_CONFIG\n"
                "print(ApprovalPolicy(mode='never',"
                " sandbox_mode=APPROVAL_CONFIG['sandbox_mode']).sandbox_mode)")
        p = _sub(code, {"SANDBOX_MODE": value})
        assert p.returncode == 0, f"{value} 让配置崩了：{p.stderr[-400:]}"
        assert p.stdout.strip() == expected


class TestMcpToggle:
    def test_disabled_skips_connecting(self, monkeypatch):
        """MCP_ENABLED=false 必须真的不连服务器（此前是个死开关）。"""
        from agent import Agent
        from config import MCP_CONFIG

        monkeypatch.setitem(MCP_CONFIG, "enabled", False)
        inst = object.__new__(Agent)            # 绕过 __init__，只测这一段逻辑
        inst.config = SimpleNamespace(mcp_servers=[{"name": "x", "command": "x"}])
        connected = []
        inst.tool_manager = SimpleNamespace(
            connect_mcp_server=lambda *a, **k: connected.append(a))
        inst._mcp_connected = False
        inst._log = lambda *a, **k: None

        Agent._init_mcp_servers(inst)
        assert connected == [], "MCP_ENABLED=false 没有拦住连接"

    def test_enabled_still_connects(self, monkeypatch):
        from agent import Agent
        from config import MCP_CONFIG

        monkeypatch.setitem(MCP_CONFIG, "enabled", True)
        inst = object.__new__(Agent)
        inst.config = SimpleNamespace(mcp_servers=[{"name": "x", "command": "cmd"}])
        connected = []
        inst.tool_manager = SimpleNamespace(
            connect_mcp_server=lambda *a, **k: connected.append(a))
        inst._mcp_connected = False
        inst._log = lambda *a, **k: None

        Agent._init_mcp_servers(inst)
        assert connected, "开着的时候反而没连"


class TestBuildConfigWiring:
    @staticmethod
    def _args(**over):
        base = dict(max_steps=5, quiet=False, no_vision=False, approval=None,
                    sandbox=None, guardian=None, supervisor=None, no_rollout=False,
                    max_replans=7, max_step_ops=3, session=None, exec_mode="loop",
                    max_ops=None, no_stream=False, model=None, base_url=None,
                    api_key=None)
        base.update(over)
        return SimpleNamespace(**base)

    def test_max_replans_is_wired(self):
        """`--max-replans` 此前解析了却从未传进 AgentConfig。"""
        from main import build_config
        assert build_config(self._args(max_replans=9)).max_replans == 9

    def test_rollout_not_forced_on(self):
        """不传 `--no-rollout` 时不能顶掉 .env 的总开关。"""
        from main import build_config
        from config import ROLLOUT_CONFIG
        cfg = build_config(self._args())
        assert cfg.rollout_enabled == ROLLOUT_CONFIG.get("enabled", True)

    def test_no_rollout_still_forces_off(self):
        from main import build_config
        assert build_config(self._args(no_rollout=True)).rollout_enabled is False

    def test_approval_interactive_follows_config(self):
        """`APPROVAL_INTERACTIVE=false` 不该被 CLI 的硬编码 True 顶掉。"""
        from main import build_config
        from config import APPROVAL_CONFIG
        cfg = build_config(self._args())
        assert cfg.approval_interactive == APPROVAL_CONFIG.get("interactive", True)
