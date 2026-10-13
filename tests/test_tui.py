"""TUI 骨架测试：三区布局、常驻输入框、事件 → 行的映射。

用 Textual 的 `run_test()` 无头驱动（不需要真终端），所以 CI/管道里也能跑。
异步用 asyncio.run 包一层同步用例：本仓库没装 pytest-asyncio（只有 anyio）。
"""
import asyncio
import html
import re

import pytest

from agent.tui import AgentTUI, _compact_args

#: SVG 导出的空格是不换行空格，断言前要归一（写成转义，别在源码里留不可见字符）
NBSP = "\u00a0"


def _run(coro_fn, **kw):
    return asyncio.run(coro_fn(**kw))


def screen_text(app) -> str:
    """把 Textual 当前屏幕导出成文本（走 export_screenshot 的 SVG）。

    比断言 `app.lines`（应用自己的行镜像）强：它验证的是**真的画在屏幕上**的内容。
    """
    svg = app.export_screenshot()
    frags = []
    for m in re.finditer(r'<text[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"[^>]*>(.*?)</text>',
                         svg, re.S):
        x, y, body = float(m.group(1)), float(m.group(2)), m.group(3)
        frags.append((y, x, html.unescape(body).replace(NBSP, " ")))
    # 同一行的多个样式段 y 会差零点几：按容差归组，否则一行会被拆成好几行
    rows, current, base_y = [], [], None
    for y, x, text in sorted(frags):
        if base_y is None or abs(y - base_y) <= 1.5:
            if base_y is None:
                base_y = y
            current.append((x, text))
        else:
            rows.append(current)
            current, base_y = [(x, text)], y
    if current:
        rows.append(current)
    return "\n".join("".join(t for _, t in sorted(items)) for items in rows)


class TestLogMapping:
    def test_tool_call_and_result_lines(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                app.add_tool_call("terminal", {"command": "pytest -q"})
                app.add_tool_result(True, "120 passed")
                await pilot.pause()
                return screen_text(app)
        text = _run(body)
        assert "⏺ terminal(command=pytest -q)" in text
        assert "⎿ 120 passed" in text

    def test_truncation_points_to_transcript(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                app.add_tool_result(True, "\n".join(f"行{i}" for i in range(9)))
                await pilot.pause()
                return screen_text(app)
        assert "/transcript" in _run(body)

    def test_todo_list_uses_tool_glyphs(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                app.add_todo([{"id": "1", "title": "甲", "status": "done"},
                              {"id": "2", "title": "乙", "status": "in_progress"},
                              {"id": "3", "title": "丙", "status": "todo"}])
                await pilot.pause()
                return screen_text(app)
        joined = _run(body)
        assert "待办 1/3" in joined and "✓" in joined and "▶" in joined and "○" in joined

    def test_empty_todo_says_cleared(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                app.add_todo([])
                await pilot.pause()
                return screen_text(app)
        assert "已清空" in _run(body)


class TestPersistentPrompt:
    """TUI 相对行式渲染的核心好处：任务跑着，输入框还在（只是置灰）。"""

    def test_prompt_stays_mounted_while_busy(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                prompt = app.query_one("#prompt")
                assert prompt.display is True
                app.set_busy(True)
                await pilot.pause()
                busy = (prompt.disabled, prompt.placeholder)
                app.set_busy(False)
                await pilot.pause()
                idle = (prompt.disabled, prompt.placeholder)
                still_there = prompt.display
                return busy, idle, still_there
        busy, idle, still_there = _run(body)
        assert busy[0] is True and "运行中" in busy[1]
        assert idle[0] is False and "输入目标" in idle[1]
        assert still_there is True, "任务运行期间输入框不该从屏幕上消失"

    def test_submit_calls_back_with_goal(self):
        async def body():
            seen = []
            app = AgentTUI(on_submit=seen.append)
            async with app.run_test(size=(80, 20)) as pilot:
                prompt = app.query_one("#prompt")
                prompt.focus()
                prompt.value = "帮我看下日志"
                await pilot.press("enter")
                await pilot.pause()
                return seen, screen_text(app)
        seen, screen = _run(body)
        assert seen == ["帮我看下日志"]
        assert "帮我看下日志" in screen, "提交后应回显在输出区"

    def test_clear_log_resets_screen_and_mirror(self):
        async def body():
            app = AgentTUI()
            async with app.run_test(size=(80, 20)) as pilot:
                app.write("一")
                app.write("二")
                await pilot.pause()
                app.action_clear_log()
                await pilot.pause()
                return app.lines, screen_text(app)
        lines, screen = _run(body)
        assert lines == [] and "一" not in screen


class TestStatusAndCompactArgs:
    def test_status_line_carries_key_hints(self):
        async def body():
            app = AgentTUI(status="deepseek-flash · never")
            async with app.run_test(size=(110, 20)) as pilot:
                await pilot.pause()
                return screen_text(app)
        text = _run(body)
        assert "deepseek-flash" in text and "ctrl+c" in text, \
            "状态栏要常驻显示怎么退出/清屏（同类 CLI 的观感就靠这条）"

    def test_status_update_keeps_hints(self):
        async def body():
            app = AgentTUI(status="就绪")
            async with app.run_test(size=(110, 20)) as pilot:
                app.set_status("↑3.2k ↓480")
                await pilot.pause()
                return screen_text(app)
        text = _run(body)
        assert "↑3.2k" in text and "ctrl+c" in text

    def test_compact_args_truncates_long_values(self):
        out = _compact_args({"code": "x" * 200}, limit=40)
        assert len(out) <= 41 and out.endswith("…")
