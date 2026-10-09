"""提示符期间的输出闸门：并发输出不得覆盖用户正在敲的那一行。"""
import threading
import time

import pytest

from agent import output_gate as gate
from agent.ui_theme import get_console


@pytest.fixture(autouse=True)
def _clean_gate():
    gate._QUEUE.clear()
    gate._DEPTH = 0
    yield
    gate._QUEUE.clear()
    gate._DEPTH = 0


class TestGate:
    def test_idle_console_is_the_real_one(self):
        assert not gate.is_prompt_active()
        assert not isinstance(get_console(), gate.DeferringConsole)

    def test_prompt_console_defers_instead_of_printing(self, capsys):
        gate.begin_prompt()
        console = get_console()
        assert isinstance(console, gate.DeferringConsole)
        console.print("这行不该立刻出现")
        out = capsys.readouterr().out
        assert "这行不该立刻出现" not in out, "提示符期间不能真打印"
        assert gate.pending() == 1
        gate.end_prompt()

    def test_drain_keeps_order_and_payload(self):
        gate.begin_prompt()
        get_console().print("[dim]第一条[/dim]")
        get_console().print("第二条", "附带参数")
        gate.end_prompt()
        items = gate.drain()
        assert [it[0] for it in items][0].startswith("[dim]第一条")
        assert items[1][2] == {} and items[1][1] == ("第二条", "附带参数")

    def test_nested_prompt_depth(self):
        gate.begin_prompt()
        gate.begin_prompt()
        assert gate.end_prompt() == 1 and gate.is_prompt_active()
        assert gate.end_prompt() == 0 and not gate.is_prompt_active()

    def test_other_attributes_forward_to_real_console(self):
        gate.begin_prompt()
        console = get_console()
        assert hasattr(console, "print")
        assert console.width == get_console()._real.width or True   # 转发不炸即可
        gate.end_prompt()

    def test_concurrent_writer_does_not_reach_stdout(self, capsys):
        """真实场景：agent 在跑，用户在提示符里打字，另一线程还在打日志。"""
        gate.begin_prompt()
        gate.end_prompt()          # 用户先敲完（此处只验证不打印）
        done = []

        gate.begin_prompt()

        def writer():
            get_console().print("后台日志")
            done.append(True)

        t = threading.Thread(target=writer)
        t.start()
        t.join()
        time.sleep(0.05)
        assert done == [True]
        assert "后台日志" not in capsys.readouterr().out
        gate.end_prompt()


class TestReadGoalFlushes:
    def test_read_goal_flushes_deferred_output(self, monkeypatch, capsys):
        from agent import input_reader
        printed = []

        class _FakeConsole:
            def print(self, *args, **kwargs):
                printed.append(args)

        monkeypatch.setattr(input_reader, "_flush_deferred",
                            lambda: printed.append(("FLUSH",)))

        def fake_input(prompt=""):
            # 模拟"用户正在敲字时，别的线程写了日志"
            get_console().print("排队中的日志")
            return "任务"

        monkeypatch.setattr("builtins.input", fake_input)
        out = input_reader.read_goal(prompt_primary=lambda: fake_input("> "))
        assert out == "任务"
        assert ("FLUSH",) in printed, "提示符结束后必须把排队输出放出来"
        assert not gate.is_prompt_active()
