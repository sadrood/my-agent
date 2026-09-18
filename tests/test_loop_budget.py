# -*- coding: utf-8 -*-
"""动态轮数预算（agent/loop_budget.py）的单元测试。

背景（用户痛点原话）："总是任务没完成轮数耗尽，导致任务中断"。
原实现是 `for turn in range(max_ops)`——一个常数同时要服务简单任务和复杂任务。
这里钉住新语义：**有进展就续期，原地打转才提前停**。
"""
from types import SimpleNamespace

from agent.loop_budget import LoopBudget, TurnOutcome, tool_signature


class TestTurnOutcome:
    def test_successful_new_call_is_progress(self):
        assert TurnOutcome(tool_calls=1, succeeded=1).productive is True

    def test_file_change_is_progress(self):
        assert TurnOutcome(files_changed=2).productive is True

    def test_identical_repeat_is_not_progress_even_if_it_touched_files(self):
        """"反复写同一内容"是典型的假装干活：改了文件也不算进展。"""
        assert TurnOutcome(files_changed=3, repeated=True).productive is False

    def test_failed_but_new_attempt_is_neither_progress_nor_spinning(self):
        """调试任务里"失败→换个方式再试"是正常推进，不能判成打转。"""
        o = TurnOutcome(tool_calls=1, failed=1)
        assert o.productive is False
        assert o.spinning is False

    def test_repeat_and_empty_are_spinning(self):
        assert TurnOutcome(repeated=True).spinning is True
        assert TurnOutcome(empty_response=True).spinning is True

    def test_describe_is_human_readable(self):
        text = TurnOutcome(tool_calls=2, succeeded=1, failed=1).describe()
        assert "成功 1" in text and "失败 1" in text


class TestToolSignature:
    def test_same_calls_same_signature(self):
        a = [SimpleNamespace(name="python", arguments={"code": "print(1)", "x": 1})]
        b = [SimpleNamespace(name="python", arguments={"x": 1, "code": "print(1)"})]
        assert tool_signature(a) == tool_signature(b), "参数顺序不同不算换了新办法"

    def test_different_calls_differ(self):
        a = [SimpleNamespace(name="python", arguments={"code": "print(1)"})]
        b = [SimpleNamespace(name="python", arguments={"code": "print(2)"})]
        assert tool_signature(a) != tool_signature(b)

    def test_empty_and_dict_forms_are_safe(self):
        assert tool_signature([]) == ()
        assert tool_signature([{"name": "x", "arguments": None}])


class TestExtension:
    def test_progress_extends_the_budget(self):
        b = LoopBudget(base=3, extend=2, stall_limit=9, hard_cap=50)
        assert b.limit == 3
        b.observe(TurnOutcome(succeeded=1))
        assert b.limit == 5 and b.extensions == 1
        b.observe(TurnOutcome(succeeded=1))
        assert b.limit == 7 and b.extensions == 2

    def test_extension_respects_hard_cap(self):
        b = LoopBudget(base=3, extend=10, stall_limit=9, hard_cap=5)
        for _ in range(5):
            b.observe(TurnOutcome(succeeded=1))
        assert b.limit == 5, "安全网不能被续期突破"

    def test_failed_new_attempts_do_not_extend(self):
        b = LoopBudget(base=2, extend=5, stall_limit=9, hard_cap=50)
        b.observe(TurnOutcome(tool_calls=1, failed=1))
        assert b.limit == 2, "失败不续期（但也不算打转）"
        assert b.spinning_turns == 0

    def test_hard_cap_zero_means_no_ceiling(self):
        b = LoopBudget(base=2, extend=3, stall_limit=9, hard_cap=0)
        for _ in range(10):
            b.observe(TurnOutcome(succeeded=1))
        assert b.limit == 2 + 30, "0 = 不设上限，只由进展/停止条件决定"

    def test_hard_cap_below_base_still_wins(self):
        """显式 max_ops=5 时必须恰好 5 轮——不能被起步轮数顶掉。"""
        assert LoopBudget.fixed(5).limit == 5


class TestStallDetection:
    def test_repeated_calls_stop_early(self):
        b = LoopBudget(base=100, extend=5, stall_limit=3, hard_cap=100)
        for _ in range(3):
            b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=True))
        assert b.spinning_turns == 3
        assert b.allow_next() is False
        assert b.stop_reason == "stalled"

    def test_progress_resets_the_spinning_counter(self):
        b = LoopBudget(base=100, extend=5, stall_limit=3, hard_cap=100)
        b.observe(TurnOutcome(repeated=True))
        b.observe(TurnOutcome(repeated=True))
        assert b.spinning_turns == 2
        b.observe(TurnOutcome(succeeded=1))          # 又往前走了
        assert b.spinning_turns == 0
        assert b.allow_next() is True

    def test_exhausted_takes_precedence_only_after_stall_check(self):
        b = LoopBudget(base=2, extend=0, stall_limit=9, hard_cap=2)
        b.observe(TurnOutcome(succeeded=1))
        b.observe(TurnOutcome(succeeded=1))
        assert b.allow_next() is False
        assert b.stop_reason == "exhausted"

    def test_summary_reports_both_reasons(self):
        b = LoopBudget(base=4, extend=1, stall_limit=2, hard_cap=8)
        b.observe(TurnOutcome(repeated=True))
        b.observe(TurnOutcome(repeated=True))
        b.allow_next()
        s = b.summary()
        assert s["stop_reason"] == "stalled"
        assert s["spinning_turns"] == 2 and s["used"] == 2 and s["limit"] == 4


class TestStopMessages:
    def test_stalled_message_says_it_is_not_unsolvable(self):
        b = LoopBudget(base=10, extend=1, stall_limit=1, hard_cap=10)
        b.observe(TurnOutcome(repeated=True))
        b.allow_next()
        msg = b.stop_message()
        assert "打转" in msg
        assert "不代表任务无解" in msg, "要避免让用户/模型以为任务做不了"
        assert "新的" in msg, "要引导给出新做法，而不是原地重试"

    def test_exhausted_message_keeps_backward_compatible_marker(self):
        """agent.py 的 INCOMPLETE_MARKERS 靠这句话识别"没做完"，不能改掉。"""
        b = LoopBudget(base=1, extend=0, stall_limit=9, hard_cap=1)
        b.observe(TurnOutcome(succeeded=1))
        b.allow_next()
        msg = b.stop_message()
        assert "已达到任务最大操作轮数" in msg
        assert "1 轮" in msg


class TestFromConfig:
    def test_reads_new_knobs(self):
        cfg = {"loop_base_turns": 7, "loop_extend_per_progress": 3,
               "loop_stall_limit": 4, "loop_hard_cap": 99}
        b = LoopBudget.from_config(80, config=cfg)
        assert (b.base, b.extend, b.stall_limit, b.hard_cap) == (7, 3, 4, 99)

    def test_falls_back_to_legacy_max_loop_ops_as_hard_cap(self):
        """没配新旋钮的机器：硬上限取旧的 max_loop_ops → 行为不劣化于升级前。"""
        b = LoopBudget.from_config(120, config={})
        assert b.hard_cap == 120

    def test_explicit_unlimited_hard_cap(self):
        b = LoopBudget.from_config(120, config={"loop_hard_cap": 0})
        assert b.hard_cap == 0
