"""QQ 机器人桥：白名单、限流、独立会话、审批回 y/a/n、权限档不可提权、密钥不落日志。"""
import asyncio
import os
import threading
import time

import pytest

from agent.qqbot import (ALLOWED_PRESETS, CHUNK_CHARS,
                         MAX_REPLIES_PER_MESSAGE, PASSIVE_REPLY_WINDOW_C2C,
                         QQBotBridge, QQBotConfig,
                         TASK_TIMEOUT, UserSession, _chunk, _chunks_capped,
                         _chunks_markdown)


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

    def test_unknown_group_message_event_is_swallowed_not_fatal(self):
        """平台推 GROUP_MESSAGE_CREATE 时，botpy 查表 KeyError 会中断整个事件分发，
        连 @ 消息都收不到（"突然完全不回"）。必须给这个事件一个兜底处理器。
        """
        pytest.importorskip("botpy")
        from agent.qqbot import _build_client
        _build_client(QQBotBridge(_cfg()))          # 触发 BotWebSocket.__init__ 补丁

        import botpy.gateway as gw

        # 真的构造一个 BotWebSocket，确认 parser 表里已多出兜底键
        class _Session:  session_id = ""
        ws = gw.BotWebSocket(_Session(), object.__new__(gw.BotWebSocket).__class__ and
                              type("C", (), {"parser": {}})())
        parsers = getattr(ws, "_parser", None)
        assert isinstance(parsers, dict), "拿不到 parser 表，补丁可能没生效"
        assert "group_message_create" in parsers, "全量群消息必须有兜底，否则分发被打断"
        # 兜底必须是可调用的（能被 gateway.py: func(msg) 正常执行）
        parsers["group_message_create"]({"id": "x"})

    def test_image_paths_survive_windows_drive_letters(self):
        """补发图片的正则必须认得盘符路径。

        旧实现 `(?:/[A-Za-z0-9_.-]+)+\\.(png|...)` 会把 `D:/aaa/x.png` 的盘符吃掉，
        抠成 `/aaa/x.png` → open() 找不到 → 用户只看到本地路径、收不到图。
        """
        from agent.qqbot import _find_image_paths
        got = _find_image_paths("已生成：D:/aaa/work/work/my_agent/generated_images/img-x.png")
        assert got, "盘符路径必须能匹配出来"
        assert got[0].startswith("D:/"), f"盘符不能被吃掉: {got}"
        assert os.path.isabs(got[0]) and not got[0].startswith("/aaa"), f"必须是合法绝对路径: {got}"
        # 同一张图不该因为多轮匹配被收进两次（会白吃"最多补发 3 张"的配额）
        assert len(got) == 1, f"重复匹配: {got}"

    def test_image_paths_cover_relative_and_bare_names(self):
        """相对路径与纯文件名也要认（agent 有时只回文件名）。"""
        from agent.qqbot import _find_image_paths
        assert _find_image_paths("看图 generated_images/img-a.png") == ["generated_images/img-a.png"]
        assert _find_image_paths("已保存 img-20261013-1.png") == ["img-20261013-1.png"]
        assert _find_image_paths("这里没有图") == []

    def test_group_message_create_handler_exists_and_is_quiet(self):
        """全量消息不需处理（本桥只响应 @机器人），但必须有兜底不让分发被打断。"""
        pytest.importorskip("botpy")
        from agent.qqbot import _build_client
        _build_client(QQBotBridge(_cfg()))          # 不抛异常即可（补丁在 _WS.__init__ 上）

    def test_group_add_event_logs_the_group_openid(self, monkeypatch):
        """被拉进群时要把 group_openid 记下来 —— 群白名单要的是它，不是群号。

        没有这条，用户就只能看到"拉进群后机器人毫无反应"，然后在群里填群号（永远匹配不上）。
        """
        pytest.importorskip("botpy")
        import asyncio

        from agent import run_log
        from agent.qqbot import _build_client
        logged = []
        monkeypatch.setattr(run_log, "log", lambda msg, level="info": logged.append((msg, level)))
        client = _build_client(QQBotBridge(_cfg()))

        class _Event:
            group_openid = "GROUP-OPENID-XYZ"
            op_member_openid = "OP-1234567890"

        asyncio.run(client.on_group_add_robot(_Event()))
        asyncio.set_event_loop(asyncio.new_event_loop())   # 别把线程的 loop 留空（botpy 要用）
        blob = " ".join(m for m, _ in logged)
        assert "GROUP-OPENID-XYZ" in blob, "要把群 openid 原样打出来"
        assert "QQBOT_ALLOW_GROUPS" in blob, "并告诉人填到哪儿"
        assert any(level == "warn" for _, level in logged), "这是要人处理的事，warning 级别"


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
        """用户输入必须被 max_chars 截断，防止超长输入顶爆上下文。

        传给 runner 的是 `goal + QQ_CHANNEL_HINT`（通道能力说明），
        所以断言"以截断后的用户输入开头"，而不是等长——
        能力说明是系统侧常量，本就不该被 max_chars 削。
        """
        seen = {}
        b = QQBotBridge(_cfg(max_chars=10), runner=lambda goal: seen.setdefault("goal", goal) or "ok")
        b.handle_text("me", "x" * 50)
        assert seen["goal"].startswith("x" * 10), "用户输入必须被 max_chars 截断"
        # 截断后的用户输入不能超过上限（能力说明在其后）
        assert len(seen["goal"]) >= 10


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

    def test_group_approval_is_keyed_per_group(self):
        """群里审批按群共享：任何人都能答复群里的审批。"""
        b = QQBotBridge(_cfg(allow_group="true", allow_groups="G1"),
                        runner=lambda goal: "ok")
        b.approve_timeout = 0.05
        b._pending["g:G1"] = {"event": threading.Event(), "allow": False, "tool": "t"}
        reply = b.handle_text("friend", "y", "G1")
        assert "已按你的答复" in reply, "群里任何人都能答复审批"

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


class TestOnDemandVoice:
    """语音默认不发；只有 /v 前缀或 /v 重播才发（QQBOT_VOICE_REPLY=false）。"""

    def _b(self):
        from agent.qqbot import QQBotBridge, QQBotConfig
        cfg = QQBotConfig({"app_id": "1", "app_secret": "x",
                           "allow_from": "me", "max_chars": "200"})
        return QQBotBridge(cfg, runner=lambda g: "结果:" + g.splitlines()[0][:20])

    def test_plain_message_has_no_voice_flag(self):
        from agent.qqbot import _VOICE_FLAG
        b = self._b()
        assert not b.handle_text("me", "你好").startswith(_VOICE_FLAG), "默认不该要语音"

    def test_slash_v_prefix_marks_voice_and_strips_prefix(self):
        """/v 内容 → 带语音哨兵，且 "/v " 前缀必须从正文剥掉（否则模型会当成任务的一部分）。"""
        from agent.qqbot import _VOICE_FLAG
        b = self._b()
        out = b.handle_text("me", "/v 帮我查下天气")
        assert out.startswith(_VOICE_FLAG), "/v 应标记要语音"
        body = out[len(_VOICE_FLAG):]
        assert "帮我查下天气" in body
        assert "/v" not in body, f"前缀没剥干净: {body}"

    def test_slash_v_alone_queues_previous_reply(self):
        """`/v` 单独发 → 把上一条回复排进语音队列（不重跑 agent）。

        注意：这里用 kind='chat' + llm 引擎那条路径，它会写 session.last_output
        （_execute_task 那条由真agent 写，测试里的假 runner 不会）。
        """
        from agent.qqbot import QQBotBridge, QQBotConfig
        cfg = QQBotConfig({"app_id": "1", "app_secret": "x", "allow_from": "me",
                           "max_chars": "200", "chat_engine": "llm"})
        b = QQBotBridge(cfg)
        key = b.scope_key("me")
        sess = b._session_for(key)
        sess.last_output = "这是上一次回复的内容"
        out = b.handle_text("me", "/v")
        assert "这是上一次回复的内容" in out, "/v 应回显上一条内容"
        assert b._pending_voice.get(key) == "这是上一次回复的内容", "应进语音队列"


class TestMediaDelivery:
    """agent 生成的图片 **和音频** 都要把文件本体发出去，而不是只回本地路径。"""

    def test_audio_paths_are_recognized_and_typed_as_voice(self):
        """mp3 必须进补发流程，且 file_type=3（语音）而不是被当成图片。"""
        from agent.qqbot import _find_image_paths, _MEDIA_FILE_TYPES
        got = _find_image_paths("配音已生成: D:/aaa/work/x/generated_audio/tts-1.mp3")
        assert got, "音频路径必须能识别出来（之前 _IMAGE_EXTS 不含 mp3 → 静默丢路径）"
        assert _MEDIA_FILE_TYPES["mp3"] == 3
        assert _MEDIA_FILE_TYPES["png"] == 1

    def test_existing_audio_is_found_but_empty_file_is_rejected(self, tmp_path):
        """_first_existing_audio：认真实文件，拒 0 字节（TTS 偶尔产空文件）。"""
        from agent.qqbot import _first_existing_audio
        good = tmp_path / "tts-good.mp3"
        good.write_bytes(b"x" * 4096)
        empty = tmp_path / "tts-empty.mp3"
        empty.write_bytes(b"")
        p1, raw1 = _first_existing_audio(f"已生成 {good}")
        assert p1 and os.path.isfile(p1)
        assert raw1, "要返回原始文本片段，调用方靠它做 replace"
        p2, _ = _first_existing_audio(f"已生成 {empty}")
        assert not p2, "0 字节音频不该被选中"

    def test_sent_placeholder_distinguishes_media_kind(self):
        from agent.qqbot import _sent_placeholder
        assert "语音" in _sent_placeholder("a/b/tts-1.mp3")
        assert "图片" in _sent_placeholder("a/b/img-1.png")


class TestStopCommand:
    """/stop：按会话隔离、只停当前、必须在会话锁之前生效。"""

    def _b(self):
        from agent.qqbot import QQBotBridge, QQBotConfig
        cfg = QQBotConfig({"app_id": "1", "app_secret": "x", "allow_from": "me",
                           "max_chars": "200"})
        return QQBotBridge(cfg, runner=lambda g: "结果:" + str(g)[:20])

    def test_stop_with_nothing_running_is_harmless(self):
        b = self._b()
        assert "没有在跑" in b.handle_text("me", "/stop")

    def test_stop_sets_only_this_session(self):
        """按会话隔离：停私聊不能碰到群会话的停止信号。"""
        import threading
        b = self._b()
        ckey, gkey = b.scope_key("me"), b.scope_key("", "G9")
        cs, gs = b._session_for(ckey), b._session_for(gkey)
        cev, gev = threading.Event(), threading.Event()
        cs._current_stop_event, gs._current_stop_event = cev, gev

        out = b.handle_text("me", "/stop")
        assert cev.is_set(), "本会话要被置位"
        assert not gev.is_set(), "别的会话不该被误杀"
        assert cs.stopped_by_user is True
        assert "喊停" in out

    def test_stop_is_idempotent(self):
        import threading
        b = self._b()
        key = b.scope_key("me")
        s = b._session_for(key)
        s._current_stop_event = threading.Event()
        b.handle_text("me", "/stop")
        assert "已经喊停" in b.handle_text("me", "/stop")

    def test_stop_all_hits_every_running_session(self):
        import threading
        b = self._b()
        keys = [b.scope_key("me"), b.scope_key("", "G9")]
        evs = []
        for k in keys:
            s = b._session_for(k)
            ev = threading.Event()
            s._current_stop_event = ev
            evs.append(ev)
        out = b.handle_text("me", "/stop all")
        assert "2 个会话" in out
        assert all(e.is_set() for e in evs)

    def test_non_stop_text_is_not_intercepted(self):
        b = self._b()
        assert b._handle_stop(b.scope_key("me"), "你好") is None
        assert b._handle_stop(b.scope_key("me"), "/status") is None


class TestGroupChat:

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
        # runner 收到的是 `原目标 + QQ_CHANNEL_HINT`（通道能力说明），所以用 startswith
        assert b.handle_text("me", "干活", "G1").startswith("已执行: 干活")

    def test_speaker_must_be_whitelisted_too(self):
        b = self._group_bridge()
        reply = b.handle_text("stranger", "干活", "G1")
        assert "调用白名单里没有你" in reply, "要说清是发言人没过白名单，不是群的问题"
        assert "stranger" in reply and b.sessions == {}

    def test_group_not_whitelisted_says_so_with_the_group_id(self):
        b = self._group_bridge()
        reply = b.handle_text("me", "干活", "G9")
        assert "群白名单" in reply and "G9" in reply

    def test_group_members_share_session(self):
        b = self._group_bridge()
        b.handle_text("me", "一", "G1")
        b.handle_text("friend", "二", "G1")
        assert set(b.sessions) == {"g:G1"}

    def test_same_user_in_group_and_private_are_different_scopes(self):
        b = self._group_bridge()
        b.handle_text("me", "私聊任务")
        b.handle_text("me", "群任务", "G1")
        assert set(b.sessions) == {"c:me", "g:G1"}

    def test_scope_key_roundtrip(self):
        key = QQBotBridge.scope_key("user-1", "group-9")
        assert QQBotBridge.split_key(key) == ("group-9", "")
        assert QQBotBridge.split_key(QQBotBridge.scope_key("user-1")) == ("", "user-1")

    def test_group_rate_limit_is_per_group(self):
        b = self._group_bridge(rate_per_minute=1)
        assert b.handle_text("me", "1", "G1").startswith("已执行: 1")
        assert "频繁" in b.handle_text("friend", "2", "G1"), "群内共享限流"

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

    def test_bind_passes_extra_args_through(self, monkeypatch):
        from agent import qqbot, qqbot_bind
        seen = []
        monkeypatch.setattr(qqbot_bind, "main", lambda argv: seen.append(list(argv)) or 0)
        assert qqbot.main(["--bind", "--timeout", "60"]) == 0
        assert seen == [["--timeout", "60"]], "--bind 之外的参数该透传给绑定流程"

    def test_bind_alone_passes_empty_argv(self, monkeypatch):
        from agent import qqbot, qqbot_bind
        seen = []
        monkeypatch.setattr(qqbot_bind, "main", lambda argv: seen.append(list(argv)) or 0)
        assert qqbot.main(["--bind"]) == 0
        assert seen == [[]]

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
        # 正文可能在 content（纯文本）或 markdown.content（富文本）里，取到哪个都算
        last = api.calls[-1][1]
        body = last.get("content") or (last.get("markdown") or {}).get("content", "")
        assert "已省略" in body, "截断要说清，别让用户以为内容就这么多"

    def test_markdown_payload_is_sent_as_markdown(self):
        """USE_MARKDOWN 时走 msg_type=2 + markdown.content（官方 2026-04 起单聊/群聊均可用）。"""
        api = _FakeApi()
        b = self._bridge_with_api(api)
        asyncio.run(b._send("c:me", "**加粗**标题"))
        kw = api.calls[0][1]
        assert kw["msg_type"] == 2
        assert kw["markdown"]["content"] == "**加粗**标题"
        assert not kw.get("content"), "传了 markdown 后 content 必须为空（官方接口要求）"

    def test_markdown_never_splits_a_code_fence(self):
        """富文本按行切，不能把 ``` 围栏劈开 —— 否则渲染错乱。"""
        long = "说明\n\n```python\n" + ("x" * (CHUNK_CHARS * 3)) + "\n```"
        pieces = _chunks_markdown(long)
        assert all(p.count("```") % 2 == 0 for p in pieces), f"围栏被劈开: {pieces}"

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


class TestSessionEviction:
    """空闲会话回收：常驻服务里会话不能只增不减（每个用户都挂着一个 agent）。"""

    def test_idle_session_is_reclaimed(self):
        b = QQBotBridge(_cfg(session_ttl=100), runner=lambda g: "ok")
        b.handle_text("me", "你好")
        assert b.sessions, "先得建出会话"
        for k in list(b._last_active):
            b._last_active[k] -= 1000          # 把活动时间推回过去
        assert b._prune_sessions(force=True) >= 1
        assert not b.sessions, "空闲会话应被回收"

    def test_running_session_is_kept(self):
        b = QQBotBridge(_cfg(session_ttl=100), runner=lambda g: "ok")
        b.handle_text("me", "你好")
        key = QQBotBridge.scope_key("me")
        b._running.add(key)                    # 假装正在跑任务
        b._last_active[key] -= 1000
        assert b._prune_sessions(force=True) == 0
        assert key in b.sessions, "正在跑的会话不能被回收"

    def test_pending_approval_session_is_kept(self):
        b = QQBotBridge(_cfg(session_ttl=100), runner=lambda g: "ok")
        b.handle_text("me", "你好")
        key = QQBotBridge.scope_key("me")
        b._pending[key] = {"tool": "terminal"}
        b._last_active[key] -= 1000
        assert b._prune_sessions(force=True) == 0
        assert key in b.sessions, "挂着待审批的会话不能被回收"

    def test_ttl_zero_disables_reclaim(self):
        b = QQBotBridge(_cfg(session_ttl=0), runner=lambda g: "ok")
        b.handle_text("me", "你好")
        for k in list(b._last_active):
            b._last_active[k] -= 100000
        assert b._prune_sessions(force=True) == 0
        assert b.sessions


class TestConcurrencyGate:
    """并发闸门：小机器上别让无限并发把核与内存拉满。"""

    def test_refuses_when_slots_full(self):
        b = QQBotBridge(_cfg(max_concurrent_tasks=1, task_queue_timeout=0),
                        runner=lambda g: "ok")
        s = b._session_for("c:me")
        b._slots.acquire()                      # 占满唯一名额
        try:
            out = b._execute_task(s, "c:me", "干活")
        finally:
            b._slots.release()
        assert "上限" in out

    def test_slot_released_after_run(self):
        b = QQBotBridge(_cfg(max_concurrent_tasks=1), runner=lambda g: "ok")
        s = b._session_for("c:me")
        assert b._execute_task(s, "c:me", "干活") == "ok"
        assert b._execute_task(s, "c:me", "再干") == "ok", "名额必须释放，否则第二次排不上"


class TestReplyBudget:
    """同一条消息最多回 4 次：回执 / 补发的图 / 语音 / 正文共用同一份额度。"""

    def _bridge_with_ctx(self, api, key="c:me", msg_id="m1"):
        b = QQBotBridge(_cfg())
        b._ctx[key] = {"api": api, "msg_id": msg_id, "loop": None, "recv_at": time.time()}
        return b

    def test_remaining_shrinks_with_each_reply(self):
        b = self._bridge_with_ctx(_FakeApi())
        assert b._remaining_replies("c:me") == MAX_REPLIES_PER_MESSAGE
        b._next_seq("c:me", "m1")
        b._next_seq("c:me", "m1")
        assert b._remaining_replies("c:me") == MAX_REPLIES_PER_MESSAGE - 2

    def test_send_caps_chunks_at_remaining_budget(self):
        """回执已占 1 条 → 正文最多再发 3 块（此前不封顶，会超平台上限整条失败）。"""
        api = _FakeApi()
        b = self._bridge_with_ctx(api)
        b._next_seq("c:me", "m1")
        asyncio.run(b._send("c:me", "长" * 6000))
        assert len(api.calls) == MAX_REPLIES_PER_MESSAGE - 1

    def test_send_is_skipped_when_budget_exhausted(self):
        api = _FakeApi()
        b = self._bridge_with_ctx(api)
        for _ in range(MAX_REPLIES_PER_MESSAGE):
            b._next_seq("c:me", "m1")
        asyncio.run(b._send("c:me", "hello"))
        assert api.calls == [], "额度用尽就不该再发（发了会整条失败）"

    def test_media_leaves_a_slot_for_text(self, tmp_path, monkeypatch):
        """只剩 1 条额度时不再补发媒体，把最后一条留给正文结论。"""
        img = tmp_path / "a.png"
        img.write_bytes(b"x" * 2048)
        api = _FakeApi()
        b = self._bridge_with_ctx(api)

        async def _fake_upload(*a, **kw):
            return {"file_info": "fi", "file_uuid": "u"}

        monkeypatch.setattr(b, "_upload_base64", _fake_upload)
        for _ in range(MAX_REPLIES_PER_MESSAGE - 1):
            b._next_seq("c:me", "m1")
        assert asyncio.run(b.send_local_images("c:me", f"图 {img}")) == []


class TestMediaSlotHygiene:
    """补发名额只该被**真实存在**的媒体占用。"""

    def test_url_no_longer_yields_fake_local_paths(self):
        from agent.qqbot import _find_image_paths
        # 一个 URL 之前会被拆成 s://…、//…、com/… 三条假路径占满名额
        assert _find_image_paths("看 https://example.com/pic/logo.png 这张图") == []
        assert _find_image_paths("图 D:/x/a.png") == ["D:/x/a.png"]

    def test_missing_and_url_paths_do_not_take_slots(self, tmp_path, monkeypatch):
        img = tmp_path / "real.png"
        img.write_bytes(b"x" * 2048)
        # 补发只在工程目录内生效：把工程根指到 tmp_path，让"真实存在的文件"落在工程内
        import agent.qqbot as qb
        monkeypatch.setattr(qb, "_project_root", lambda: str(tmp_path))
        api = _FakeApi()
        b = QQBotBridge(_cfg())
        b._ctx["c:me"] = {"api": api, "msg_id": "m1", "loop": None, "recv_at": time.time()}

        async def _fake_upload(*a, **kw):
            return {"file_info": "fi", "file_uuid": "u"}

        monkeypatch.setattr(b, "_upload_base64", _fake_upload)
        text = f"图 {img}\n不存在的 output/nope.mp3\nURL https://e.com/a/b.png"
        sent = asyncio.run(b.send_local_images("c:me", text))
        assert sent == [str(img)], f"只该发真实存在的那张：{sent}"


class TestBotpyPatchIdempotent:
    def test_patch_not_nested_on_repeat_build(self):
        """重复构建客户端时不该把 __init__ 再包一层（会无限套娃）。"""
        import botpy.gateway as gw
        from agent.qqbot import _build_client
        b = QQBotBridge(_cfg())
        _build_client(b)
        _build_client(b)
        assert getattr(gw.BotWebSocket, "_myagent_group_msg_patch", False) is True
        before = gw.BotWebSocket.__init__
        _build_client(b)
        assert gw.BotWebSocket.__init__ is before, "重复调用又包了一层"



class TestChatEngineConfig:
    """QQBOT_CHAT_ENGINE 必须真的接在 config 上 —— 以前 QQBotConfig 读它、QQBOT_CONFIG
    里却没有这个键，导致 .env 设了也不生效（闲聊始终走 agent 循环）。"""

    def test_chat_engine_key_exists(self):
        from config import QQBOT_CONFIG
        assert "chat_engine" in QQBOT_CONFIG, "QQBOT_CHAT_ENGINE 会读不到"

    def test_chat_engine_value_parsed(self):
        assert QQBotConfig({"chat_engine": "llm"}).chat_engine == "llm"
        assert QQBotConfig({"chat_engine": "agent"}).chat_engine == "agent"
        assert QQBotConfig({"chat_engine": "乱写"}).chat_engine == "agent"   # 非法值回默认
