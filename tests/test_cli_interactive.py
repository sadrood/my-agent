"""交互模式命令分发回归（此前**零测试覆盖**，所以踩了坑也没被发现）。

实测故障（2026-09-22 审计）：`/goal` 分支里写的是 `store = GoalStore()`，覆盖了
交互循环开头 `store = SessionStore()` 的**同一个局部变量**；分支末尾 `continue` 让
覆盖持续存在，于是之后的 `/sessions`（`store.list_conversations()`）与 `/open`
必抛 AttributeError —— 而命令分发**不在任何 try 里**，异常直接冲出
`run_interactive`，整个 CLI 带 traceback 退出。
"""
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
            return script.pop(0) if script else None

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
