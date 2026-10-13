"""`ask_user` 工具：中途向用户提问。

两条最要紧的保证：
- **非交互（无终端 / interactive=false）时绝不阻塞** —— 必须立刻回一句"用户不可达"给模型，
  否则无人值守的运行会卡死在一个永远等不到人的 input() 上；
- 问的时候借的是主循环那条通道（`input_reader.read_goal`），所以输出闸门会开、排队输出会在
  回答后放行（不然别的线程的输出会把用户正在敲的字覆盖掉）。
"""
import io
import sys

import pytest

from agent import output_gate
from tools.ask import AskTool, _interactive_ok


def _tool() -> AskTool:
    return AskTool()


class TestNonInteractiveNeverBlocks:
    def test_no_tty_returns_unreachable(self, monkeypatch):
        monkeypatch.setattr("tools.ask._interactive_ok", lambda: False)
        # 故意让 read_goal 一调用就炸：真阻塞了这条测试就会以别的方式失败
        monkeypatch.setattr("agent.input_reader.read_goal",
                            lambda **kw: pytest.fail("非交互时不该去读输入"))
        r = _tool().execute_json({"question": "用哪个方案？"})
        assert r.success is False
        assert "不可达" in (r.error or "")
        assert "自行" in (r.error or "") or "假设" in (r.error or "")

    def test_interactive_ok_false_without_tty(self, monkeypatch):
        class _NotATty(io.StringIO):
            def isatty(self):
                return False

        monkeypatch.setattr(sys, "stdin", _NotATty())
        assert _interactive_ok() is False

    def test_interactive_ok_honors_config_switch(self, monkeypatch):
        class _ATty(io.StringIO):
            def isatty(self):
                return True

        import config
        monkeypatch.setattr(sys, "stdin", _ATty())
        monkeypatch.setitem(config.APPROVAL_CONFIG, "interactive", False)
        assert _interactive_ok() is False, "关掉交互的进程里不该弹问题"
        monkeypatch.setitem(config.APPROVAL_CONFIG, "interactive", True)
        assert _interactive_ok() is True


class TestAnswerHandling:
    @pytest.fixture(autouse=True)
    def _interactive(self, monkeypatch):
        monkeypatch.setattr("tools.ask._interactive_ok", lambda: True)

    def _answer(self, monkeypatch, text: str, gate_probe: list = None):
        # 只替换"真正读输入"的那一层，保留 read_goal 的闸门逻辑 —— 否则测不到闸门开没开
        def fake_inner(prompt_primary=None, prompt_continuation=None, *a, **kw):
            if gate_probe is not None:
                gate_probe.append(output_gate.is_prompt_active())
            return text
        monkeypatch.setattr("agent.input_reader._read_goal_inner", fake_inner)

    def test_free_text_answer(self, monkeypatch):
        self._answer(monkeypatch, "用 B 方案，别动线上配置")
        r = _tool().execute_json({"question": "用哪个方案？"})
        assert r.success and "用户回答：用 B 方案" in r.output

    def test_number_picks_option_text(self, monkeypatch):
        self._answer(monkeypatch, "2")
        r = _tool().execute_json({"question": "用哪个？", "options": ["改配置", "改代码"]})
        assert r.success and "用户选择：改代码（第 2 项）" in r.output

    def test_out_of_range_number_is_kept_as_text(self, monkeypatch):
        self._answer(monkeypatch, "9")
        r = _tool().execute_json({"question": "用哪个？", "options": ["甲", "乙"]})
        assert "用户回答：9" in r.output

    def test_empty_answer_uses_default(self, monkeypatch):
        self._answer(monkeypatch, "")
        r = _tool().execute_json(
            {"question": "用哪个？", "default": "保持不变"})
        assert "用户回答：保持不变" in r.output

    def test_empty_without_default_says_undecided(self, monkeypatch):
        self._answer(monkeypatch, "   ")
        r = _tool().execute_json({"question": "用哪个？"})
        assert "未作答" in r.output and "自行决定" in r.output

    def test_output_gate_is_open_while_asking(self, monkeypatch):
        probe = []
        self._answer(monkeypatch, "好", gate_probe=probe)
        _tool().execute_json({"question": "行不行？"})
        assert probe == [True], "提问期间要开输出闸门，否则用户敲的字会被并发输出覆盖"
        assert output_gate.is_prompt_active() is False, "问完必须关掉闸门（可重入计数要平衡）"

    def test_missing_question_is_rejected(self):
        r = _tool().execute_json({})
        assert r.success is False and "question" in (r.error or "")

    def test_string_entry_point_works(self, monkeypatch):
        self._answer(monkeypatch, "可以")
        r = _tool().execute("这样改行不行？")
        assert r.success and "用户回答：可以" in r.output


class TestToolMetadata:
    def test_not_parallel_safe(self):
        """等人输入的工具不能和其他工具并跑（问题会和别的输出/输入缠在一起）。"""
        assert _tool().parallel_safe is False

    def test_low_risk_and_auto_approval(self):
        t = _tool()
        assert t.risk_level == "low" and t.approval == "auto"
        assert "question" in t.schema["properties"]


class TestGuideMatchesExistingPolicy:
    """工具引导必须与"完成度闸门"的既有政策一致 —— 那三种情况之外不许问。

    背景：仓库此前专门治过"做一半就回来问要不要继续"（提示词 + 独立监管者两道保险），
    所以新加一个"能问"的工具时，引导必须收着写，别把口径放大回去。
    """

    def test_guide_is_restrictive(self):
        from models.prompts import ASK_USER_PROMPT_GUIDE as guide
        assert "默认不要问" in guide, "引导要以'别问'为默认，否则又会退回做一半就问"
        for case in ("本人操作", "不可逆", "先请示"):
            assert case in guide, f"必须与既有三种情况对齐：缺 {case}"

    def test_registered_in_default_toolset(self):
        from tools.tool_manager import ToolManager
        assert ToolManager().get_tool("ask_user") is not None
