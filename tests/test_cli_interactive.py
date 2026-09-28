"""交互模式命令分发回归（之前**零测试覆盖**，所以踩了坑也没被发现）。"""
import pytest

import main as m


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """把交互循环跑在沙箱里：假 Agent + 脚本化输入，返回 (输出, 启动器)。"""
    from agent.session import SessionStore

    monkeypatch.setattr(
        "agent.session.SessionStore",
        lambda: SessionStore(config={"dir": str(tmp_path / "sessions"),
                                     "max_sessions": 50}))

    made = {}

    class _FakeGoalStore:
        def __init__(self):
            made["goal_store"] = self

        def get(self, name):
            return ""

        def set(self, name, goal):
            made["goal"] = goal

    monkeypatch.setattr("agent.goal.GoalStore", _FakeGoalStore)

    class _FakeClient:
        api_key = "sk-test"
        base_url = "http://127.0.0.1:1/v1"

    class _FakeLLM:
        def __init__(self):
            self.client = _FakeClient()
            self.default_model = "fake-model"

    class _FakeAgent:
        def __init__(self, config=None, **kw):
            from agent import AgentConfig
            self.config = config or AgentConfig(verbose=False)
            self.llm = _FakeLLM()
            self.tool_manager = None
            self.memory = None

        def _restore_conversation_model(self, data):
            pass

        def run(self, *a, **k):
            return "（假回答）"

    monkeypatch.setattr(m, "Agent", _FakeAgent)

    def _launch(commands):
        script = list(commands)

        def _read_goal(prompt_primary=None, prompt_continuation=None):
            item = script.pop(0) if script else None
            if isinstance(item, BaseException):     # 脚本里放异常对象 → 抛出（验证主循环兜底）
                raise item
            return item

        # input_reader 是在 run_interactive 里局部 import 的，所以打在模块上
        monkeypatch.setattr("agent.input_reader.read_goal", _read_goal)
        m.run_interactive()
        return made

    return _launch


def test_goal_then_sessions_does_not_crash(drive):
    """`/goal` 之后再 `/sessions`：必须正常列出，而不是 AttributeError 崩掉。"""
    made = drive(["/goal 写一本小说", "/sessions", "exit"])
    assert made.get("goal") == "写一本小说"


def test_goal_then_open_does_not_crash(drive):
    """`/open` 走的是同一个 `store`，同样不能被打坏。"""
    drive(["/goal 写一本小说", "/open conv-20260922-abcdef", "exit"])


def test_sessions_before_goal_still_works(drive):
    """顺序反过来（先 /sessions 再 /goal）本来就没问题，确认没被我改坏。"""
    drive(["/sessions", "/goal 目标X", "exit"])


def test_decode_error_does_not_kill_the_loop(drive):
    """Linux 粘贴撞上 UnicodeDecodeError：只作废本次输入，主循环必须继续（而不是进程退出）。"""
    import codecs
    bad = UnicodeDecodeError("utf-8", b"\xe6", 0, 1, "invalid continuation byte")
    made = drive([bad, "/goal 解码失败之后还能用", "exit"])
    assert made.get("goal") == "解码失败之后还能用", "异常之后必须回到提示符继续工作"


class _ConsoleStub:
    def __init__(self):
        self.printed = []

    def print(self, text, end=""):
        self.printed.append(text)


def test_prompt_callbacks_read_on_windows_but_only_show_on_posix(monkeypatch):
    """Windows 回调必须**读取**；POSIX 只显示（读取归单一字节读取者）。"""
    import main as m

    class _FakePrompt:
        def __init__(self, text, console=None):
            self.text = text

        def __call__(self):
            return "输入的这一行"

    console = _ConsoleStub()

    monkeypatch.setattr(m.sys, "platform", "win32")
    primary, continuation = m._prompt_callbacks(console, _FakePrompt)
    assert primary("> ") == "输入的这一行", "Windows 上不读输入 = 没人读输入"
    assert continuation("… ") == "输入的这一行"

    monkeypatch.setattr(m.sys, "platform", "linux")
    primary, _ = m._prompt_callbacks(console, _FakePrompt)
    assert primary("> ") is None, "POSIX 只显示提示符，读取交给 input_reader"
    assert "> " in console.printed


def test_windows_loop_reads_and_exits_cleanly(monkeypatch, tmp_path):
    """Windows 布线端到端：喂 exit 必须正常退出（曾经因回调只显示而抛 AttributeError）。"""
    import main as m
    from agent import input_reader

    monkeypatch.setattr(m.sys, "platform", "win32")

    class _FakeClient:
        api_key = "sk-test"
        base_url = "http://127.0.0.1:1/v1"

    class _FakeLLM:
        def __init__(self):
            self.client = _FakeClient()
            self.default_model = "fake-model"

    class _FakeAgent:                      # 不构造真 Agent：它会联网探测，测试会挂住
        def __init__(self, config=None, **kw):
            from agent import AgentConfig
            self.config = config or AgentConfig(verbose=False)
            self.llm = _FakeLLM()
            self.tool_manager = None
            self.memory = None

        def _restore_conversation_model(self, data):
            pass

        def run(self, *a, **k):
            return "（假回答）"

    monkeypatch.setattr(m, "Agent", _FakeAgent)

    # _AgentPrompt 是 run_interactive 内部的类：直接打基类的 __call__ 喂输入
    monkeypatch.setattr("rich.prompt.Prompt.__call__", lambda self: "exit")

    from agent.session import SessionStore as _RealStore
    monkeypatch.setattr(
        "agent.session.SessionStore",
        lambda *a, **k: _RealStore(config={"dir": str(tmp_path / "sessions"),
                                           "max_sessions": 20}))

    monkeypatch.setattr(input_reader, "read_goal", input_reader.read_goal)   # 用真实实现
    m.run_interactive()          # 不抛异常即通过；exit 让它自己退出
