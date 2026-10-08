"""批次2 回归：后台任务的文件句柄与临时日志不再泄漏。"""
import glob
import os
import tempfile
import time

import pytest

from tools.terminal import TerminalTool


@pytest.fixture
def tool():
    return TerminalTool()


def bg_logs() -> set:
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "my_agent_bg_*.log")))


def start_bg(tool, cmd: str) -> str:
    r = tool.execute_json({"command": cmd, "background": True})
    assert r.success, r.error
    return [k for k in tool._jobs][-1]


class TestBackgroundJobHygiene:
    def test_handle_is_tracked(self, tool):
        jid = start_bg(tool, "echo bg-handle-test")
        time.sleep(1.0)
        assert "handle" in tool._jobs[jid], "必须保存日志句柄，否则无法关闭"

    def test_kill_closes_handle_but_keeps_log(self, tool):
        """kill 关闭句柄（修 fd 泄漏），但保留日志供事后排查。"""
        jid = start_bg(tool, "echo bg-kill-test")
        time.sleep(1.0)
        path = tool._jobs[jid]["out_path"]
        assert os.path.exists(path)
        r = tool.execute_json({"command": f"bg kill {jid}"})
        assert r.success
        assert "handle" not in tool._jobs.get(jid, {}), "句柄应已关闭并摘除"
        assert os.path.exists(path), "日志应保留，便于 kill 后查输出"
        out = tool.execute_json({"command": f"bg output {jid} 5"})
        assert out.success, "kill 后仍应能查看输出"

    def test_eviction_deletes_log_and_entry(self, tool):
        """任务表超限时，被淘汰的老任务其日志文件也要删掉（总量有界）。"""
        jid = start_bg(tool, "echo bg-evict-test")
        time.sleep(0.8)
        path = tool._jobs[jid]["out_path"]
        assert os.path.exists(path)
        for i in range(25):                     # 触发淘汰
            start_bg(tool, f"echo filler-{i}")
        time.sleep(1.5)
        assert len(tool._jobs) <= 21, "任务表未淘汰"
        assert jid not in tool._jobs, "最老的任务应已被淘汰"
        assert not os.path.exists(path), "被淘汰任务的临时日志应删除"

    def test_output_tail_is_bounded(self, tool):
        """刷屏任务只能回读尾部，不能把整个日志读进内存。"""
        jid = start_bg(tool, "for /L %i in (1,1,3000) do @echo line-%i-flood")
        time.sleep(2.5)
        r = tool.execute_json({"command": f"bg output {jid} 5"})
        assert r.success, r.error
        assert r.output.count("\n") <= 8, "只应返回请求的尾部行数"
        tool.execute_json({"command": f"bg kill {jid}"})

    def test_jobs_map_is_bounded(self, tool):
        """连续起任务不能无上限堆积（每个都握着一个文件句柄）。"""
        for i in range(25):
            start_bg(tool, f"echo job-{i}")
        time.sleep(1.5)
        assert len(tool._jobs) <= 21, f"任务表未淘汰: {len(tool._jobs)}"

    def test_release_is_idempotent(self, tool):
        jid = start_bg(tool, "echo idem")
        time.sleep(0.8)
        job = tool._jobs[jid]
        tool._release_job(job)
        tool._release_job(job)          # 重复调用不得抛错
        assert "handle" not in job


class TestBackgroundStartFailureCleansUp:
    """Popen 抛错时必须关句柄、删日志。
    每失败一次就漏一个句柄 + 一个空日志文件。"""

    def test_failed_spawn_leaves_no_handle_or_file(self, monkeypatch, tmp_path):
        import glob
        import os
        import tempfile

        import tools.terminal as t

        tool = t.TerminalTool()
        opened = []
        real_open = open

        def _tracking_open(path, *a, **k):
            f = real_open(path, *a, **k)
            opened.append(f)
            return f

        def _boom(*a, **k):
            raise OSError("shell 拉不起来")

        monkeypatch.setattr("builtins.open", _tracking_open)
        monkeypatch.setattr(t.subprocess, "Popen", _boom)

        pattern = os.path.join(tempfile.gettempdir(), "my_agent_bg_*.log")
        before = set(glob.glob(pattern))
        r = tool.execute_json({"command": "whatever", "background": True})

        assert r.success is False and "后台启动失败" in r.error
        assert all(f.closed for f in opened), "日志句柄没关"
        assert set(glob.glob(pattern)) == before, "失败时留下了空日志文件"


class TestBackgroundSandboxFailClosed:
    """沙箱开启时后台命令必须拒绝：裸 Popen 不受 AppContainer 约束，是绕过口子。"""

    @pytest.fixture(autouse=True)
    def _sandbox_off_by_default(self, monkeypatch):
        from config import SANDBOX_EXEC_CONFIG
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "off")

    def test_sandbox_enabled_refuses_background(self, monkeypatch, tool):
        from config import SANDBOX_EXEC_CONFIG
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setattr(TerminalTool, "_start_background",
                            lambda self, cmd: pytest.fail("沙箱下不该真的起后台进程"))
        r = tool.execute_json({"command": "echo nope", "background": True})
        assert r.success is False
        assert "沙箱" in r.error and "前台" in r.error
        assert "SANDBOX_EXECUTION=off" in r.error, "要给恢复途径"

    def test_background_unchanged_when_sandbox_off(self, tool):
        jid = start_bg(tool, "echo bg-allowed")
        assert jid in tool._jobs

    def test_foreground_still_routed_into_sandbox(self, monkeypatch, tool):
        import agent.sandbox as sandbox
        from config import SANDBOX_EXEC_CONFIG
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        seen = {}

        class _Outcome:
            returncode = 0
            stdout = "sandboxed-ok"
            stderr = ""
            error = ""

        monkeypatch.setattr(sandbox, "run_isolated",
                            lambda cmd, workspace: seen.update(cmd=cmd) or _Outcome())
        r = tool._run_command("echo fg")
        assert "echo fg" in seen.get("cmd", ""), "前台命令必须仍走沙箱"
        assert r.success and "sandboxed-ok" in r.output

    def test_embedded_terminal_shares_the_gate(self, monkeypatch):
        """内嵌终端后台转调 super()._run_command → 同一道闸门，无旁路。"""
        from config import SANDBOX_EXEC_CONFIG
        from tools.embedded_terminal import EmbeddedTerminalTool
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        t = EmbeddedTerminalTool(bridge_url="http://127.0.0.1:1/terminal")
        r = t._run_command("echo x", background=True)
        assert r.success is False and "沙箱" in r.error


class TestSecurityCriticalCommandApproval:
    """命令文本提到安全关键文件（且非只读）→ 强制人工确认，无人值守下拒绝。"""

    def test_redirect_into_critical_file_escalates(self, tool):
        r = tool.build_approval_request({"command": "echo x > agent/approval.py"})
        assert (r.risk_level, r.approval) == ("high", "on-request")

    def test_inplace_edit_of_config_escalates(self, tool):
        r = tool.build_approval_request({"command": "sed -i s/a/b/ config.py"})
        assert (r.risk_level, r.approval) == ("high", "on-request")

    def test_readonly_inspection_not_escalated(self, tool):
        r = tool.build_approval_request({"command": "git diff agent/approval.py"})
        assert r.approval == "auto", "只读命令不该被升级"
        assert r.risk_level == "low"

    def test_plain_command_unchanged(self, tool):
        r = tool.build_approval_request({"command": "echo hello"})
        assert (r.risk_level, r.approval) == ("low", "auto")

    def test_never_policy_refuses_critical_command(self, tool):
        from agent.approval import ApprovalPolicy
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        d = p.decide(tool.build_approval_request({"command": "echo x > .env"}))
        assert d.allowed is False
