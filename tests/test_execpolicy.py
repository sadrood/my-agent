"""
execpolicy DSL（结构化命令策略）单元测试。
"""
import json
import logging

from agent.execpolicy import ExecPolicy


class TestExecPolicyDecide:
    def test_no_match_returns_none(self):
        p = ExecPolicy([{"match": {"tool": "terminal"}, "decision": "deny"}])
        assert p.decide("file", "anything") is None
        assert p.decide("terminal", "anything") == "deny"

    def test_deny_priority_over_allow(self):
        """deny 优先：即使 allow 规则在前且先匹配，也返回 deny。"""
        rules = [
            {"match": {"tool": "terminal", "command_prefix": "npm "}, "decision": "allow"},
            {"match": {"tool": "terminal", "command_prefix": "npm "}, "decision": "deny"},
        ]
        p = ExecPolicy(rules)
        assert p.decide("terminal", "npm install") == "deny"

    def test_deny_priority_scans_all_rules(self):
        """deny 在任意位置命中都生效（不按规则顺序）。"""
        rules = [
            {"match": {"command_prefix": "git "}, "decision": "allow"},
            {"match": {"command_prefix": "git "}, "decision": "deny"},
        ]
        assert ExecPolicy(rules).decide("terminal", "git push") == "deny"

    def test_first_match_allow(self):
        """无 deny 时按规则顺序 first-match 取 allow/ask。"""
        rules = [
            {"match": {"command_prefix": "git "}, "decision": "allow"},
            {"match": {"command_prefix": "git "}, "decision": "ask"},
        ]
        p = ExecPolicy(rules)
        assert p.decide("terminal", "git status") == "allow"

    def test_command_prefix(self):
        p = ExecPolicy([
            {"match": {"command_prefix": "npm "}, "decision": "allow"},
        ])
        assert p.decide("terminal", "npm install lodash") == "allow"
        assert p.decide("terminal", "pip install lodash") is None

    def test_command_prefix_case_sensitive(self):
        """前缀匹配原样比较（大小写敏感），与要求一致。"""
        p = ExecPolicy([
            {"match": {"command_prefix": "npm "}, "decision": "allow"},
        ])
        assert p.decide("terminal", "NPM install") is None

    def test_pattern_regex_ignorecase(self):
        """pattern 用 re.search + IGNORECASE。"""
        p = ExecPolicy([
            {"match": {"pattern": r"\binstall\b"}, "decision": "ask"},
        ])
        assert p.decide("terminal", "npm INSTALL lodash") == "ask"
        assert p.decide("terminal", "npm installing lodash") is None

    def test_tool_filter(self):
        """tool 精确相等：只影响指定工具。"""
        p = ExecPolicy([
            {"match": {"tool": "terminal", "command_prefix": "rm "}, "decision": "deny"},
        ])
        assert p.decide("terminal", "rm -rf build/") == "deny"
        assert p.decide("file", "rm -rf build/") is None

    def test_empty_match_matches_anything(self):
        """match 全空 = 任意工具任意命令。"""
        p = ExecPolicy([{"match": {}, "decision": "deny"}])
        assert p.decide("terminal", "whatever") == "deny"
        assert p.decide("file", "whatever") == "deny"

    def test_ask_verdict(self):
        p = ExecPolicy([{"match": {}, "decision": "ask"}])
        assert p.decide("terminal", "npm install") == "ask"

    def test_invalid_regex_rule_skipped(self):
        """非法正则会跳过该条规则，不炸判定。"""
        p = ExecPolicy([
            {"match": {"pattern": "[unclosed"}, "decision": "deny"},
            {"match": {}, "decision": "allow"},
        ])
        assert p.decide("terminal", "anything") == "allow"

    def test_unknown_decision_ignored(self):
        """decision 不在 allow/deny/ask 中的规则不产生判定。"""
        p = ExecPolicy([
            {"match": {}, "decision": "maybe"},
            {"match": {}, "decision": "allow"},
        ])
        assert p.decide("terminal", "anything") == "allow"


class TestExecPolicyLoad:
    def test_load_valid_rules(self, tmp_path):
        f = tmp_path / "policy.json"
        f.write_text(json.dumps([
            {"match": {"tool": "terminal", "command_prefix": "npm "}, "decision": "allow"}
        ]), encoding="utf-8")
        p = ExecPolicy.load(str(f))
        assert p.decide("terminal", "npm install") == "allow"

    def test_load_missing_file_fail_open(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="agent.execpolicy"):
            p = ExecPolicy.load(str(tmp_path / "nope.json"))
        assert p.rules == []
        assert p.decide("terminal", "anything") is None
        assert any("execpolicy" in r.message for r in caplog.records)

    def test_load_bad_json_fail_open(self, tmp_path, caplog):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agent.execpolicy"):
            p = ExecPolicy.load(str(f))
        assert p.rules == []
        assert p.decide("terminal", "anything") is None
        assert any("execpolicy" in r.message for r in caplog.records)

    def test_load_non_array_fail_open(self, tmp_path, caplog):
        f = tmp_path / "obj.json"
        f.write_text('{"match": {}}', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agent.execpolicy"):
            p = ExecPolicy.load(str(f))
        assert p.rules == []

    def test_load_empty_array(self, tmp_path):
        """空数组 = 合法规则集：无规则可命中。"""
        f = tmp_path / "empty.json"
        f.write_text("[]", encoding="utf-8")
        p = ExecPolicy.load(str(f))
        assert p.rules == []
        assert p.decide("terminal", "anything") is None

    def test_load_zero_byte_file_fail_open(self, tmp_path):
        """0 字节文件（JSON 损坏）→ fail-open 空规则。"""
        f = tmp_path / "zero.json"
        f.write_text("", encoding="utf-8")
        p = ExecPolicy.load(str(f))
        assert p.rules == []
