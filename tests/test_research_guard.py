"""研究员内部调用必须过同一道审批门。

research 直接调 ToolManager（不经 Executor），历史上绕过了审批策略：
同一条 browser 命令走工具调用会被拦，走研究员内部却不会。
"""
from types import SimpleNamespace

from tools.base import ToolResult
from tools.research import DeepResearcher


class FakeToolManager:
    """只实现研究员用到的那两个方法。"""

    def __init__(self, html="<a href='https://example.org/a'>A</a>"):
        self.calls = []
        self._html = html

    def get_tool(self, name):
        return object()          # 非 None 即视为浏览器可用

    def execute(self, name, command):
        self.calls.append((name, command))
        if command == "html":
            return ToolResult(success=True, output=self._html)
        return ToolResult(success=True, output="搜索结果正文 https://example.org/a")


def _fast(monkeypatch):
    monkeypatch.setattr("tools.research.time.sleep", lambda *_: None)


def test_denied_guard_blocks_all_browsing(monkeypatch):
    _fast(monkeypatch)
    tm = FakeToolManager()
    seen = []

    def guard(tool, cmd):
        seen.append((tool, cmd))
        return "审批策略拒绝"

    researcher = DeepResearcher(tm, guard=guard)
    sources = researcher._search_and_collect("q", limit=2)

    assert sources == [], "被拒后不该产出任何来源"
    assert tm.calls == [], "被拒后不该真的执行浏览器命令"
    assert seen and seen[0][0] == "browser"


def test_allowed_guard_still_runs(monkeypatch):
    _fast(monkeypatch)
    tm = FakeToolManager()
    researcher = DeepResearcher(tm, guard=lambda tool, cmd: "")

    researcher._search_and_collect("q", limit=1)
    commands = [cmd for _, cmd in tm.calls]
    assert any(c.startswith("goto ") for c in commands), commands
    assert "text" in commands and "html" in commands


def test_without_guard_behaves_as_before(monkeypatch):
    """没注入守卫（已在外层审批内的调用）不能被改变行为。"""
    _fast(monkeypatch)
    tm = FakeToolManager()
    DeepResearcher(tm)._search_and_collect("q", limit=1)
    assert tm.calls, "无守卫时应照旧直接调用"


def test_make_tool_guard_uses_policy_decision():
    from agent.approval import make_tool_guard

    class _Deny:
        def decide(self, request):
            return SimpleNamespace(allowed=False, reason="策略 never：拒绝")

    class _TM:
        def build_approval_request(self, name, args):
            return object()

    guard = make_tool_guard(_Deny(), _TM())
    assert "拒绝" in guard("browser", "goto https://x")

    class _Allow:
        def decide(self, request):
            return SimpleNamespace(allowed=True, reason="")

    assert make_tool_guard(_Allow(), _TM())("browser", "goto https://x") == ""
    assert make_tool_guard(None, _TM())("browser", "goto https://x") == ""
