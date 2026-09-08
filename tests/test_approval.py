"""
审批策略与命令安全测试（主流 approval_policy 语义）。
"""
import pytest

from agent.approval import ApprovalPolicy, CommandSafety
from agent.execpolicy import ExecPolicy
from config import APPROVAL_CONFIG
from tools.base import ApprovalRequest


def req(risk="low", min_sandbox="read-only", tool="t", approval="auto"):
    return ApprovalRequest(
        tool_name=tool,
        arguments={},
        command="test",
        risk_level=risk,
        min_sandbox_mode=min_sandbox,
        approval=approval,
    )


class TestCommandSafety:
    def test_readonly_commands(self):
        assert CommandSafety.classify("dir") == "low"
        assert CommandSafety.classify("ls -la") == "low"
        assert CommandSafety.classify("pip list") == "low"
        assert CommandSafety.classify("git status") == "low"
        assert CommandSafety.classify("echo hello") == "low"
        assert CommandSafety.classify("python --version") == "low"

    def test_blocked_commands(self):
        assert CommandSafety.classify("rm -rf /") == "blocked"
        assert CommandSafety.classify("rm -rf ~") == "blocked"
        assert CommandSafety.classify("mkfs.ext4 /dev/sda1") == "blocked"
        assert CommandSafety.classify("shutdown -s") == "blocked"
        assert CommandSafety.classify("del /f /s C:\\Windows") == "blocked"
        assert CommandSafety.classify(":(){ :|:& };:") == "blocked"
        assert CommandSafety.classify("git push --force origin main") == "blocked"

    def test_high_risk_commands(self):
        assert CommandSafety.classify("rm -rf build/") == "high"
        assert CommandSafety.classify("pip uninstall requests") == "high"
        assert CommandSafety.classify("git reset --hard HEAD~1") == "high"
        assert CommandSafety.classify("taskkill /f /im chrome.exe") == "high"

    def test_unknown_is_medium(self):
        assert CommandSafety.classify("some-custom-tool --flag") == "medium"


class TestApprovalPolicy:
    def test_never_allows_medium(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        d = p.decide(req(risk="medium", min_sandbox="workspace-write"))
        assert d.allowed is True

    def test_never_still_blocks_blocked(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        d = p.decide(req(risk="blocked"))
        assert d.allowed is False

    def test_never_danger_full_access_allows_blocked(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="danger-full-access", interactive=False)
        d = p.decide(req(risk="blocked", min_sandbox="danger-full-access"))
        assert d.allowed is True

    def test_sandbox_level_insufficient(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="read-only", interactive=False)
        d = p.decide(req(risk="low", min_sandbox="workspace-write"))
        assert d.allowed is False
        assert "沙箱" in d.reason

    def test_untrusted_asks_for_medium(self):
        # 非交互 + untrusted → 询问被拒（保守）
        p = ApprovalPolicy(mode="untrusted", sandbox_mode="workspace-write", interactive=False)
        d = p.decide(req(risk="medium", min_sandbox="workspace-write"))
        assert d.allowed is False
        d2 = p.decide(req(risk="low"))
        assert d2.allowed is True

    def test_untrusted_approver_callback(self):
        answers = {"first": True, "second": False}

        def approver(r):
            return answers.pop("first") if "first" in answers else answers.pop("second")

        p = ApprovalPolicy(mode="untrusted", sandbox_mode="workspace-write",
                           interactive=False, approver=approver)
        assert p.decide(req(risk="high", min_sandbox="workspace-write")).allowed is True
        assert p.decide(req(risk="high", min_sandbox="workspace-write")).allowed is False

    def test_on_failure_high_risk_requires_approval(self):
        p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write", interactive=False)
        d = p.decide(req(risk="high", min_sandbox="workspace-write"))
        assert d.allowed is False  # 非交互保守拒绝
        d2 = p.decide(req(risk="medium", min_sandbox="workspace-write"))
        assert d2.allowed is True

    def test_on_failure_high_risk_in_full_access(self):
        p = ApprovalPolicy(mode="on-failure", sandbox_mode="danger-full-access", interactive=False)
        d = p.decide(req(risk="high", min_sandbox="danger-full-access"))
        assert d.allowed is True

    def test_on_request_approval_flag(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        r = req(risk="low", approval="on-request")
        d = p.decide(r)
        assert d.allowed is False  # 工具要求批准但策略 never

        p2 = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                            interactive=False, approver=lambda r: True)
        d2 = p2.decide(req(risk="low", approval="on-request"))
        assert d2.allowed is True

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            ApprovalPolicy(mode="bogus", sandbox_mode="workspace-write")

    def test_report(self):
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        p.decide(req(risk="medium", min_sandbox="workspace-write"))
        p.decide(req(risk="blocked"))
        assert "2 次" in p.report()


def term_req(command, risk="low", min_sandbox="read-only"):
    """终端命令审批请求（与 TerminalTool.build_approval_request 同构）。"""
    return ApprovalRequest(
        tool_name="terminal",
        arguments={"command": command},
        command=command,
        risk_level=risk,
        min_sandbox_mode=min_sandbox,
    )


class TestCommandWhitelist:
    """命令白名单模式（深度防御）：未命中白名单的终端命令需批准/拒绝。"""

    def test_whitelisted_command_passes_through(self):
        """命中白名单的只读命令：never 策略下按原路径放行。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True)
        d = p.decide(term_req("git status"))
        assert d.allowed is True

    def test_unlisted_command_denied_under_never(self):
        """never（无人值守）：未命中白名单直接拒绝。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True)
        d = p.decide(term_req("some-custom.exe --flag"))
        assert d.allowed is False
        assert "白名单" in d.reason

    def test_unlisted_command_denied_when_not_interactive(self):
        """on-failure 非交互：询问走保守拒绝。"""
        p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True)
        d = p.decide(term_req("run-my-thing"))
        assert d.allowed is False

    def test_unlisted_command_asks_approver(self):
        """on-failure + 自定义 approver：未命中白名单升级为询问。"""
        p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True,
                           approver=lambda r: True)
        assert p.decide(term_req("run-my-thing")).allowed is True

    def test_extra_patterns_extend_whitelist(self):
        """追加正则放行项目自有安全命令。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True,
                           command_whitelist_extra=[r"\bmytool\s+--safe\b"])
        assert p.decide(term_req("mytool --safe all")).allowed is True
        assert p.decide(term_req("mytool --dangerous")).allowed is False

    def test_whitelist_only_affects_terminal(self):
        """白名单只拦终端命令：其他工具不受影响。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True)
        d = p.decide(req(risk="medium", min_sandbox="workspace-write", tool="file"))
        assert d.allowed is True

    def test_blocked_command_still_denied_with_whitelist(self):
        """黑名单优先级最高：白名单模式下依然拒绝。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True,
                           command_whitelist_extra=[r"\bmkfs\b.*safe-looking\b"])
        d = p.decide(term_req("mkfs.ext4 /dev/sda1", risk="blocked"))
        assert d.allowed is False

    def test_disabled_by_default_unchanged(self):
        """默认关闭：行为与旧版完全一致（never 放行未列命令）。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        assert p.command_whitelist is False
        d = p.decide(term_req("some-custom.exe --flag"))
        assert d.allowed is True

    def test_invalid_extra_regex_does_not_crash(self):
        """用户配了非法正则：跳过该项，不炸审批门。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, command_whitelist=True,
                           command_whitelist_extra=[r"[unclosed"])
        assert p.decide(term_req("git status")).allowed is True
        assert p.decide(term_req("whatever")).allowed is False


class TestExecPolicyIntegration:
    """execpolicy DSL 与审批门集成（DSL 在黑名单/沙箱之后、白名单之前）。"""

    def test_dsl_allow_waives_high_risk_ask(self):
        """on-failure 高风险：DSL allow 直接放行，approver 不被调到。"""
        called = []

        def approver(r):
            called.append(r)
            return True

        p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                           interactive=False, approver=approver,
                           exec_policy=ExecPolicy([
                               {"match": {"tool": "terminal", "command_prefix": "rm "},
                                "decision": "allow"},
                           ]))
        d = p.decide(term_req("rm -rf build/", risk="high", min_sandbox="workspace-write"))
        assert d.allowed is True
        assert "execpolicy" in d.reason
        assert called == []   # 没有走询问环节

    def test_dsl_deny_rejected_even_under_never(self):
        """never（无人值守）：DSL deny 仍拒绝。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False,
                           exec_policy=ExecPolicy([
                               {"match": {"command_prefix": "npm "}, "decision": "deny"},
                           ]))
        d = p.decide(term_req("npm install lodash"))
        assert d.allowed is False
        assert "execpolicy" in d.reason

    def test_blocked_command_denied_even_with_dsl_allow(self):
        """黑名单优先：黑名单命令即使 DSL allow 也拒绝（DSL 不能豁免黑名单）。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False,
                           exec_policy=ExecPolicy([{"match": {}, "decision": "allow"}]))
        d = p.decide(term_req("mkfs.ext4 /dev/sda1", risk="blocked"))
        assert d.allowed is False

    def test_sandbox_insufficient_denied_even_with_dsl_allow(self):
        """沙箱等级不足：即使 DSL allow 也拒绝（DSL 不能豁免沙箱等级）。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="read-only",
                           interactive=False,
                           exec_policy=ExecPolicy([{"match": {}, "decision": "allow"}]))
        d = p.decide(term_req("dir", min_sandbox="workspace-write"))
        assert d.allowed is False
        assert "沙箱" in d.reason

    def test_dsl_ask_goes_to_approver(self):
        """DSL ask：走 _ask（由 approver 决定）。"""
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                           interactive=False, approver=lambda r: True,
                           exec_policy=ExecPolicy([{"match": {}, "decision": "ask"}]))
        assert p.decide(term_req("some-custom.exe --flag")).allowed is True
        p2 = ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                            interactive=False, approver=lambda r: False,
                            exec_policy=ExecPolicy([{"match": {}, "decision": "ask"}]))
        assert p2.decide(term_req("some-custom.exe --flag")).allowed is False

    def test_disabled_by_default_unchanged(self, monkeypatch):
        """enabled=false 时 exec_policy 为 None，行为与现在完全一致。"""
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", False)
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_file", "./execpolicy.json")
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        assert p.exec_policy is None
        d = p.decide(term_req("some-custom.exe --flag"))
        assert d.allowed is True

    def test_enabled_missing_file_still_none(self, monkeypatch):
        """enabled=true 但文件不存在：不创建 ExecPolicy，保持旧路径。"""
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", True)
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_file", "./definitely-not-here.json")
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        assert p.exec_policy is None

    def test_enabled_with_existing_file_loads_rules(self, monkeypatch, tmp_path):
        """enabled=true 且文件存在：自动加载规则并生效。"""
        import json
        f = tmp_path / "policy.json"
        f.write_text(json.dumps([
            {"match": {"command_prefix": "npm "}, "decision": "deny"}
        ]), encoding="utf-8")
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", True)
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_file", str(f))
        p = ApprovalPolicy(mode="never", sandbox_mode="workspace-write", interactive=False)
        assert p.exec_policy is not None
        assert p.decide(term_req("npm install lodash")).allowed is False
        assert p.decide(term_req("git status")).allowed is True
