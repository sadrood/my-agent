"""
子进程终止联动测试：停止信号必须能真正杀掉正在运行的前台命令。

覆盖三层：
1. TerminalTool：stop_event 置位 / terminate_current() 直接调用 → 慢命令快速退出
2. ToolManager：bind_stop_event 注入 + cancel_active_tools 兜底
3. Executor：循环执行慢命令期间置位 stop_event → 整体快速返回 stopped
"""
import threading
import time

from agent.executor import Executor
from agent.approval import ApprovalPolicy
from models.llm import LLMToolResponse, ToolCall
from tools.terminal import TerminalTool
from tools.tool_manager import ToolManager

# 跨平台慢命令：约 30 秒，正常绝不会在测试时限内自己结束
SLOW_CMD = 'python -c "import time; time.sleep(30)"'


class TestTerminalTermination:
    def test_stop_event_cancels_slow_command(self):
        tool = TerminalTool()
        stop = threading.Event()
        tool.set_stop_event(stop)

        box = {}

        def _run():
            box["result"] = tool.execute_json({"command": SLOW_CMD})

        t = threading.Thread(target=_run, daemon=True)
        t0 = time.time()
        t.start()
        time.sleep(0.8)          # 让命令真正跑起来
        stop.set()
        t.join(timeout=10)
        elapsed = time.time() - t0

        assert not t.is_alive(), "停止信号置位后命令仍未退出"
        assert elapsed < 10, f"终止耗时过长: {elapsed:.1f}s"
        result = box["result"]
        assert result.success is False
        assert "停止" in (result.error or "")
        # 进程引用已清理
        assert tool._active_proc is None

    def test_terminate_current_kills_process_tree(self):
        tool = TerminalTool()
        box = {}

        def _run():
            box["result"] = tool.execute_json({"command": SLOW_CMD})

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # 等到进程注册为活动进程
        deadline = time.time() + 5
        while tool._active_proc is None and time.time() < deadline:
            time.sleep(0.05)
        assert tool._active_proc is not None, "慢命令未启动"

        t0 = time.time()
        killed = tool.terminate_current()
        t.join(timeout=10)
        elapsed = time.time() - t0

        assert killed is True
        assert not t.is_alive(), "terminate_current 后命令线程仍未退出"
        assert elapsed < 10
        assert box["result"].success is False

    def test_terminate_current_without_active_proc(self):
        tool = TerminalTool()
        assert tool.terminate_current() is False

    def test_normal_command_unaffected(self):
        """不带停止信号时，普通命令行为不变。"""
        tool = TerminalTool()
        result = tool.execute_json({"command": "echo hello-termination"})
        assert result.success is True
        assert "hello-termination" in result.output


class TestToolManagerCancel:
    def test_bind_and_cancel(self):
        tm = ToolManager()
        term = tm.get_tool("terminal")
        stop = threading.Event()
        tm.bind_stop_event(stop)
        assert term._stop_event is stop

        box = {}

        def _run():
            box["result"] = tm.execute_json("terminal", {"command": SLOW_CMD})

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        deadline = time.time() + 5
        while term._active_proc is None and time.time() < deadline:
            time.sleep(0.05)
        assert term._active_proc is not None

        t0 = time.time()
        tm.cancel_active_tools()   # 不依赖 stop_event，直接兜底强杀
        t.join(timeout=10)
        assert not t.is_alive()
        assert time.time() - t0 < 10
        assert box["result"].success is False


class _FakeLLM:
    def __init__(self, script):
        self.script = list(script)

    def chat_with_tools(self, messages, tools, **kwargs):
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        return self.script.pop(0)


class TestExecutorStopLinkage:
    def test_stop_event_during_slow_tool(self):
        """执行器跑到慢命令时置位 stop_event：命令被杀、循环快速 stopped 退出。"""
        llm = _FakeLLM([
            LLMToolResponse(content="", tool_calls=[
                ToolCall("1", "terminal", {"command": SLOW_CMD}),
            ]),
            LLMToolResponse(content="不该走到这里"),
        ])
        tm = ToolManager()
        ex = Executor(
            tool_manager=tm,
            llm=llm,
            approval_policy=ApprovalPolicy(mode="never", sandbox_mode="workspace-write",
                                           interactive=False),
            guardian=None,
            rollout=None,
            instructions_text="",
            max_step_ops=10,
            llm_retry_delay=0,
        )
        stop = threading.Event()
        box = {}

        def _run():
            box["result"] = ex.execute_goal_loop(
                goal="跑一个慢命令", system_prompt="测试", stop_event=stop)

        t = threading.Thread(target=_run, daemon=True)
        t0 = time.time()
        t.start()
        # 等 terminal 的前台进程真的起来了再按停止
        term = tm.get_tool("terminal")
        deadline = time.time() + 10
        while term._active_proc is None and time.time() < deadline:
            time.sleep(0.05)
        assert term._active_proc is not None, "执行器未启动慢命令"
        stop.set()
        t.join(timeout=15)
        elapsed = time.time() - t0

        assert not t.is_alive(), "stop_event 置位后执行器未及时退出"
        assert elapsed < 15, f"停止联动耗时过长: {elapsed:.1f}s（正常应 2~3 秒内）"
        result = box["result"]
        assert result.get("stopped") is True
        # 慢命令的工具结果应标记为失败（被终止），而不是假成功
        term_call = [c for c in result["tool_calls"] if c["name"] == "terminal"]
        assert term_call and term_call[0]["success"] is False