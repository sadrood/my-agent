"""QQ 机器人桥：白名单、限流、独立会话、审批回 y/a/n、权限档不可提权、密钥不落日志。"""
import threading
import time

import pytest

from agent.qqbot import (ALLOWED_PRESETS, APPROVE_TIMEOUT, CHUNK_CHARS, QQBotBridge,
                         QQBotConfig, UserSession, _chunk)


def _cfg(**kw):
    base = {"app_id": "1903204272", "app_secret": "SECRET-XYZ", "allow_from": "me,friend"}
    base.update(kw)
    return QQBotConfig(base)


@pytest.fixture
def bridge():
    b = QQBotBridge(_cfg(), runner=lambda goal: f"已执行: {goal}")
    b.approve_timeout = 0.05        # 审批超时不能在测试里真的等 180 秒
    return b


class TestSecretsAndConfig:
    def test_repr_never_leaks_secret(self):
        cfg = _cfg()
        text = repr(cfg)
        assert "SECRET-XYZ" not in text and "1903204272" not in text
        assert "secret=***" in text

    def test_full_preset_is_downgraded(self):
        """聊天通道不允许提权：full 一律回落 ask。"""
        assert QQBotConfig({"permission": "full"}).preset == "ask"
        assert QQBotConfig({"permission": "block"}).preset == "block"
        assert "full" not in ALLOWED_PRESETS

    def test_missing_credentials_is_not_ready(self):
        assert QQBotConfig({}).ready() is False
        assert _cfg().ready() is True

    def test_allow_from_accepts_list_or_csv(self):
        assert QQBotConfig({"allow_from": "a, b ,c"}).allow_from == ["a", "b", "c"]
        assert QQBotConfig({"allow_from": ["x", " y "]}).allow_from == ["x", "y"]


class TestGating:
    def test_unknown_sender_is_rejected_with_his_openid(self, bridge):
        reply = bridge.handle_text("stranger", "帮我干活")
        assert "白名单" in reply and "stranger" in reply, "要把 openid 回显出来方便加白名单"

    def test_empty_openid_is_ignored(self, bridge):
        assert bridge.handle_text("", "hi") == ""

    def test_rate_limit_blocks_after_quota(self):
        b = QQBotBridge(_cfg(rate_per_minute=2), runner=lambda goal: "ok")
        assert b.handle_text("me", "1") == "ok"
        assert b.handle_text("me", "2") == "ok"
        assert "频繁" in b.handle_text("me", "3")

    def test_plan_is_pure(self, bridge):
        before = dict(bridge._recent)
        bridge.plan("me", "任务")
        assert bridge._recent == before, "plan 不能消耗配额"

    def test_group_messages_are_ignored_by_default(self):
        assert _cfg().allow_group is False
        assert _cfg(allow_group="true").allow_group is True
        assert _cfg(allow_group=True).allow_group is True


class TestSession:
    def test_each_user_gets_an_isolated_session(self, bridge):
        bridge.handle_text("me", "一")
        bridge.handle_text("friend", "二")
        a, b = bridge.sessions["c:me"], bridge.sessions["c:friend"]
        assert a.session_name() != b.session_name()
        assert a.session_name().startswith("qq-")

    def test_session_name_is_stable_and_hides_openid(self):
        s1 = UserSession("openid-abcdef123456").session_name()
        s2 = UserSession("openid-abcdef123456").session_name()
        assert s1 == s2 and "openid" not in s1

    def test_slash_commands(self, bridge):
        assert "可用" in bridge.handle_text("me", "/help")
        assert "已开新会话" in bridge.handle_text("me", "/new")
        assert "未知指令" in bridge.handle_text("me", "/nope")
        bridge.handle_text("me", "干活")
        status = bridge.handle_text("me", "/status")
        assert "qq-" in status and "轮" in status and "档位 ask" in status

    def test_injected_runner_never_builds_a_real_agent(self, bridge):
        """测试必须注入 runner：否则会真的去调 LLM（实测跑出 9 分钟）。"""
        bridge.handle_text("me", "干活")
        assert bridge.sessions["c:me"].agent is None

    def test_task_text_is_capped(self, bridge):
        seen = {}
        b = QQBotBridge(_cfg(max_chars=10), runner=lambda goal: seen.setdefault("goal", goal) or "ok")
        b.handle_text("me", "x" * 50)
        assert len(seen["goal"]) == 10


class TestApproval:
    def _request(self, tool="terminal", command="rm -rf x", reason="命令涉及安全关键文件：config.py"):
        return type("Req", (), {"tool_name": tool, "command": command, "reason": reason})()

    def test_approval_sends_question_and_waits_for_yes(self, bridge):
        sent = []
        bridge._api = type("Api", (), {})()
        bridge._send_sync = lambda key, text: sent.append((key, text))

        def answer():
            for _ in range(50):
                if bridge._pending.get("c:me"):
                    bridge.handle_text("me", "y")
                    return
                time.sleep(0.02)

        threading.Thread(target=answer).start()
        assert bridge.begin_approval("me", self._request()) is True
        assert sent and "需要批准" in sent[0][1] and "config.py" in sent[0][1]
        assert sent[0][0] == "c:me", "审批问题要发回提问的那个会话"

    def test_deny_and_timeout_default_to_no(self, bridge):
        bridge._api = type("Api", (), {})()
        bridge._send_sync = lambda key, text: None
        bridge._pending["c:me"] = {"event": threading.Event(), "allow": True, "tool": "x"}
        bridge._pending["c:me"]["event"].set()
        assert bridge.begin_approval("me", self._request()) is False   # 已被清空 → 超时视为拒绝
        assert bridge.begin_approval("me", self._request()) is False

    def test_always_answer_skips_next_time(self, bridge):
        """回 a：本次放行，并记住这个工具，之后不再打扰。"""
        sent = []
        bridge._api = type("Api", (), {})()
        bridge._send_sync = lambda key, text: sent.append(text)
        state = {"event": threading.Event(), "allow": False, "always": False, "tool": "terminal"}
        bridge._pending["c:friend"] = state
        bridge.handle_text("friend", "a")
        state["event"].set()
        assert state["allow"] is True and state["always"] is True
        assert bridge.begin_approval("friend", self._request("terminal")) is True
        assert "terminal" in bridge._always.get("c:friend", set())
        assert len(sent) == 0, "记住之后再问一次是多余的（不该再发审批消息）"

    def test_other_tools_still_ask_after_always(self, bridge):
        bridge._api = type("Api", (), {})()
        bridge._send_sync = lambda key, text: None
        bridge._always["c:friend"] = {"terminal"}
        assert bridge.begin_approval("friend", self._request("terminal")) is True
        assert bridge.begin_approval("friend", self._request("python")) is False, "别的工具仍要问"

    def test_without_api_it_denies(self, bridge):
        """没法发消息问用户时必须保守拒绝。"""
        assert bridge.begin_approval("me", self._request()) is False

    def test_approval_reply_is_consumed_not_treated_as_task(self, bridge):
        bridge._pending["c:me"] = {"event": threading.Event(), "allow": False, "tool": "t"}
        reply = bridge.handle_text("me", "n")
        assert "已按你的答复" in reply
        assert bridge.sessions == {}, "审批答复不能被当成新任务"

    def test_group_approval_is_keyed_per_member(self):
        """群里 A 的审批答复不能替 B 处理，也不能被别人消费掉。"""
        b = QQBotBridge(_cfg(allow_group="true", allow_groups="G1"),
                        runner=lambda goal: "ok")
        b.approve_timeout = 0.05
        b._pending["g:G1:me"] = {"event": threading.Event(), "allow": False, "tool": "t"}
        reply = b.handle_text("friend", "y", "G1")
        assert "已按你的答复" not in reply, "别人的答复不能替 A 处理审批"
        assert b._pending["g:G1:me"]["event"].is_set() is False, "A 的审批还挂着"
        assert "已按你的答复" in b.handle_text("me", "y", "G1")

    def test_approver_factory_binds_the_user(self, bridge):
        seen = []
        bridge.begin_approval = lambda openid, req: seen.append(openid) or True
        assert bridge.approver_for("friend")(self._request()) is True
        assert seen == ["friend"]


class TestFormatting:
    def test_chunks_respect_limit(self):
        pieces = _chunk("a" * (CHUNK_CHARS * 2 + 5))
        assert all(len(p) <= CHUNK_CHARS for p in pieces)
        assert len(pieces) == 3

    def test_empty_output_still_replies(self):
        assert _chunk("") == ["（没有输出）"]

    def test_long_single_line_is_hard_split(self):
        pieces = _chunk("z" * (CHUNK_CHARS + 10))
        assert len(pieces) == 2 and all(len(p) <= CHUNK_CHARS for p in pieces)


class TestGroupChat:
    """群聊默认关；开了也要群白名单 + 发言人白名单双过，且每人独立上下文。"""

    def _group_bridge(self, **kw):
        cfg = _cfg(allow_group="true", allow_groups="G1", **kw)
        b = QQBotBridge(cfg, runner=lambda goal: f"已执行: {goal}")
        b.approve_timeout = 0.05
        return b

    def test_group_off_by_default(self, bridge):
        assert bridge.config.allow_group is False
        reply = bridge.handle_text("me", "@我 干活", "G1")
        assert "群聊默认关闭" in reply
        assert bridge.sessions == {}

    def test_group_must_be_whitelisted(self):
        b = self._group_bridge()
        assert "不在群白名单" in b.handle_text("me", "干活", "G9")
        assert b.handle_text("me", "干活", "G1") == "已执行: 干活"

    def test_speaker_must_be_whitelisted_too(self):
        b = self._group_bridge()
        reply = b.handle_text("stranger", "干活", "G1")
        assert "调用白名单里没有你" in reply, "要说清是发言人没过白名单，不是群的问题"
        assert "stranger" in reply and b.sessions == {}

    def test_group_not_whitelisted_says_so_with_the_group_id(self):
        b = self._group_bridge()
        reply = b.handle_text("me", "干活", "G9")
        assert "群白名单" in reply and "G9" in reply

    def test_each_member_gets_its_own_session(self):
        b = self._group_bridge()
        b.handle_text("me", "一", "G1")
        b.handle_text("friend", "二", "G1")
        assert set(b.sessions) == {"g:G1:me", "g:G1:friend"}
        assert b.sessions["g:G1:me"].session_name() != b.sessions["g:G1:friend"].session_name()

    def test_same_user_in_group_and_private_are_different_scopes(self):
        b = self._group_bridge()
        b.handle_text("me", "私聊任务")
        b.handle_text("me", "群任务", "G1")
        assert set(b.sessions) == {"c:me", "g:G1:me"}

    def test_scope_key_roundtrip(self):
        key = QQBotBridge.scope_key("user-1", "group-9")
        assert QQBotBridge.split_key(key) == ("group-9", "user-1")
        assert QQBotBridge.split_key(QQBotBridge.scope_key("user-1")) == ("", "user-1")

    def test_group_rate_limit_is_per_member(self):
        b = self._group_bridge(rate_per_minute=1)
        assert b.handle_text("me", "1", "G1") == "已执行: 1"
        assert "频繁" in b.handle_text("me", "2", "G1")
        assert b.handle_text("friend", "3", "G1") == "已执行: 3", "别人不该被连坐"

    def test_group_handler_is_registered_on_the_right_event(self):
        """QQ 群的 @ 事件是 on_group_at_message_create；on_at_message_create 是频道事件。"""
        src = open("agent/qqbot.py", encoding="utf-8").read()
        assert "on_group_at_message_create" in src
        assert "handle_group_message" in src


class TestEntryPoint:
    def test_main_reports_missing_credentials(self, monkeypatch, capsys):
        from agent import qqbot
        monkeypatch.setattr(qqbot, "QQBotConfig", lambda: QQBotConfig({}))
        assert qqbot.main() == 2
        assert "QQBOT_APP_ID" in capsys.readouterr().out

    def test_lazy_import_keeps_other_features_working(self):
        """没装 qq-botpy 时也不该在导入期炸掉（只有真正建客户端才需要它）。"""
        import importlib.util
        assert importlib.util.find_spec("agent.qqbot") is not None
        src = open("agent/qqbot.py", encoding="utf-8").read()
        assert "\nimport botpy" not in src, "botpy 必须在函数内惰性导入"
