"""工具 schema 门控：低频大工具的 schema 不常驻，用 tools 工具按需装载。"""
import json

import pytest

from tools.base import BaseTool, ToolResult
from tools.tool_manager import GATED_TOOLS, META_TOOL_NAME, ToolManager


class _BigTool(BaseTool):
    """体积大、低频的门控工具替身。"""

    def __init__(self, name: str, gated: bool = True):
        self._name = name
        self._gated = gated

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "示例大工具。第二句不该进清单。"

    @property
    def schema(self):
        return {"type": "object",
                "properties": {f"p{i}": {"type": "string", "description": "x" * 200}
                               for i in range(20)}}

    def execute(self, input_str):
        return ToolResult(success=True, output="ok")

    def execute_json(self, arguments):
        return ToolResult(success=True, output="ok")


@pytest.fixture
def manager(monkeypatch):
    tm = ToolManager()
    # 只留下可控的替身，门控判据看名字是否在 GATED_TOOLS 里
    monkeypatch.setattr("tools.tool_manager.GATED_TOOLS",
                        frozenset({"gated_a", "gated_b"}))
    tm._tools.clear()
    tm.register(_BigTool("core_x"))
    tm.register(_BigTool("gated_a"))
    tm.register(_BigTool("gated_b"))
    from tools.tool_meta import ToolsTool
    tm.register(ToolsTool(manager=tm))
    return tm


class TestGating:
    def test_gated_schemas_not_sent_by_default(self, manager):
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "core_x" in names and META_TOOL_NAME in names
        assert "gated_a" not in names and "gated_b" not in names

    def test_load_brings_schema_back(self, manager):
        loaded, unknown = manager.mark_loaded(["gated_a"])
        assert loaded == ["gated_a"] and unknown == []
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "gated_a" in names and "gated_b" not in names

    def test_unload_removes_again(self, manager):
        manager.mark_loaded(["gated_a", "gated_b"])
        dropped, _ = manager.mark_unloaded(["gated_a"])
        assert dropped == ["gated_a"]
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "gated_b" in names and "gated_a" not in names

    def test_non_gated_name_is_rejected(self, manager):
        loaded, unknown = manager.mark_loaded(["core_x", "nope"])
        assert loaded == [] and set(unknown) == {"core_x", "nope"}

    def test_all_tools_flag_keeps_selfcheck_possible(self, manager):
        names = [t["function"]["name"] for t in manager.list_openai_schemas(all_tools=True)]
        assert {"core_x", "gated_a", "gated_b"}.issubset(set(names))

    def test_schema_tokens_actually_drop(self, manager):
        """门控的意义就是省 token：证明清单比 schema 小一个量级。"""
        full = len(json.dumps(manager.list_openai_schemas(all_tools=True), ensure_ascii=False))
        lean = len(json.dumps(manager.list_openai_schemas(), ensure_ascii=False))
        hint = len(manager.tools_hint())
        assert lean < full * 0.6
        assert hint < lean, "一行式清单必须比 schema 更省"


class TestToolsHint:
    def test_hint_lists_gated_names_and_marks_loaded(self, manager):
        hint = manager.tools_hint()
        assert "gated_a" in hint and "gated_b" in hint and "tools load" in hint
        manager.mark_loaded(["gated_a"])
        assert "[已装载]" in manager.tools_hint()

    def test_hint_is_one_line_per_tool(self, manager):
        line = [l for l in manager.tools_hint().splitlines() if l.startswith("- ")][0]
        assert "第二句" not in line, "只要第一句，别把整段描述搬进提示词"

    def test_detail_view_is_for_the_tool_not_the_prompt(self, manager):
        assert "用 tools load" in manager.tools_hint(detail=True)
        assert manager.tools_hint(detail=True).count("\n") >= manager.tools_hint().count("\n")


class TestToolsTool:
    def _call(self, manager, **args):
        return manager.get_tool(META_TOOL_NAME).execute_json(args)

    def test_list_action(self, manager):
        out = self._call(manager, action="list")
        assert out.success and "gated_a" in out.output

    def test_load_action_then_schema_available(self, manager):
        out = self._call(manager, action="load", names="gated_a, gated_b")
        assert out.success and out.metadata["loaded"] == ["gated_a", "gated_b"]
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "gated_a" in names and "gated_b" in names

    def test_load_without_names_is_an_error(self, manager):
        out = self._call(manager, action="load")
        assert not out.success and "工具名" in out.error

    def test_unknown_action_is_an_error(self, manager):
        out = self._call(manager, action="explode")
        assert not out.success and "未知 action" in out.error

    def test_unload_action(self, manager):
        self._call(manager, action="load", names="gated_a")
        out = self._call(manager, action="unload", names="gated_a")
        assert out.success and out.metadata["loaded"] == ["gated_a"]

    def test_tool_is_core_and_low_risk(self, manager):
        tool = manager.get_tool(META_TOOL_NAME)
        assert tool.risk_level == "low" and tool.name not in GATED_TOOLS


class TestAutoload:
    def test_goal_keyword_loads_tool(self, manager, monkeypatch):
        monkeypatch.setattr("tools.tool_manager.GATED_KEYWORDS",
                            {"gated_a": ("做A事",), "gated_b": ("做B事",)})
        assert manager.autoload_for_text("帮我做A事") == ["gated_a"]
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "gated_a" in names and "gated_b" not in names

    def test_no_keyword_means_no_change(self, manager, monkeypatch):
        monkeypatch.setattr("tools.tool_manager.GATED_KEYWORDS", {"gated_a": ("做A事",)})
        assert manager.autoload_for_text("普通任务") == []
        names = [t["function"]["name"] for t in manager.list_openai_schemas()]
        assert "gated_a" not in names

    def test_already_loaded_is_not_reported_twice(self, manager, monkeypatch):
        monkeypatch.setattr("tools.tool_manager.GATED_KEYWORDS", {"gated_a": ("做A事",)})
        manager.mark_loaded(["gated_a"])
        assert manager.autoload_for_text("做A事") == []


class TestExecutorWiring:
    def test_fc_tools_includes_think_and_core_only(self, manager, monkeypatch):
        from agent.executor import Executor
        ex = Executor.__new__(Executor)
        ex.tool_manager = manager
        names = [t["function"]["name"] for t in ex._fc_tools()]
        assert "core_x" in names and "gated_a" not in names
        assert any(n in ("think", "think_tool") for n in names)

    def test_fc_tools_reflects_late_load(self, manager):
        from agent.executor import Executor
        ex = Executor.__new__(Executor)
        ex.tool_manager = manager
        manager.mark_loaded(["gated_a"])
        names = [t["function"]["name"] for t in ex._fc_tools()]
        assert "gated_a" in names, "装载后下一轮必须能拿到 schema"

    def test_hint_goes_into_prompt(self, manager):
        from agent.executor import Executor
        ex = Executor.__new__(Executor)
        ex.tool_manager = manager
        assert "gated_a" in ex._tools_hint()


class TestSchemaIsSerializable:
    """每个工具的 schema 必须是可 JSON 序列化的 `@property`。

    背景：base 的 `to_openai_schema()` 取的是 `self.schema` 本身（不是 `self.schema()`），
    所以子类必须写 `@property`；写成普通方法会把一个 bound method 塞进请求体，
    后果不是"这个工具不可用"，而是**每次带工具的模型调用都 TypeError**
    （实测把一个正在跑的会话整个打挂：Object of type method is not JSON serializable）。
    """

    def test_every_registered_tool_serializes(self):
        tm = ToolManager()
        broken = []
        for item in tm.list_openai_schemas(all_tools=True):
            try:
                json.dumps(item, ensure_ascii=False)
            except TypeError as exc:
                broken.append(f"{item.get('function', {}).get('name')}: {exc}")
        assert not broken, "这些工具的 schema 不能序列化（多半是漏了 @property）：" + "; ".join(broken)

    def test_schema_attribute_is_a_dict_not_a_method(self):
        tm = ToolManager()
        bad = [n for n, t in tm._tools.items() if not isinstance(getattr(t, "schema", None), dict)]
        assert not bad, f"schema 不是 dict（应为 @property）：{bad}"


class TestGatedAutoUnload:
    """门控工具不能只增不减：连续空闲超过阈值就自动卸下，长会话才真的省 token。"""

    def _names(self, manager):
        return [t["function"]["name"] for t in manager.list_openai_schemas()]

    def test_idle_gated_tool_is_evicted(self, manager):
        from tools.tool_manager import GATED_IDLE_TURNS
        manager.mark_loaded(["gated_a"])
        for _ in range(GATED_IDLE_TURNS - 1):          # 空闲到阈值前一轮：仍保留
            assert manager.begin_round() == []
        assert "gated_a" in self._names(manager)
        assert manager.begin_round() == ["gated_a"]    # 到阈值：卸下
        assert "gated_a" not in self._names(manager)

    def test_used_gated_tool_keeps_alive(self, manager):
        from tools.tool_manager import GATED_IDLE_TURNS
        manager.mark_loaded(["gated_a"])
        for _ in range(GATED_IDLE_TURNS + 3):
            manager.begin_round()
            manager.note_tool_used("gated_a")          # 每轮都用到 → 不该被卸
        assert "gated_a" in self._names(manager)

    def test_reload_restarts_the_clock(self, manager):
        manager.mark_loaded(["gated_a"])
        manager.mark_unloaded(["gated_a"])
        manager.mark_loaded(["gated_a"])               # 重新装载
        assert manager.begin_round() == []             # 不该因残留的旧计时立刻被卸
