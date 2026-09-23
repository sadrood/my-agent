"""
批次3 回归：并发上限、同批兄弟调用的 reset 保护、重试预算与可中断睡眠。

背景（2026-09-17 审计）：
- `_run_tool_calls_parallel` 每个调用起一个线程、**无上限**，模型一轮发 N 个
  并行安全调用就 N 路并发（各自还能再拉子进程/HTTP）。
- 并行批里同工具的兄弟调用还在跑时，一个超时就会 `reset_tool`，把
  worker/队列置空并塞 None 哨兵 → 兄弟调用被跳过、`done.wait()` 永不返回。
- 轮级重试 6 次 × llm.py 内层 3 次 × 分钟边界 65s ≈ 单回合最坏十几分钟纯等待，
  且 `time.sleep` 整段睡，期间 stop_event 完全不响应。
"""
import threading
import time

import pytest

from agent.approval import ApprovalPolicy
from agent.executor import Executor
from tools.base import BaseTool, ToolResult
from tools.tool_manager import ToolManager


class SlowTool(BaseTool):
    """并行安全、可观测并发数的假工具。"""

    name = "slowtool"
    description = "并发观测"
    risk_level = "low"
    approval = "auto"
    min_sandbox_mode = "workspace-write"
    parallel_safe = True

    def __init__(self, hold: float = 0.4, rendezvous: int = 0,
                 rendezvous_timeout: float = 15.0):
        self.hold = hold
        #: >0 时：等到这么多兄弟**同时在飞**才继续（见 execute_json）
        self.rendezvous = rendezvous
        self.rendezvous_timeout = rendezvous_timeout
        self.live = 0
        self.peak = 0
        #: [(线程名, 进入时刻, 进入后在飞数)] —— 断言失败时打出来，便于判断是
        #: "第 N 个线程来得太晚"还是"根本没被派发"，不用再靠猜
        self.entries = []
        self.lock = threading.Lock()

    def execute(self, input_str):
        return self.execute_json({})

    def execute_json(self, arguments):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.entries.append((threading.current_thread().name, time.time(), self.live))
        try:
            # "并发真的跑起来了"不能靠 sleep 撞运气：机器有负载时第 N 个线程可能还没起，
            # 前 N-1 个就已经结束 → peak 少 1（实测在全量测试里抖过两次，独立跑则 8/8 通过）。
            # 这里让每个调用**等**到凑够 rendezvous 个同时在飞再往下走；上限真的更小的话，
            # 等待会在 deadline 后超时退出，peak 仍达不到目标值，断言照样能抓出来
            # （已用"把上限压到 3"负向验证过：peak=3 → 断言失败）。
            if self.rendezvous:
                deadline = time.time() + self.rendezvous_timeout
                while time.time() < deadline:
                    with self.lock:
                        if self.live >= self.rendezvous:
                            break
                    time.sleep(0.005)
            time.sleep(self.hold)
        finally:
            with self.lock:
                self.live -= 1
        return ToolResult(success=True, output="ok")

    def is_parallel_safe(self, arguments):
        return True


class NoopLLM:
    def chat(self, *a, **k):
        return ""

    def chat_with_tools(self, *a, **k):
        raise AssertionError("本测试不调用模型")


def make_executor(monkeypatch, tool):
    monkeypatch.setitem(__import__("config").TOOL_CONFIG, "tool_timeout", 5)
    tm = ToolManager()
    tm.register(tool)
    return Executor(llm=NoopLLM(), tool_manager=tm, approval_policy=ApprovalPolicy(
        mode="never", sandbox_mode="workspace-write", interactive=False))


class TestParallelCap:
    def test_fanout_is_capped(self, monkeypatch):
        """10 个并行调用必须受 max_parallel_tools 限制。"""
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "max_parallel_tools", 3)
        tool = SlowTool(hold=0.3)
        ex = make_executor(monkeypatch, tool)

        from models.llm import ToolCall
        tcs = [ToolCall(str(i), "slowtool", {}) for i in range(10)]
        t0 = time.time()
        results = ex._run_tool_calls_parallel(tcs, "目标")
        elapsed = time.time() - t0

        assert len(results) == 10
        assert all(r[0].success for r in results)
        assert tool.peak <= 3, f"并发超过上限: peak={tool.peak}"
        # 3 路并发跑 10 个 0.3s 任务：约 4 批 ≈ 1.2s；无上限则约 0.3s
        assert elapsed >= 0.9, f"疑似没有真正排队: {elapsed:.2f}s"

    def test_runs_parallel_when_under_cap(self, monkeypatch):
        from config import TOOL_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "max_parallel_tools", 4)
        # rendezvous=4：每个调用等到 4 个同时在飞才继续，去掉"谁先起谁先跑完"的时序运气
        tool = SlowTool(hold=0.05, rendezvous=4)
        ex = make_executor(monkeypatch, tool)

        from models.llm import ToolCall
        tcs = [ToolCall(str(i), "slowtool", {}) for i in range(4)]
        ex._run_tool_calls_parallel(tcs, "目标")
        assert tool.peak == 4, f"未并发执行: peak={tool.peak}; 进入时间线={tool.entries}"


class TestSiblingResetProtection:
    def test_refcount_counts_only_other_calls(self, monkeypatch):
        """判定必须是"**其它**调用在飞"，不能把调用者自己算进去。

        实测故障（2026-09-22 审计）：`_execute_one_tool_call` 在派发**之前**就先
        `_enter_tool_call` 给自己记了账，于是 `_dispatch_tool_call` 里读到的计数至少
        是 1 —— 判 `> 0` 恒为真，`reset_tool` 在生产路径上一次都不会被调用，而错误
        文案仍写着"已重置该工具状态"：browser 卡死一次后，坏掉的 playwright 连接
        原样留到后续每一轮，正是那段注释要根治的"一次卡死、次次卡死"。
        """
        tool = SlowTool()
        ex = make_executor(monkeypatch, tool)
        assert ex._sibling_calls_in_flight("slowtool") is False
        ex._enter_tool_call("slowtool")            # 只有"我自己"
        assert ex._sibling_calls_in_flight("slowtool") is False, "不能把自己算成兄弟"
        ex._enter_tool_call("slowtool")            # 这才是兄弟
        assert ex._sibling_calls_in_flight("slowtool") is True
        ex._leave_tool_call("slowtool")
        assert ex._sibling_calls_in_flight("slowtool") is False

    def test_timeout_resets_tool_on_real_entry(self, monkeypatch):
        """走**真实入口**时超时必须真的 reset_tool（旧实现恒不执行）。

        这条是上面那条的"行为版"：只看 `_sibling_calls_in_flight` 的返回值容易改对，
        真正要证明的是生产路径上 `reset_tool` 会被调到。
        """
        from config import TOOL_CONFIG
        from models.llm import ToolCall

        monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", 0.2)
        ex = make_executor(monkeypatch, SlowTool(hold=1.5))     # 必然超时
        monkeypatch.setitem(TOOL_CONFIG, "tool_timeout", 0.2)   # make_executor 会设成 5，这里再压一次
        resets = []
        monkeypatch.setattr(ex.tool_manager, "reset_tool",
                            lambda name: resets.append(name))

        ex._execute_one_tool_call(ToolCall("1", "slowtool", {}), "目标")
        assert resets == ["slowtool"], "超时后没重置工具（把自己当成兄弟了）"

    def test_reset_skipped_while_sibling_running(self, monkeypatch):
        """回归：兄弟调用在飞时不得 reset_tool（会把它排队的任务吞掉）。"""
        tool = SlowTool()
        ex = make_executor(monkeypatch, tool)
        resets = []
        monkeypatch.setattr(ex.tool_manager, "reset_tool",
                            lambda name: resets.append(name))
        ex._enter_tool_call("slowtool")          # 模拟同批兄弟在跑
        ex._enter_tool_call("slowtool")
        assert ex._sibling_calls_in_flight("slowtool") is True
        ex._leave_tool_call("slowtool")
        assert resets == []
        ex._leave_tool_call("slowtool")
        assert resets == []


class TestInterruptibleSleep:
    def test_returns_immediately_when_stopped(self):
        ev = threading.Event()
        ev.set()
        t0 = time.time()
        interrupted = Executor._sleep_interruptible(5.0, ev)
        assert interrupted is True
        assert time.time() - t0 < 0.5, "stop 置位后应立即返回，而不是睡满"

    def test_stop_during_sleep_is_noticed_quickly(self):
        ev = threading.Event()

        def stop_later():
            time.sleep(0.3)
            ev.set()

        threading.Thread(target=stop_later, daemon=True).start()
        t0 = time.time()
        assert Executor._sleep_interruptible(10.0, ev) is True
        assert time.time() - t0 < 2.0, "分片睡眠应及时察觉 stop"

    def test_sleeps_full_time_without_stop(self):
        t0 = time.time()
        assert Executor._sleep_interruptible(0.4, None) is False
        assert time.time() - t0 >= 0.35


class TestVideoGenParallel:
    def test_video_gen_declares_parallel_safe(self):
        """26 镜漫剧 80% 的时间卡在串行等视频，必须允许并发。"""
        from tools.video_gen import VideoGenTool
        assert VideoGenTool.parallel_safe is True

    def test_tool_manager_reports_parallel_safe(self):
        from tools.video_gen import VideoGenTool
        tm = ToolManager()
        tm.register(VideoGenTool())
        assert tm.is_parallel_safe("video_gen", {"command": "generate"}) is True
