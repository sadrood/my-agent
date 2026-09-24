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

    def test_infinite_mode_never_exhausts(self):
        """"让 agent 做完任务再结束"：hard_cap=0 时永远放行下一轮。

        旧行为是固定 80/120 轮封顶——复杂任务跑到一半被砍，用户原话
        "任务没完成轮数耗尽，导致任务中断"。
        """
        b = LoopBudget(base=2, extend=0, stall_limit=5, hard_cap=0)
        for _ in range(200):
            assert b.allow_next() is True
            b.observe(TurnOutcome(tool_calls=1, failed=1))    # 失败的**新**尝试
        assert b.stop_reason == ""
        assert b.near_limit() is False, "无限模式下不存在'接近上限'"

    def test_infinite_mode_still_stops_when_spinning(self):
        """没有上限不等于没有刹车：原地打转仍然要停（否则会一直烧钱）。"""
        b = LoopBudget(base=2, extend=0, stall_limit=3, hard_cap=0)
        for _ in range(3):
            b.observe(TurnOutcome(repeated=True))
        assert b.allow_next() is False
        assert b.stop_reason == "stalled"

    def test_infinite_mode_summary_says_unlimited(self):
        b = LoopBudget(base=2, extend=0, stall_limit=5, hard_cap=0)
        b.observe(TurnOutcome(succeeded=1))
        assert b.summary()["hard_cap"] == 0
        assert "安全网上限 无" in b.stop_message()

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
class TestStagnationWarning:
    """连续无进展（包括"换了新做法但失败"）的可见性预警。

    背景：无限模式（hard_cap=0）下，只有"原样重复 / 空回复"能触发 stalled 停止；
    "换了新做法但失败"的轮次（productive=False 且 spinning=False）永远到不了上限，
    会无限跑下去。stagnation 计数器让这件事可见——达到阈值响一次，不自动停。
    """

    def test_new_approaches_that_fail_accumulate_stagnation(self):
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0,
                       stagnation_limit=3)
        # 每轮换新做法但失败：不打转（spinning=False）、不续期（productive=False）
        for _ in range(2):
            b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
        assert b.stagnation_turns == 2
        assert b.spinning_turns == 0, "新做法失败不是打转"
        assert b.stagnation_alarm() is False, "还没到阈值"

    def test_alarm_fires_exactly_once_at_threshold(self):
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0,
                       stagnation_limit=3)
        alarms = []
        for _ in range(6):
            b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
            alarms.append(b.stagnation_alarm())
        assert alarms == [False, False, True, False, False, False], \
            "只在达到阈值的那个轮次响一次，不刷屏"

    def test_progress_resets_stagnation_counter(self):
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0,
                       stagnation_limit=3)
        b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
        b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
        b.observe(TurnOutcome(succeeded=1))          # 谷歌一次成功 → 清零
        assert b.stagnation_turns == 0
        assert b.stagnation_alarm() is False

    def test_spinning_also_counts_as_stagnation(self):
        """打转当然也是"无进展"，两者同时累计。"""
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0,
                       stagnation_limit=2)
        b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=True))
        assert b.stagnation_turns == 1 and b.spinning_turns == 1
        b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=True))
        assert b.stagnation_alarm() is True

    def test_disabled_by_default_threshold_zero(self):
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0)
        for _ in range(20):
            b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
        assert b.stagnation_alarm() is False, "阈值 0 = 关闭"

    def test_from_config_reads_stagnation_knob(self):
        b = LoopBudget.from_config(80, config={
            "loop_hard_cap": 0, "loop_stagnation_warn": 7})
        assert b.stagnation_limit == 7
        assert b.hard_cap == 0, "显式 0=无限仍生效"

    def test_infinite_mode_painful_case_now_visible_via_summary(self):
        """病态场景复现：5000 轮换新做法失败在无限模式下永不停止，但摘要可见。"""
        b = LoopBudget(base=30, extend=10, stall_limit=5, hard_cap=0,
                       stagnation_limit=10)
        for _ in range(5000):
            if not b.allow_next():
                break
            b.observe(TurnOutcome(tool_calls=1, failed=1, repeated=False))
        assert b.stop_reason == ""          # 仍不自动停（尊重"做完再结束"）
        assert b.stagnation_turns == 5000   # 但"卡了多久"是可见的
        assert b.spinning_turns == 0