"""QQ 桥的对话/任务双通道：寒暄不再被当成任务扔进工具循环。"""
import pytest

from agent.qqbot import CHITCHAT_HINTS, QQBotBridge, QQBotConfig, UserSession, looks_like_chitchat


def _cfg(**kw):
    base = {"app_id": "1", "app_secret": "s", "allow_from": "me"}
    base.update(kw)
    return QQBotConfig(base)


class _FakeLLM:
    def __init__(self, reply="你好呀，我在。"):
        self.reply = reply
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return self.reply


class TestChitchatDetection:
    @pytest.mark.parametrize("text", ["你好", "您好", "hi", "Hi", "hello", "在吗",
                                      "在吗？", "谢谢", "thanks", "辛苦了", "哈喽"])
    def test_greetings_are_chitchat(self, text):
        assert looks_like_chitchat(text) is True

    @pytest.mark.parametrize("text", ["帮我抓取 https://example.com 的标题",
                                      "把 output 里的临时文件删掉",
                                      "你好，帮我查一下今天的天气怎么样",
                                      "在吗，我需要你把 README 更新一下",
                                      ""])
    def test_real_requests_are_not_chitchat(self, text):
        assert looks_like_chitchat(text) is False

    def test_hints_cover_common_greetings(self):
        assert "你好" in CHITCHAT_HINTS and "hi" in CHITCHAT_HINTS


class TestChatPath:
    def _bridge(self, **cfg_kw):
        cfg_kw.setdefault("chat_engine", "llm")     # 默认引擎=agent，这里是轻量路径的用例
        b = QQBotBridge(_cfg(**cfg_kw), runner=lambda goal: f"任务被执行: {goal}")
        session = b._session_for("c:me")
        return b, session

    def test_greeting_does_not_start_a_task(self):
        b, session = self._bridge()
        ran = []
        b._run_task = lambda s, k, g: ran.append(g) or "任务"
        session.chat = lambda text: f"（聊天）{text}"
        assert b.handle_text("me", "你好") == "（聊天）你好"
        assert ran == [], "打招呼绝不能进任务循环"

    def test_slash_do_forces_a_task(self):
        b, session = self._bridge()
        assert b.handle_text("me", "/do 抓取首页标题").startswith("任务被执行")
        assert session.mode == "task", "/do 不改模式"

    def test_slash_chat_switches_mode_and_next_message_chats(self):
        b, session = self._bridge()
        assert "对话模式" in b.handle_text("me", "/chat")
        session.chat = lambda text: f"（聊天）{text}"
        assert b.handle_text("me", "今天过得怎么样") == "（聊天）今天过得怎么样"
        assert session.mode == "chat"

    def test_slash_task_switches_back(self):
        b, session = self._bridge()
        b.handle_text("me", "/chat")
        assert "任务模式" in b.handle_text("me", "/task")
        assert b.handle_text("me", "把 output 里的旧日志清一下").startswith("任务被执行")

    def test_chat_mode_with_inline_text_answers_once(self):
        b, session = self._bridge()
        session.chat = lambda text: f"（聊天）{text}"
        assert b.handle_text("me", "/chat 你在吗") == "（聊天）你在吗"
        assert session.mode == "task", "带内容的 /chat 只作用于这一条"

    def test_default_mode_can_be_chat(self):
        b = QQBotBridge(_cfg(default_mode="chat", chat_engine="llm"),
                        runner=lambda g: f"任务: {g}")
        session = b._session_for("c:me")
        assert session.mode == "chat"
        session.chat = lambda text: "（聊天）好"
        assert b.handle_text("me", "随便聊聊") == "（聊天）好"

    def test_default_mode_is_task(self):
        b = QQBotBridge(_cfg(), runner=lambda g: f"任务: {g}")
        assert b._session_for("c:me").mode == "task"

    def test_unknown_mode_falls_back_to_task(self):
        assert _cfg(default_mode="whatever").default_mode == "task"

    def test_help_lists_the_new_commands(self):
        b, _ = self._bridge()
        help_text = b.handle_text("me", "/help")
        for cmd in ("/do", "/chat", "/task", "/shot", "/status"):
            assert cmd in help_text

    def test_status_shows_mode(self):
        b, _ = self._bridge()
        assert "模式 task" in b.handle_text("me", "/status")
        b.handle_text("me", "/chat")
        assert "模式 chat" in b.handle_text("me", "/status")


class TestAgentChatEngine:
    """默认引擎 = agent：闲聊走**同一个会话**（上下文与任务共享、落盘、需要时可用工具）。"""

    def _bridge(self, **cfg_kw):
        cfg_kw.setdefault("chat_engine", "agent")
        goals = []

        def _runner(goal):
            goals.append(goal)
            return f"回复: {goal.splitlines()[0]}"

        b = QQBotBridge(_cfg(**cfg_kw), runner=_runner)
        return b, goals

    def test_default_engine_is_agent(self):
        assert _cfg().chat_engine == "agent"
        assert _cfg(chat_engine="llm").chat_engine == "llm"
        assert _cfg(chat_engine="whatever").chat_engine == "agent"

    def test_greeting_goes_through_the_agent_session_with_a_hint(self):
        b, goals = self._bridge()
        out = b.handle_text("me", "你好")
        assert out.startswith("回复: 你好")
        assert len(goals) == 1 and goals[0].startswith("你好")
        assert "闲聊" in goals[0], "要带提示，否则同一条会话里又被当成任务"

    def test_chat_shares_the_same_session_as_tasks(self):
        """上下文共享：聊过之后发任务，走的是同一个 UserSession（同一个 agent 实例）。"""
        b, goals = self._bridge()
        b.handle_text("me", "你好")
        first = b.sessions["c:me"]
        b.handle_text("me", "把 README 里的错别字改一下")
        assert b.sessions["c:me"] is first, "闲聊与任务不能各起一个会话"
        assert len(goals) == 2, "两条都进了同一个 agent 循环"

    def test_chat_does_not_push_frames_or_screenshot(self):
        b, _ = self._bridge()
        calls = {"pusher": 0, "frame": 0}
        b._start_frame_pusher = lambda key: calls.__setitem__("pusher", calls["pusher"] + 1)
        b.schedule_frame = lambda key: calls.__setitem__("frame", calls["frame"] + 1)
        b.handle_text("me", "/chat")                  # 切到对话模式，避免被当成任务
        b.handle_text("me", "今天有什么好玩的")
        assert calls == {"pusher": 0, "frame": 0}, "闲聊不该抓帧/发截图"

    def test_task_path_still_pushes_frames(self):
        b, _ = self._bridge()
        calls = {"pusher": 0}
        b._start_frame_pusher = lambda key: calls.__setitem__("pusher", calls["pusher"] + 1)
        b.handle_text("me", "打开 example.com 读出标题")
        assert calls["pusher"] == 1, "任务期间仍要推帧（实时画面）"

    def test_new_session_clears_chat_history(self):
        b, _ = self._bridge(chat_engine="llm")
        session = b._session_for("c:me")
        session.chat_history = [{"role": "user", "content": "旧对话"}]
        assert "已开新会话" in b.handle_text("me", "/new")
        assert session.chat_history == []

    def test_status_shows_mode_and_engine(self):
        b, _ = self._bridge()
        assert "agent" in b.handle_text("me", "/status")


class TestChatSession:
    def test_chat_uses_llm_without_tools(self):
        """对话路径只调 LLM：agent.run（工具循环）绝不能被触发。"""
        session = UserSession("c:me", chat_turns=3, chat_max_tokens=256)
        llm = _FakeLLM("在的，怎么了？")

        class _Agent:
            def __init__(self):
                self.llm = llm
                self.run_called = False

            def run(self, *a, **kw):
                self.run_called = True
                return "不该走这里"

        agent = _Agent()
        session.agent = agent
        assert session.chat("在吗") == "在的，怎么了？"
        assert agent.run_called is False
        assert llm.calls, "必须真的问过 LLM"
        messages, kwargs = llm.calls[0]
        assert messages[0]["role"] == "system" and "不调用任何工具" in messages[0]["content"]
        assert kwargs.get("max_tokens") == 256

    def test_history_is_kept_and_capped(self):
        session = UserSession("c:me", chat_turns=2)
        llm = _FakeLLM("嗯")
        session.agent = type("A", (), {"llm": llm})()
        for i in range(5):
            session.chat(f"第{i}句")
        assert len(session.chat_history) == 10          # 5 问 5 答
        messages, _ = llm.calls[-1]
        assert len(messages) <= 1 + 2 * 2, "历史要按 chat_turns 截断"

    def test_blank_reply_is_handled(self):
        session = UserSession("c:me")
        session.agent = type("A", (), {"llm": _FakeLLM("   ")})()
        assert session.chat("喂") == ""
