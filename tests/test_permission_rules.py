"""agent/permission_rules.py 的离线单测：规则解析、匹配语义、优先级、兜底。"""

import json
import os

import pytest

from agent.permission_rules import (
    Decision,
    PermissionRules,
    Rule,
    parse_rule,
    parse_rules,
    subject_of,
)


# ---------------------------------------------------------------- 解析

def test_parse_rule_with_pattern():
    r = parse_rule("Bash(git *)", "allow")
    assert r.tool == "Bash"
    assert r.pattern == "git *"
    assert r.effect == "allow"
    assert r.scope == "args"


def test_parse_rule_bare_tool_matches_whole_tool():
    r = parse_rule("Read", "allow")
    assert r.tool == "Read"
    assert r.pattern is None
    assert r.scope == "tool"


def test_parse_rule_empty_parens_is_tool_scope():
    assert parse_rule("Read()", "allow").pattern is None


def test_parse_rule_strips_whitespace():
    r = parse_rule("  Edit( src/** )  ", "deny")
    assert (r.tool, r.pattern) == ("Edit", "src/**")


@pytest.mark.parametrize("bad", ["", "   ", "(cmd)", "!!x", None, 42])
def test_parse_rule_rejects_junk(bad):
    assert parse_rule(bad, "allow") is None


def test_parse_rule_rejects_unknown_effect():
    with pytest.raises(ValueError):
        parse_rule("Read", "maybe")


def test_parse_rules_skips_junk_keeps_rest():
    rules = parse_rules(["Read", "", "!!!", "Bash(git *)"], "allow")
    assert [r.tool for r in rules] == ["Read", "Bash"]


def test_parse_rules_handles_none():
    assert parse_rules(None, "allow") == []


# ---------------------------------------------------------------- 主语提取

def test_subject_of_terminal_prefers_command():
    assert subject_of("terminal", {"command": "git status", "cwd": "x"}) == "git status"


def test_subject_of_file_prefers_path():
    assert subject_of("file", {"path": "src/a.py", "content": "zzz"}) == "src/a.py"


def test_subject_of_falls_back_to_first_string():
    assert subject_of("weirdtool", {"whatever": "hello", "n": 1}) == "hello"


def test_subject_of_empty_and_non_dict():
    assert subject_of("terminal", None) == ""
    assert subject_of("terminal", {}) == ""
    assert subject_of("terminal", "raw cmd") == "raw cmd"


# ---------------------------------------------------------------- 匹配语义

def test_command_prefix_pattern():
    rules = PermissionRules(allow=["Bash(git *)"])
    assert rules.decide("Bash", {"command": "git status -s"}).effect == "allow"
    assert rules.decide("Bash", {"command": "git"}).effect == "allow"
    assert rules.decide("Bash", {"command": "gitk --all"}).effect == "ask"


def test_command_exact_pattern():
    rules = PermissionRules(allow=["terminal(npm test)"])
    assert rules.decide("terminal", {"command": "npm test"}).effect == "allow"
    assert rules.decide("terminal", {"command": "npm test extra"}).effect == "ask"


def test_path_doublestar_crosses_directories():
    rules = PermissionRules(allow=["Edit(src/**)"])
    assert rules.decide("Edit", {"file_path": "src/a.py"}).effect == "allow"
    assert rules.decide("Edit", {"file_path": "src/a/b/c.py"}).effect == "allow"
    assert rules.decide("Edit", {"file_path": "tests/a.py"}).effect == "ask"


def test_path_single_star_stays_in_one_level():
    rules = PermissionRules(allow=["Edit(src/*)"])
    assert rules.decide("Edit", {"file_path": "src/a.py"}).effect == "allow"
    assert rules.decide("Edit", {"file_path": "src/a/b.py"}).effect == "ask"


def test_path_match_is_separator_agnostic():
    rules = PermissionRules(allow=["Edit(src/**)"])
    assert rules.decide("Edit", {"file_path": "src\\a\\b.py"}).effect == "allow"


def test_tool_scope_rule_matches_any_args():
    rules = PermissionRules(deny=["WebFetch"])
    assert rules.decide("WebFetch", {"url": "http://x"}).effect == "deny"
    assert rules.decide("WebFetch", {}).effect == "deny"


def test_tool_name_match_is_case_insensitive():
    rules = PermissionRules(allow=["bash(git *)"])
    assert rules.decide("Bash", {"command": "git log"}).effect == "allow"


# ---------------------------------------------------------------- 优先级

def test_deny_beats_allow():
    rules = PermissionRules(allow=["Bash(git *)"], deny=["Bash(git push*)"])
    d = rules.decide("Bash", {"command": "git push origin main"})
    assert d.effect == "deny"
    assert d.rule.raw == "Bash(git push*)"


def test_ask_beats_allow():
    rules = PermissionRules(allow=["terminal(*)"], ask=["terminal(rm *)"])
    assert rules.decide("terminal", {"command": "rm -rf build"}).effect == "ask"
    assert rules.decide("terminal", {"command": "ls -la"}).effect == "allow"


def test_deny_beats_ask():
    rules = PermissionRules(ask=["terminal(*)"], deny=["terminal(shutdown*)"])
    assert rules.decide("terminal", {"command": "shutdown /s"}).effect == "deny"


def test_same_effect_first_rule_wins():
    rules = PermissionRules(allow=["Bash(git *)", "Bash(git status)"])
    d = rules.decide("Bash", {"command": "git status"})
    assert d.rule.raw == "Bash(git *)"


# ---------------------------------------------------------------- 兜底与零回归

def test_no_match_falls_back_to_default():
    rules = PermissionRules(allow=["Bash(git *)"])
    d = rules.decide("terminal", {"command": "ls"})
    assert d.effect == "ask"
    assert isinstance(d, Decision)
    assert d.rule is None


def test_custom_default_respected():
    rules = PermissionRules(allow=["Read"], default="allow")
    assert rules.decide("Bash", {"command": "ls"}).effect == "allow"


def test_invalid_default_falls_back_to_ask():
    assert PermissionRules(default="nonsense").default == "ask"


def test_empty_ruleset_is_empty_and_neutral():
    rules = PermissionRules()
    assert rules.empty and len(rules) == 0
    assert rules.decide("terminal", {"command": "rm -rf /"}).effect == "ask"


# ---------------------------------------------------------------- 载入

def test_from_dict_roundtrip():
    rules = PermissionRules.from_dict(
        {"allow": ["Bash(git *)"], "deny": ["Bash(git push*)"], "default": "allow"}
    )
    assert len(rules) == 2
    assert rules.decide("Bash", {"command": "git diff"}).effect == "allow"
    assert rules.decide("Bash", {"command": "git push"}).effect == "deny"


def test_from_dict_handles_none():
    assert PermissionRules.from_dict(None).empty


def test_from_file_missing_is_empty(tmp_path):
    assert PermissionRules.from_file(tmp_path / "nope.json").empty


def test_from_file_broken_json_is_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert PermissionRules.from_file(p).empty


def test_from_file_reads_rules(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"allow": ["Edit(src/**)"]}), encoding="utf-8")
    rules = PermissionRules.from_file(p)
    assert rules.decide("Edit", {"file_path": "src/x.py"}).effect == "allow"


def test_from_env_without_var_is_empty(monkeypatch):
    monkeypatch.delenv("MY_AGENT_PERMISSION_RULES", raising=False)
    assert PermissionRules.from_env().empty


def test_from_env_reads_path(tmp_path, monkeypatch):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"allow": ["Read"]}), encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_PERMISSION_RULES", str(p))
    assert PermissionRules.from_env().decide("Read", {"path": "a"}).effect == "allow"


def test_rule_dataclass_is_hashable():
    assert Rule(tool="Read", effect="allow") in {Rule(tool="Read", effect="allow")}


# ---------------------------------------------------------------- 接入 ApprovalPolicy

def _req(tool="terminal", command="git status", risk="medium",
         sandbox="workspace-write", approval="auto"):
    return ApprovalRequest(
        tool_name=tool,
        arguments={"command": command},
        command=command,
        risk_level=risk,
        min_sandbox_mode=sandbox,
        approval=approval,
    )


def _policy(rules=None, **kw):
    kw.setdefault("mode", "on-failure")
    kw.setdefault("sandbox_mode", "workspace-write")
    kw.setdefault("interactive", False)
    kw.setdefault("permission_rules", rules if rules is not None else PermissionRules())
    return ApprovalPolicy(**kw)


def test_policy_without_rules_behaves_as_before():
    assert _policy().decide(_req()).allowed is True


def test_policy_allow_rule_short_circuits():
    p = _policy(PermissionRules(allow=["terminal(git *)"]))
    d = p.decide(_req(command="git status -s"))
    assert d.allowed is True
    assert "权限规则" in d.reason


def test_policy_deny_rule_blocks():
    p = _policy(PermissionRules(deny=["terminal(git push*)"]))
    d = p.decide(_req(command="git push origin main"))
    assert d.allowed is False
    assert "deny" in d.reason


def test_policy_ask_rule_asks_and_noninteractive_denies():
    p = _policy(PermissionRules(ask=["terminal(*)"], default="allow"))
    d = p.decide(_req(command="ls -la"))
    assert d.allowed is False
    assert "ask" in d.reason


def test_policy_ask_rule_uses_approver():
    seen = []
    p = _policy(PermissionRules(ask=["terminal(*)"]),
                approver=lambda r: (seen.append(r), True)[1])
    d = p.decide(_req(command="ls"))
    assert d.allowed is True
    assert seen


def test_policy_rule_only_matches_its_tool():
    p = _policy(PermissionRules(allow=["terminal(git *)"]))
    d = p.decide(_req(tool="file", command="read something"))
    assert "权限规则" not in (d.reason or "")
    assert d.allowed is True


def test_blacklist_still_beats_allow_rule():
    p = _policy(PermissionRules(allow=["terminal(*)"]))
    d = p.decide(_req(command="rm -rf /", risk="blocked"))
    assert d.allowed is False
    assert "黑名单" in d.reason


def test_policy_reads_rules_from_arguments_when_present():
    p = _policy(PermissionRules(allow=["edit(src/**)"]))
    d = p.decide(ApprovalRequest(
        tool_name="edit",
        arguments={"file_path": "src/a.py"},
        command="edit src/a.py",
        risk_level="medium",
        min_sandbox_mode="workspace-write",
    ))
    assert d.allowed is True
    assert "权限规则" in d.reason


def test_policy_permission_rules_from_env(monkeypatch, tmp_path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text('{"allow": ["terminal(git *)"]}', encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_PERMISSION_RULES", str(rules_file))
    p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write", interactive=False)
    assert p.decide(_req(command="git diff")).allowed is True


# 模块级导入放文件末尾同样生效（后续用例运行时可见）
from agent.approval import ApprovalPolicy  # noqa: E402
from tools.base import ApprovalRequest  # noqa: E402


class TestToolAliasAndCompoundCommands:
    """`Bash(...)` 别名 + 复合命令语义 —— 两条都会让规则**静默失效**。"""

    def test_bash_alias_equals_terminal(self):
        """模块 docstring 与 Claude Code 都写 `Bash(git *)`，本仓库工具却叫 terminal。"""
        assert PermissionRules(deny=["Bash(rm *)"]).decide(
            "terminal", {"command": "rm -rf x"}).effect == "deny"
        assert PermissionRules(allow=["Bash(git *)"]).decide(
            "terminal", {"command": "git status"}).effect == "allow"

    def test_alias_and_tool_name_are_case_insensitive(self):
        assert PermissionRules(deny=["BASH(rm *)"]).decide(
            "Terminal", {"command": "rm -rf x"}).effect == "deny"

    def test_allow_rule_requires_every_subcommand(self):
        """allow 不该把 `git status; rm -rf /` 整串放行（前缀语义的经典绕过）。"""
        r = PermissionRules(allow=["terminal(git *)"])
        assert r.decide("terminal", {"command": "git status; rm -rf /"}).rule is None
        assert r.decide("terminal", {"command": "git status && git diff"}).effect == "allow"

    def test_deny_rule_matches_any_subcommand(self):
        """deny 要认出复合命令里的危险段：`cd / && rm -rf /`。"""
        r = PermissionRules(deny=["terminal(rm *)"])
        assert r.decide("terminal", {"command": "cd / && rm -rf /"}).effect == "deny"
        assert r.decide("terminal", {"command": "ls | rm -rf x"}).effect == "deny"

    def test_plain_command_still_matches(self):
        """没有分隔符的普通命令不受切分影响。"""
        assert PermissionRules(allow=["terminal(git *)"]).decide(
            "terminal", {"command": "git status -s"}).effect == "allow"


class TestRulesFileLoading:
    """规则文件的来源与优先级（config 锚定路径 > 环境变量原文）。"""

    def test_from_env_prefers_config_path(self, monkeypatch, tmp_path):
        import config
        f = tmp_path / "r.json"
        f.write_text(json.dumps({"allow": ["terminal(git *)"]}), encoding="utf-8")
        monkeypatch.setitem(config.APPROVAL_CONFIG, "permission_rules_file", str(f))
        monkeypatch.delenv("MY_AGENT_PERMISSION_RULES", raising=False)
        assert PermissionRules.from_env().decide(
            "terminal", {"command": "git status"}).effect == "allow"

    def test_from_env_falls_back_to_env_var(self, monkeypatch, tmp_path):
        """config 没配时，显式给的环境变量仍要生效（否则就是把老用法弄坏了）。"""
        import config
        f = tmp_path / "r.json"
        f.write_text(json.dumps({"deny": ["terminal(rm *)"]}), encoding="utf-8")
        monkeypatch.setitem(config.APPROVAL_CONFIG, "permission_rules_file", "")
        monkeypatch.setenv("MY_AGENT_PERMISSION_RULES", str(f))
        assert PermissionRules.from_env().decide(
            "terminal", {"command": "rm -rf x"}).effect == "deny"

    def test_relative_config_path_is_anchored(self):
        """相对路径必须锚定项目根 —— 否则换个 cwd 启动，规则会静默变空集。"""
        import config
        p = os.path.normpath(config.resolve_under_root("./rules.json"))
        assert os.path.isabs(p)
        assert os.path.normcase(os.path.dirname(p)) == os.path.normcase(config.PROJECT_ROOT)


class TestExplicitlyEmptyRulesAreHonored:
    """显式传入空规则集不能被环境变量悄悄替换（`PermissionRules` 是 falsy）。"""

    def test_empty_rules_not_replaced_by_env(self, monkeypatch, tmp_path):
        f = tmp_path / "r.json"
        f.write_text(json.dumps({"deny": ["terminal(git *)"]}), encoding="utf-8")
        monkeypatch.setenv("MY_AGENT_PERMISSION_RULES", str(f))

        p = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                           interactive=False, permission_rules=PermissionRules())
        d = p.decide(_req(command="git status"))
        # 显式空集 = 无规则 = 走原策略放行；若被 env 规则替换，这里会变成 deny
        assert d.allowed is True and "权限规则" not in d.reason


class TestApprovalChainPrecedence:
    """优先级链：黑名单 > 沙箱 > execpolicy > 权限规则 > 白名单 > 策略（此前无测试）。"""

    def test_sandbox_insufficiency_beats_allow_rule(self):
        rules = PermissionRules(allow=["edit(src/**)"])
        req = ApprovalRequest(tool_name="edit", arguments={"file_path": "src/a.py"},
                              command="edit src/a.py", risk_level="low",
                              min_sandbox_mode="workspace-write")
        denied = ApprovalPolicy(mode="on-failure", sandbox_mode="read-only",
                                interactive=False, permission_rules=rules).decide(req)
        assert denied.allowed is False and "沙箱" in denied.reason
        # 对照组：沙箱够用时同一条 allow 规则就能放行（证明是沙箱先拦下的）
        ok = ApprovalPolicy(mode="on-failure", sandbox_mode="workspace-write",
                            interactive=False, permission_rules=rules).decide(req)
        assert ok.allowed is True and "权限规则" in ok.reason

    def test_permission_allow_beats_command_whitelist(self):
        """白名单模式下，权限规则的 allow 应先放行（链上它排在白名单之前）。"""
        p = _policy(PermissionRules(allow=["terminal(git *)"]), command_whitelist=True)
        d = p.decide(_req(command="git status"))
        assert d.allowed is True and "权限规则" in d.reason

    def test_permission_deny_wins_over_policy_allow(self):
        """never 策略下也拦得住：deny 规则排在策略之前。"""
        p = _policy(PermissionRules(deny=["terminal(rm *)"]), mode="never")
        assert p.decide(_req(command="rm -rf /tmp/x")).allowed is False
