"""QQ 机器人桥：白名单、限流、独立会话、审批回 y/a/n、权限档不可提权、密钥不落日志。"""
import asyncio
import threading
import time

import pytest

from agent.qqbot import (ALLOWED_PRESETS, APPROVE_TIMEOUT, CHUNK_CHARS,
                         MAX_REPLIES_PER_MESSAGE, PASSIVE_REPLY_WINDOW_C2C,
                         PASSIVE_REPLY_WINDOW_GROUP, QQBotBridge, QQBotConfig,
                         TASK_TIMEOUT, UserSession, _chunk, _chunks_capped)


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

    def test_sandbox_defaults_on(self):
        """新机器人默认只存在于沙箱；连正式环境会"连上了却收不到任何消息"。"""
        assert _cfg().sandbox is True
        assert QQBotConfig({"sandbox": "false"}).sandbox is False
        assert QQBotConfig({"sandbox": "0"}).sandbox is False
        assert QQBotConfig({"sandbox": "true"}).sandbox is True
        assert QQBotConfig({"sandbox": ""}).sandbox is True, "空值按安全默认（沙箱）"

    def test_repr_shows_which_environment(self):
        assert "沙箱" in repr(_cfg())
        assert "正式" in repr(_cfg(sandbox="false"))

    def test_client_follows_the_sandbox_flag(self):
        """沙箱标志必须真的传进 botpy，否则连错环境一条消息都收不到。"""
        pytest.importorskip("botpy")
        from agent.qqbot import _build_client
        assert _build_client(QQBotBridge(_cfg())).http.is_sandbox is True
        assert _build_client(QQBotBridge(_cfg(sandbox="false"))).http.is_sandbox is False


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


class _FakeApi:
    """假发送端：记录每次调用，可配置成必失败。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def post_c2c_message(self, **kw):
        self.calls.append(("c2c", kw))
        if self.fail:
            raise RuntimeError("boom")

    async def post_group_message(self, **kw):
        self.calls.append(("group", kw))
        if self.fail:
            raise RuntimeError("boom")


class TestDelivery:
    """发送层：msg_seq 递增、被动窗口降级、失败不炸、上下文按会话隔离。"""

    def _bridge_with_api(self, api, key="c:me", msg_id="m1", age=0.0):
        b = QQBotBridge(_cfg(), runner=lambda goal: "ok")
        b._ctx[QQBotBridge.norm_key(key)] = {
            "api": api, "msg_id": msg_id, "loop": None, "recv_at": time.time() - age}
        return b

    def test_chunks_increment_msg_seq(self):
        """官方：相同 msg_id + msg_seq 重复发送会失败 —— 分块必须递增序号。"""
        api = _FakeApi()
        b = self._bridge_with_api(api)
        asyncio.run(b._send("c:me", "a" * (CHUNK_CHARS * 2 + 5)))
        assert [c[1]["msg_seq"] for c in api.calls] == [1, 2, 3]
        assert all(c[1]["msg_id"] == "m1" for c in api.calls)

    def test_group_chunks_increment_msg_seq(self):
        api = _FakeApi()
        b = self._bridge_with_api(api, key="g:G1:me")
        asyncio.run(b._send("g:G1:me", "b" * (CHUNK_CHARS + 10)))
        assert api.calls[0][0] == "group"
        assert [c[1]["msg_seq"] for c in api.calls] == [1, 2]

    def test_reply_within_window_stays_passive(self):
        api = _FakeApi()
        b = self._bridge_with_api(api, age=10)
        asyncio.run(b._send("c:me", "hi"))
        assert api.calls[0][1]["msg_id"] == "m1", "窗口内应带 msg_id 被动回复"

    def test_reply_after_window_becomes_active(self):
        """超窗口还带原 msg_id 必被平台拒；此时只能改主动消息。"""
        api = _FakeApi()
        b = self._bridge_with_api(api, age=PASSIVE_REPLY_WINDOW_C2C + 60)
        asyncio.run(b._send("c:me", "hi"))
        assert api.calls[0][1]["msg_id"] is None, "超窗口要改主动消息"

    def test_window_differs_by_scene(self):
        """官方：私聊 60 分钟、群聊 5 分钟 —— 同样 10 分钟前，判断应当不同。"""
        api_g = _FakeApi()
        asyncio.run(self._bridge_with_api(api_g, key="g:G1:me", age=600)
                    ._send("g:G1:me", "hi"))
        assert api_g.calls[0][1]["msg_id"] is None, "群聊超 5 分钟要改主动消息"

        api_c = _FakeApi()
        asyncio.run(self._bridge_with_api(api_c, key="c:me", age=600)
                    ._send("c:me", "hi"))
        assert api_c.calls[0][1]["msg_id"] == "m1", "私聊 60 分钟内仍能被动回复"

    def test_c2c_stays_passive_well_past_the_group_window(self):
        api = _FakeApi()
        b = self._bridge_with_api(api, key="c:me", age=PASSIVE_REPLY_WINDOW_C2C - 60)
        asyncio.run(b._send("c:me", "hi"))
        assert api.calls[0][1]["msg_id"] == "m1"

    def test_replies_per_message_are_capped(self):
        """平台限制同一条消息最多回复 4~5 次，超了整条失败 —— 要截断而不是硬发。"""
        api = _FakeApi()
        b = self._bridge_with_api(api)
        asyncio.run(b._send("c:me", "a" * (CHUNK_CHARS * 10)))
        assert len(api.calls) == MAX_REPLIES_PER_MESSAGE
        assert [c[1]["msg_seq"] for c in api.calls] == list(
            range(1, MAX_REPLIES_PER_MESSAGE + 1))
        assert "已省略" in api.calls[-1][1]["content"], "截断要说清，别让用户以为内容就这么多"

    def test_capped_chunks_leave_short_replies_alone(self):
        assert _chunks_capped("短回复") == ["短回复"]
        assert len(_chunks_capped("a" * (CHUNK_CHARS * 10))) == MAX_REPLIES_PER_MESSAGE

    def test_send_failure_is_logged_not_raised(self):
        api = _FakeApi(fail=True)
        b = self._bridge_with_api(api)
        asyncio.run(b._send("c:me", "hi"))           # 不该抛出去
        assert len(api.calls) == 1

    def test_send_sync_without_loop_is_safe(self):
        api = _FakeApi()
        b = self._bridge_with_api(api)
        b._send_sync("c:me", "hi")                   # 无事件循环：记日志丢弃，不炸
        assert api.calls == []

    def test_context_is_isolated_per_scope(self):
        """群聊与私聊并发时 api/msg_id 不能互相覆盖，否则审批会发错窗口。"""
        api_a, api_b = _FakeApi(), _FakeApi()
        b = QQBotBridge(_cfg(), runner=lambda goal: "ok")
        ma = type("M", (), {"_api": api_a, "id": "m-private"})()
        mb = type("M", (), {"_api": api_b, "id": "m-group"})()
        b.remember_context("c:me", ma, None)
        b.remember_context("g:G1:me", mb, None)
        assert b._ctx_for("c:me")["msg_id"] == "m-private"
        assert b._ctx_for("g:G1:me")["msg_id"] == "m-group"
        assert b._ctx_for("c:me")["api"] is api_a

    def test_approval_goes_back_to_its_own_session(self):
        sent = []
        b = QQBotBridge(_cfg(), runner=lambda goal: "ok")
        b.approve_timeout = 0.05
        b._ctx["c:me"] = {"api": _FakeApi(), "msg_id": "m1",
                          "loop": None, "recv_at": time.time()}
        b._ctx["c:friend"] = {"api": _FakeApi(), "msg_id": "m2",
                              "loop": None, "recv_at": time.time()}
        b._send_sync = lambda key, text: sent.append(key)
        req = type("R", (), {"tool_name": "terminal", "command": "ls", "reason": "测试"})()
        b.begin_approval("me", req)
        assert sent == ["c:me"], "审批要发回提问的那个会话"


class TestTaskTimeout:
    """超时不硬杀线程：给 agent 置停止信号，让它优雅收尾并保留成果。"""

    def test_timeout_stops_agent_and_keeps_partial_result(self):
        class _SlowAgent:
            def run(self, goal, keep_session=False, stop_event=None):
                assert stop_event is not None, "必须把停止信号交给 agent"
                stop_event.wait(5)                   # 等超时置位
                return "部分成果"

        s = UserSession("u")
        s.agent = _SlowAgent()
        s.task_timeout = 0.1
        out = s.run("干活")
        assert "已中断" in out and "部分成果" in out
        assert s.timed_out is True

    def test_normal_run_is_untouched(self):
        class _FastAgent:
            def run(self, goal, keep_session=False, stop_event=None):
                return "正常完成"

        s = UserSession("u")
        s.agent = _FastAgent()
        s.task_timeout = 30
        assert s.run("干活") == "正常完成"
        assert s.timed_out is False

    def test_timeout_can_be_disabled(self):
        class _FastAgent:
            def run(self, goal, keep_session=False, stop_event=None):
                assert stop_event is not None
                return "正常完成"

        s = UserSession("u")
        s.agent = _FastAgent()
        s.task_timeout = 0                            # <=0 不限时
        assert s.run("干活") == "正常完成"

    def test_session_defaults_to_module_timeout(self):
        assert UserSession("u").task_timeout == TASK_TIMEOUT
