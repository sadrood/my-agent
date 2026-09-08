"""
OS 级沙箱（Windows AppContainer）测试。

单测全部 Fake 化（不真启动容器，无平台依赖）：
- 配置开关与可用性探测
- fail-closed：沙箱不可用/启动失败时终端返回错误，绝不回退明文执行
- 终端集成：sandbox 开启时前台命令走沙箱，后台命令不走

真实 AppContainer 冒烟属于手工集成测试（会在容器内起真实进程），
不放进常规测试集，避免测试耗时与平台抖动。
"""
import sys
import time

import pytest

import agent.sandbox as sandbox
import tools.terminal as tools_terminal
from config import SANDBOX_EXEC_CONFIG
from tools.terminal import TerminalTool


@pytest.fixture(autouse=True)
def reset_sandbox_config(monkeypatch):
    """每条用例强制恢复 off，防止污染其他测试。"""
    monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "off")
    yield


class TestConfigGating:
    def test_disabled_by_default(self):
        assert sandbox.sandbox_mode() == "off"
        assert sandbox.sandbox_enabled() is False

    def test_enabled_when_configured(self, monkeypatch):
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        assert sandbox.sandbox_enabled() is True

    def test_run_isolated_rejects_when_off(self):
        outcome = sandbox.run_isolated("echo hi", workspace=".")
        assert outcome.sandboxed is False
        assert "未启用" in outcome.error

    def test_appcontainer_available_on_windows_only(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_IS_WINDOWS", False)
        assert sandbox.appcontainer_available() is False


class TestTerminalIntegration:
    def _make_tool(self):
        return TerminalTool()

    def test_sandbox_disabled_uses_normal_path(self, monkeypatch):
        """默认 off：走 _run_foreground，不触碰沙箱模块。"""
        tool = self._make_tool()
        captured = {}

        def fake_foreground(cmd):
            captured["cmd"] = cmd
            return _ok()

        monkeypatch.setattr(tool, "_run_foreground", fake_foreground)
        result = tool._run_command("echo hi")
        assert result.success is True
        assert captured["cmd"] == "echo hi"

    def test_sandbox_enabled_routes_foreground_through_sandbox(self, monkeypatch):
        """开启沙箱：前台命令走 run_isolated，结果正确转换。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")

        captured = {}

        def fake_run_isolated(command, workspace):
            captured["command"] = command
            captured["workspace"] = workspace
            return sandbox.SandboxOutcome(returncode=0, stdout="hello-sandboxed\n")

        monkeypatch.setattr(sandbox, "run_isolated", fake_run_isolated)
        result = self._make_tool()._run_command("echo hello-sandboxed")
        assert captured["command"] == "echo hello-sandboxed"
        assert result.success is True
        assert "hello-sandboxed" in result.output

    def test_sandbox_mechanism_failure_is_fail_closed(self, monkeypatch):
        """容器创建失败：返回错误并保留沙箱标记，绝不回退明文执行。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")

        def fake_run_isolated(command, workspace):
            return sandbox.SandboxOutcome(returncode=-1, error="CreateAppContainerProfile 失败")

        monkeypatch.setattr(sandbox, "run_isolated", fake_run_isolated)
        result = self._make_tool()._run_command("echo should-not-run")
        assert result.success is False
        assert "沙箱执行失败" in result.error
        assert "SANDBOX_EXECUTION=off" in result.error   # 附恢复途径提示
        assert "should-not-run" not in result.output

    def test_sandbox_denied_output_gets_guidance(self, monkeypatch):
        """容器内「拒绝访问」类失败：错误信息附带引导文案，避免模型盲试。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setattr(
            sandbox, "run_isolated",
            lambda cmd, workspace: sandbox.SandboxOutcome(
                returncode=1, stderr="Access is denied."),
        )
        result = self._make_tool()._run_command("some-tool.exe --flag")
        assert result.success is False
        assert "改用 python 工具" in result.error
        assert "SANDBOX_EXECUTION=off" in result.error

    def test_sandbox_normal_failure_no_guidance(self, monkeypatch):
        """非「拒绝访问」类失败（如命令自身报错）：不加沙箱引导，避免噪音。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setattr(
            sandbox, "run_isolated",
            lambda cmd, workspace: sandbox.SandboxOutcome(returncode=2, stderr="boom"),
        )
        result = self._make_tool()._run_command("exit 2")
        assert "改用 python 工具" not in result.error

    def test_sandbox_unavailable_platform_fails_closed(self, monkeypatch):
        """非 Windows 平台开启沙箱：报平台不可用而非明文执行。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setattr(sandbox, "appcontainer_available", lambda: False)
        result = self._make_tool()._run_command("echo hi")
        assert result.success is False
        assert "不可用" in result.error

    def test_sandbox_command_failure_reports_returncode(self, monkeypatch):
        """沙箱内命令失败（exit 2）：正常回传，不当作机制故障。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setattr(
            sandbox, "run_isolated",
            lambda cmd, workspace: sandbox.SandboxOutcome(returncode=2, stderr="boom"),
        )
        result = self._make_tool()._run_command("exit 2")
        assert result.success is False
        assert "返回码: 2" in result.error
        assert "boom" in result.output

    def test_background_commands_bypass_sandbox(self, monkeypatch):
        """后台任务不进沙箱（保持原路径），便于 bg 子系统管理。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        tool = self._make_tool()
        monkeypatch.setattr(tool, "_start_background", lambda cmd: _ok())
        result = tool._run_command("echo hi", background=True)
        assert result.success is True


class TestInterpreterGrants:
    """解释器目录 AC RX 授权：让容器内能跑 python/node/git（best-effort）。"""

    def test_grants_candidate_dirs_with_inheritance(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        venv_dir = tmp_path / "venv"
        base_dir = tmp_path / "py313"
        py_dir = tmp_path / "py"           # which 命中的解释器所在目录
        for d in (venv_dir, base_dir, py_dir):
            d.mkdir()
        which = lambda n: {"python.exe": str(py_dir / "python.exe")}.get(n)

        granted = sandbox._grant_interpreter_aces(
            prefix=str(venv_dir), base_prefix=str(base_dir), which_fn=which)

        targets = {c[1] for c in calls}
        assert str(venv_dir) in targets
        assert str(base_dir) in targets
        # which 命中的解释器：授权其所在目录（继承覆盖 exe）
        assert str(py_dir) in targets
        assert all("*S-1-15-2-1:(OI)(CI)(RX)" in c for c in calls)
        assert set(granted) == targets

    def test_denied_grants_swallowed(self, tmp_path, monkeypatch):
        """目录授权被拒（如 Program Files）：静默跳过，不抛异常。"""
        def fake_run(args, **kwargs):
            class R:
                returncode = 5
            return R()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        d = tmp_path / "venv"
        d.mkdir()
        granted = sandbox._grant_interpreter_aces(
            prefix=str(d), base_prefix=str(tmp_path), which_fn=lambda n: None)
        assert granted == []

    def test_duplicate_candidates_deduped(self, tmp_path, monkeypatch):
        """venv 与 which 命中同一目录：只授权一次。"""
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        d = tmp_path / "venv"
        d.mkdir()
        exe = d / "python.exe"
        exe.write_bytes(b"")   # which 命中同一 venv 里的 python
        sandbox._grant_interpreter_aces(
            prefix=str(d), base_prefix=str(tmp_path), which_fn=lambda n: str(exe))
        targets = [c[1] for c in calls]
        assert targets.count(str(d)) == 1


class TestForegroundTimeout:
    """前台命令超时可配置（实战发现 60s 掐断负载下的全量测试）。"""

    def test_fg_timeout_configurable(self, monkeypatch):
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "terminal_fg_timeout", 0.5)
        tool = TerminalTool()
        cmd = f'"{sys.executable}" -c "import time; time.sleep(3)"'
        t0 = time.time()
        result = tool._run_command(cmd)
        elapsed = time.time() - t0
        assert result.success is False
        assert "超时" in result.error
        assert "background" in result.error or "后台" in result.error
        assert elapsed < 2.5    # 没等满 3s 睡眠

    def test_fg_timeout_default_from_config(self, monkeypatch):
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "terminal_fg_timeout", 300)
        assert tools_terminal._foreground_timeout() == 300.0
        monkeypatch.setitem(TOOL_CONFIG, "terminal_fg_timeout", "bad-value")
        assert tools_terminal._foreground_timeout() == 120.0   # 非法值回退默认


def _ok():
    from tools.base import ToolResult
    return ToolResult(success=True, output="(沙箱测试桩)")


class TestSandboxCapabilityListing:
    """沙箱能力清单化：额外只读目录授权 + 网络 capability 开关。"""

    def test_grant_extra_dirs_icacls_rx(self, tmp_path, monkeypatch):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

            class R:
                returncode = 0
            return R()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        d1 = tmp_path / "shared_libs"
        d2 = tmp_path / "datasets"
        d1.mkdir()
        d2.mkdir()
        missing = str(tmp_path / "nope")      # 不存在的目录跳过

        granted = sandbox._grant_extra_dirs([str(d1), str(d2), missing, str(d1)])
        targets = [c[1] for c in calls]
        assert targets == [str(d1), str(d2)]  # 去重 + 跳过不存在
        assert all("*S-1-15-2-1:(OI)(CI)(RX)" in c for c in calls)
        assert granted == [str(d1), str(d2)]

    def test_grant_extra_dirs_denied_swallowed(self, tmp_path, monkeypatch):
        def fake_run(args, **kwargs):
            class R:
                returncode = 5
            return R()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        d = tmp_path / "x"
        d.mkdir()
        assert sandbox._grant_extra_dirs([str(d)]) == []

    @pytest.mark.skipif(not sandbox._IS_WINDOWS, reason="仅 Windows")
    def test_build_capabilities_disabled_by_default(self, monkeypatch):
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "allow_network", False)
        api = sandbox._load_apis()
        arr, keepalive = sandbox._build_capabilities(api)
        assert arr is None
        assert keepalive == []

    @pytest.mark.skipif(not sandbox._IS_WINDOWS, reason="仅 Windows")
    def test_build_capabilities_network_enabled(self, monkeypatch):
        """allow_network=true：三个网络 capability SID 由字符串构造并组装。"""
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "allow_network", True)
        api = sandbox._load_apis()

        converted = []

        def fake_convert(sid_string, sid):
            converted.append(sid_string)
            sid._obj.value = 0x2222
            return True

        monkeypatch.setitem(api, "advapi32",
                            _StubAdvapi32(fake_convert))
        arr, keepalive = sandbox._build_capabilities(api)

        assert arr is not None
        assert len(arr) == 3
        assert converted == ["S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-5"]
        assert all(e.Sid == 0x2222 and e.Attributes == sandbox.SE_GROUP_ENABLED
                   for e in arr)
        freed = []
        monkeypatch.setitem(api, "kernel32",
                            _StubKernel32(freed))
        sandbox._free_capabilities(api, keepalive)
        assert len(freed) == 3               # 每个 SID 一次 LocalFree


class _StubAdvapi32:
    def __init__(self, convert):
        self.ConvertStringSidToSidW = convert


class _StubKernel32:
    def __init__(self, freed):
        self.LocalFree = lambda h: freed.append(h)
