"""未完成交接（A）与续跑指令（B/C）的单元测试（纯函数，不联网）。"""
from agent.agent import INCOMPLETE_MARKERS, compose_handoff


def test_incomplete_markers_cover_main_cases():
    cases = [
        "已达到任务最大操作轮数（80）。任务可能比预期复杂…",
        "任务已停止",
        "已停止（用户中断）",
        "任务未完成，已完成的操作已记录",
        "中断于第 37 轮",
    ]
    for c in cases:
        assert any(k in c for k in INCOMPLETE_MARKERS), c
    # 正常完成不应误判
    assert not any(k in "任务完成，全部通过，612 passed" for k in INCOMPLETE_MARKERS)


def test_compose_handoff_structure_and_rule():
    text = compose_handoff("重构 tools 模块", "达到任务最大操作轮数（80）",
                           "原始收尾：已完成的操作已记录",
                           done_hint="修改了 tools/base.py")
    assert "未完成" in text
    assert "只执行未完成部分" in text and "不要重新执行已完成" in text
    assert "修改了 tools/base.py" in text
    assert "重构 tools 模块" in text


def test_resume_budget_keys_present():
    from config import SESSION_CONFIG
    assert int(SESSION_CONFIG.get("resume_context_messages", 0)) >= 30
    assert int(SESSION_CONFIG.get("resume_context_chars", 0)) >= 500
