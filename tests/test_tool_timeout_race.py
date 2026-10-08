"""工具超时竞态：僵尸线程退出前不得减 inflight、不得 reset 工具实例。

超时判定后工具线程仍在跑（可能还握着 playwright 连接/子进程）；此时立刻减计数并
reset，会让重置与新调用跟它争抢同一实例。inflight 的存活期 = 线程真正退出。
"""
import threading
import time
from types import SimpleNamespace

import pytest

from agent.approval import ApprovalPolicy
from agent.executor import Executor
from tools.base import BaseTool, ToolResult
from tools.tool_manager import ToolManager


class SlowTool(BaseTool):
    """跑得比超时久、但总会结束的工具（僵尸会自己退出）。"""

    name = "slow"
    description = "慢工具"
    risk_level = "low"
    approval = "auto"
    min_sandbox_mode = "workspace-write"

    def __init__(self, hold: float = 0.6):
        self.hold = hold
        self.finished = threading.Event()

    def execute(self, input_str=""):
        time.sleep(self.hold)
        self.finished.set()
        return ToolResult(success=True, output="done")

    def execute_json(self, arguments):
        return self.execute("")


class BoomTool(SlowTool):
    """快速抛异常（正常路径里"异常"分支）。"""

    name = "boom"

    def execute_json(self, arguments):
        raise RuntimeError("工具自己炸了")


class NoopLLM:
    def chat(self, *a, **k):
        return ""

    def chat_with_tools(self, *a, **k):
        return None


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _executor(monkeypatch, tool, timeout: float):
    from config import TOOL_CONFIG
    monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", timeout)
    tm = ToolManager()
    tm.register(tool)
    return Executor(llm=NoopLLM(), tool_manager=tm,
                    approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                                   interactive=False))


def _tc(name: str):
    return SimpleNamespace(name=name, arguments={})


class TestInflightSurvivesZombie:
    def test_inflight_lives_until_zombie_exits(self, monkeypatch):
        tool = SlowTool(hold=0.6)
        ex = _executor(monkeypatch, tool, timeout=0.2)
        result, blocked, _ = ex._execute_one_tool_call(_tc("slow"), "目标")

        assert result.success is False and "超时" in result.error
        assert blocked == ""
        assert ex._inflight.get("slow", 0) == 1, "僵尸线程还在跑，inflight 不能立刻归零"
        assert tool.finished.wait(3)
        assert _wait_until(lambda: ex._inflight.get("slow", 0) == 0), "僵尸退出后应回收计数"

    def test_reset_waits_for_zombie_and_zero_inflight(self, monkeypatch):
        tool = SlowTool(hold=0.5)
        ex = _executor(monkeypatch, tool, timeout=0.2)
        seen = []
        real_reset = ex.tool_manager.reset_tool

        def _reset(name):
            seen.append({"name": name, "zombie_done": tool.finished.is_set(),
                         "inflight": ex._inflight.get(name, 0)})
            return real_reset(name)

        monkeypatch.setattr(ex.tool_manager, "reset_tool", _reset)
        ex._execute_one_tool_call(_tc("slow"), "目标")

        assert seen == [], "僵尸还没退出就重置了 —— 这正是要修的竞态"
        assert tool.finished.wait(3)
        assert _wait_until(lambda: seen)
        assert seen[0]["name"] == "slow"
        assert seen[0]["zombie_done"] is True, "reset 必须晚于僵尸线程结束"
        assert seen[0]["inflight"] == 0, "reset 必须发生在 inflight 归零之后"

    def test_second_call_becomes_sibling_until_zombie_exits(self, monkeypatch):
        """僵尸期间的新调用是"兄弟在飞"：reset 必须让位（不能吞掉排队的调用）。"""
        tool = SlowTool(hold=0.5)
        ex = _executor(monkeypatch, tool, timeout=0.2)
        ex._execute_one_tool_call(_tc("slow"), "目标")
        ex._enter_tool_call("slow")              # 模拟僵尸期间进来的新调用
        assert ex._sibling_calls_in_flight("slow") is True
        time.sleep(0.8)                          # 僵尸退出，但新调用还在飞
        with ex._inflight_lock:
            assert ex._pending_reset, "重置意图应保留，等归零再执行"
        ex._leave_tool_call("slow")
        assert _wait_until(lambda: not ex._pending_reset), "归零后应由 watcher 完成重置"


class TestNormalPathUnchanged:
    def test_fast_call_returns_inflight_immediately_and_no_watcher(self, monkeypatch):
        tool = SlowTool(hold=0.0)
        ex = _executor(monkeypatch, tool, timeout=5)
        watched = []
        monkeypatch.setattr(Executor, "_start_zombie_watch",
                            lambda self, *a, **kw: watched.append(a[0]))

        result, _, _ = ex._execute_one_tool_call(_tc("slow"), "目标")

        assert result.success is True
        assert ex._inflight == {}, "正常路径计数必须立刻归零"
        assert watched == [], "正常路径不该起 watcher"

    def test_exception_path_returns_inflight_immediately(self, monkeypatch):
        """工具快速失败（ToolManager 把异常转成失败结果）→ 正常路径，无 watcher。"""
        ex = _executor(monkeypatch, BoomTool(hold=0.0), timeout=5)
        watched = []
        monkeypatch.setattr(Executor, "_start_zombie_watch",
                            lambda self, *a, **kw: watched.append(a[0]))

        result, _, _ = ex._execute_one_tool_call(_tc("boom"), "目标")

        assert result.success is False and "工具自己炸了" in result.error
        assert ex._inflight == {}
        assert watched == []

    def test_timeout_helper_reraises_and_reports_no_zombie(self):
        """_run_tool_with_timeout 的新契约：正常 → (结果, None)；异常 → 原样抛出。"""
        assert Executor._run_tool_with_timeout(lambda: "ok", 1) == ("ok", None)
        with pytest.raises(RuntimeError):
            Executor._run_tool_with_timeout(lambda: (_ for _ in ()).throw(RuntimeError("炸")), 1)


class TestGuardedPathTimeout:
    def test_call_tool_guarded_defers_reset_too(self, monkeypatch):
        """第二个超时点（call_tool_guarded）同样不能立刻重置。"""
        tool = SlowTool(hold=0.5)
        ex = _executor(monkeypatch, tool, timeout=0.2)
        seen = []
        real_reset = ex.tool_manager.reset_tool
        monkeypatch.setattr(ex.tool_manager, "reset_tool",
                            lambda name: seen.append(tool.finished.is_set()) or real_reset(name))

        result = ex.call_tool_guarded("slow", "")

        assert result.success is False
        assert "后台任务退出后" in result.error, "文案要说清何时重置"
        assert seen == [], "不能立刻重置"
        assert tool.finished.wait(3)
        assert _wait_until(lambda: seen)
        assert seen[0] is True
