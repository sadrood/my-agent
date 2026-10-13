"""
UI 主题冒烟测试：主流终端风格渲染函数不抛异常且符号正确。
"""
import io

import pytest
from rich.console import Console

import agent.ui_theme as ui


@pytest.fixture
def buf(monkeypatch):
    b = io.StringIO()
    console = Console(file=b, force_terminal=False, highlight=False)
    # 必须走 monkeypatch：直接赋值不会还原，会把 ui.get_console 永久指向这个 StringIO
    # console，后面按字母序跑的 test_status_bar 就抓不到 capsys 输出了。
    monkeypatch.setattr(ui, "get_console", lambda use_rich=True: console)
    return b


class TestMinimalRenderers:
    def test_header_has_no_panel_markers(self, buf):
        ui.print_header(title="", goal="测试目标", use_rich=True,
                        status_items=[("模式", "单循环"), ("审批", "never")])
        out = buf.getvalue()
        assert "目标" in out and "测试目标" in out
        assert "┌" not in out and "┐" not in out      # 无重型边框
        assert "单循环" in out and "never" in out

    def test_header_status_text_without_emoji(self, buf):
        ui.print_header(title="", goal="g", use_rich=True,
                        status_text="单循环 · 审批 never · 沙箱 workspace-write")
        out = buf.getvalue()
        assert "单循环 · 审批 never · 沙箱 workspace-write" in out
        assert "🧭" not in out and "🛡" not in out and "🏖" not in out

    def test_tool_call_and_result(self, buf):
        ui.print_tool_call("python", {"code": "print(7*8)"})
        ui.print_tool_result(True, "56")
        out = buf.getvalue()
        assert "⏺" in out and "python" in out
        assert 'code: "print(7*8)"' in out
        assert "⎿" in out and "56" in out

    def test_tool_result_failure_multiline(self, buf):
        ui.print_tool_result(False, "第一行\n第二行\n第三行\n第四行\n第五行")
        out = buf.getvalue()
        assert "⎿" in out
        assert "共 5 行" in out

    def test_tool_result_truncation(self, buf):
        ui.print_tool_result(True, "x" * 2000)
        out = buf.getvalue()
        assert "…" in out

    def test_final_result_minimal(self, buf):
        ui.print_final_result("7 × 8 = 56")
        out = buf.getvalue()
        assert "7 × 8 = 56" in out
        assert "┌" not in out                     # 无 Panel 边框
        assert "小悟" in out

    def test_info_styles(self, buf):
        ui.print_info("普通信息", style="info")
        ui.print_info("成功", style="success")
        ui.print_info("警告", style="warning")
        ui.print_info("错误", style="error")
        out = buf.getvalue()
        assert "普通信息" in out and "成功" in out

    def test_welcome_minimal(self, buf):
        ui.print_welcome()
        out = buf.getvalue()
        assert "my_agent" in out
        assert "┌" not in out

    def test_welcome_panel_with_info(self, buf):
        # 提供 info 时渲染单面板启动框（状态 + 快速开始 + 模型/key）
        ui.print_welcome(info={
            "model": "deepseek-v4-flash",
            "base_url": "http://127.0.0.1:3000/v1",
            "workspace": "D:\\aaa\\work",
            "session_id": "conv-test",
            "sandbox": "workspace-write",
            "approval": "on-failure",
            "api_key": "sk-12…",
        })
        out = buf.getvalue()
        assert "deepseek-v4-flash" in out
        assert "快速开始" in out            # 面板路径独有
        assert "Status: Ready" in out
        assert "Session: conv-test" in out
        assert "/help" in out
        # 续行提示里的反斜杠不能被 rich 的 \[ 转义吃掉
        assert "行尾 \\" in out
        assert "[/white]" not in out

    def test_welcome_panel_url_object_safe(self, buf):
        # LLM 客户端的 base_url 是 httpx.URL 对象（非 str）——面板必须容错
        from httpx import URL
        ui.print_welcome(info={
            "model": "deepseek-v4-flash",
            "base_url": URL("http://127.0.0.1:3000/v1"),
        })
        out = buf.getvalue()
        assert "deepseek-v4-flash" in out

    def test_welcome_panel_narrow_terminal_fallback(self, buf):
        # 窄终端（<48 列）回退极简版，不渲染面板
        ui.get_console(True).width = 40
        ui.print_welcome(info={"model": "m"})
        out = buf.getvalue()
        assert "快速开始" not in out
        assert "my_agent" in out

    def test_plan_mode_renderers(self, buf):
        ui.print_stage("制定计划")
        ui.print_step_header(1, 3, "打开浏览器")
        ui.print_step_result("completed", "已完成")
        ui.print_plan(["步骤A", "步骤B"])
        ui.print_warning("注意")
        ui.print_error("失败")
        out = buf.getvalue()
        assert "▸ 1/3" in out
        assert "✓ 完成" in out
        assert "步骤A" in out and "步骤B" in out
        assert "┌" not in out

    def test_edit_call_and_diff(self, buf):
        ui.print_edit_call("tools/file.py")
        ui.print_edit_diff("old line", "new line")
        out = buf.getvalue()
        assert "✏ edit" in out
        assert "tools/file.py" in out
        assert "- old line" in out
        assert "+ new line" in out

    def test_file_write_call(self, buf):
        ui.print_file_write_call("report.xlsx")
        out = buf.getvalue()
        assert "✏ 写入" in out
        assert "report.xlsx" in out

    def test_unified_diff_rendering(self, buf):
        ui.print_unified_diff("line1\nline2\nline3\n", "line1\nLINE2\nline3\n")
        out = buf.getvalue()
        assert "@@ -1,3 +1,3 @@" in out          # 块头（含行号）
        assert "-line2" in out                    # 删除行（标准 unified diff 格式）
        assert "+LINE2" in out                    # 新增行
        assert " line1" in out                    # 上下文行（前导空格）
        assert "---" not in out                   # 文件头被剥离

    def test_unified_diff_no_changes(self, buf):
        ui.print_unified_diff("a\n", "a\n")
        out = buf.getvalue()
        assert "无差异" in out

    def test_unified_diff_truncation(self, buf):
        # 多处分散修改 → 多个 hunk → 超过 max_diff_lines 触发截断
        old = "\n".join(f"line{i}" for i in range(100))
        new = "\n".join(
            f"CHANGED{i}" if i % 10 == 0 else f"line{i}"
            for i in range(100)
        )
        ui.print_unified_diff(old, new, max_diff_lines=10)
        out = buf.getvalue()
        assert "已截断" in out

    def test_goal_echo_minimal(self, buf):
        ui.print_goal_echo("分析一下代码")
        out = buf.getvalue()
        assert "> 分析一下代码" in out
        assert "┌" not in out and "─" not in out   # 无边框无分隔线
        assert "目标" not in out

    def test_platform_notice_windows_hints(self):
        from models.prompts import PLATFORM_NOTICE_TEMPLATE
        text = PLATFORM_NOTICE_TEMPLATE.format(system="Windows", shell="cmd.exe")
        assert "Windows" in text
        assert "dir" in text and "findstr" in text
        assert "ls" in text   # 明确告知没有这些命令

    def test_plain_text_mode(self, buf):
        ui.print_header(title="", goal="g", use_rich=False, status_items=[("模式", "单循环")])
        ui.print_tool_call("terminal", {"command": "dir"}, use_rich=False)
        ui.print_tool_result(True, "ok", use_rich=False)
        out = buf.getvalue()
        assert "目标: g" in out
        assert "⏺ terminal" in out
        assert "⎿ ok" in out


class TestStreamingMarkdown:
    def test_bold_rendered(self, buf):
        r = ui.StreamingMarkdown(use_rich=True)
        r.feed("结果是 **56** 哦")
        r.flush()
        out = buf.getvalue()
        # 星号标记被消费（渲染为加粗样式），不残留字面 **
        assert "**" not in out
        assert "结果是 " in out and "56" in out and " 哦" in out

    def test_bold_split_across_deltas(self, buf):
        r = ui.StreamingMarkdown(use_rich=True)
        r.feed("结果是 **5")
        r.feed("6** 结束")
        r.flush()
        out = buf.getvalue()
        assert "**" not in out
        assert "56" in out and "结束" in out

    def test_unclosed_bold_flushed_raw(self, buf):
        r = ui.StreamingMarkdown(use_rich=True)
        r.feed("未闭合 **加粗")
        r.flush()
        out = buf.getvalue()
        # 未闭合的 ** 原样输出
        assert "**加粗" in out

    def test_trailing_single_star_held(self, buf):
        r = ui.StreamingMarkdown(use_rich=True)
        r.feed("abc*")
        r.feed("def")
        r.flush()
        out = buf.getvalue()
        assert "*" in out and "abc" in out and "def" in out

    def test_plain_text_pass_through(self, buf):
        r = ui.StreamingMarkdown(use_rich=True)
        r.feed("普通文本，没有标记")
        r.flush()
        out = buf.getvalue()
        assert "普通文本，没有标记" in out


class TestShortcutsAndDiffPanel:
    """`/shortcuts` 面板与 `/diff` 渲染（对应同类 CLI 的 ? 面板与 /diff mod）。"""

    def test_shortcuts_panel_lists_input_and_commands(self, buf):
        ui.print_shortcuts(use_rich=False)
        out = buf.getvalue()
        assert "Ctrl+C" in out
        assert "/diff" in out and "/help" in out
        assert "多行" in out or "换行" in out

    def test_shortcuts_panel_rich_renders_table(self, buf):
        ui.print_shortcuts(use_rich=True)
        out = buf.getvalue()
        assert "输入" in out and "查看" in out
        assert "/sessions" in out

    def test_git_diff_marks_lines(self, buf):
        ui.print_git_diff("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
        out = buf.getvalue()
        assert "@@" in out and "-a" in out and "+b" in out
        assert "diff --git" in out

    def test_git_diff_empty_worktree(self, buf):
        ui.print_git_diff("")
        assert "无未提交改动" in buf.getvalue()

    def test_git_diff_truncates_long_output(self, buf):
        ui.print_git_diff("\n".join(f"+line{i}" for i in range(30)), max_lines=5)
        out = buf.getvalue()
        assert "line4" in out and "line5" not in out
        assert "共 30 行" in out

    def test_git_diff_brackets_not_eaten_by_markup(self, buf):
        """diff 内容里的 `[doc](url)` 不能被 rich 当标签吞掉（Text 渲染）。"""
        ui.print_git_diff('+const x = "[doc](http://a)"\n')
        assert "[doc](http://a)" in buf.getvalue()


class TestFoldedCallsLine:
    """连续同名调用的汇总行（折叠后替代逐次输出）。"""

    def test_rich_line_has_counts(self, buf):
        ui.print_folded_calls("terminal", 10, 7)
        out = buf.getvalue()
        assert "terminal" in out and "×10" in out and "7 次同名调用已折叠" in out

    def test_plain_text_mode(self, buf):
        ui.print_folded_calls("terminal", 10, 7, use_rich=False)
        assert "⏺ terminal ×10" in buf.getvalue()

    def test_key_with_special_chars_rendered_raw(self, buf):
        """工具名前缀可能带团队 worker 归属，不能被 markup 当标签吞掉。"""
        ui.print_folded_calls("团队·w1 ▸ terminal", 6, 3)
        assert "团队·w1 ▸ terminal" in buf.getvalue()


class TestTranscriptPanel:
    """`/transcript` 事件回放（折叠摘要的展开入口）。"""

    def _events(self):
        from agent.rollout import RolloutEvent
        return [
            RolloutEvent(ts=1700000000.0, event_type="run_start", data={"goal": "跑测试"}),
            RolloutEvent(ts=1700000001.0, event_type="tool_call",
                         data={"tool": "terminal", "args": {"command": "pytest -q"}}),
            RolloutEvent(ts=1700000002.0, event_type="tool_result",
                         data={"tool": "terminal", "success": True, "output": "3 passed"}),
            RolloutEvent(ts=1700000003.0, event_type="tool_result",
                         data={"tool": "terminal", "success": False, "error": "boom"}),
        ]

    def test_renders_calls_and_results(self, buf):
        ui.print_transcript(self._events())
        out = buf.getvalue()
        assert "terminal" in out and "pytest -q" in out      # 完整参数
        assert "3 passed" in out                             # 输出正文
        assert "✗" in out and "boom" in out                  # 失败标记
        assert "事件回放 4/4 条" in out

    def test_limit_keeps_latest_and_says_omitted(self, buf):
        ui.print_transcript(self._events(), max_events=2)
        out = buf.getvalue()
        assert "事件回放 2/4 条" in out and "更早 2 条省略" in out
        assert "pytest -q" not in out

    def test_accepts_jsonl_dict_events(self, buf):
        """磁盘上的 rollout jsonl 是 dict 形态，同样要能回放。"""
        ui.print_transcript([{"ts": 1700000001.0, "event": "tool_call",
                              "data": {"tool": "file", "input": '{"operation": "read"}'}}])
        assert "file" in buf.getvalue()

    def test_empty_events_hint(self, buf):
        ui.print_transcript([])
        assert "没有 rollout 事件" in buf.getvalue()

    def test_brackets_in_args_not_eaten(self, buf):
        ui.print_transcript([{"ts": 1.0, "event": "tool_call",
                              "data": {"tool": "browser", "args": {"sel": "[data-x](y)"}}}])
        assert "[data-x](y)" in buf.getvalue()

    def test_plain_text_mode(self, buf):
        ui.print_transcript(self._events(), use_rich=False, max_events=3)
        out = buf.getvalue()
        assert "事件回放 3/4 条" in out and "3 passed" in out


class TestThemePanel:
    """`/theme` 切换配色（会话内生效，不动 .env）。"""

    def test_theme_list_and_switch(self, buf):
        assert ui.current_theme() == "default"
        ui.set_theme("mono")
        try:
            assert ui.current_theme() == "mono"
            assert ui.COLORS["primary"] == ui.THEMES["mono"]["primary"]
            ui.print_theme()
            out = buf.getvalue()
            assert "● mono" in out and "default" in out
        finally:
            ui.set_theme("default")

    def test_colors_dict_identity_is_kept(self):
        """原地更新 COLORS：别的模块 `from agent.ui_theme import COLORS` 的引用跟着变。"""
        ref = ui.COLORS
        ui.set_theme("ocean")
        try:
            assert ref["primary"] == ui.THEMES["ocean"]["primary"]
        finally:
            ui.set_theme("default")

    def test_console_rebuilt_on_switch(self):
        before = ui.get_console()
        ui.set_theme("paper")
        try:
            assert ui.get_console() is not before
        finally:
            ui.set_theme("default")

    def test_unknown_theme_raises(self):
        with pytest.raises(ValueError):
            ui.set_theme("nope")

    def test_all_themes_have_full_palette(self):
        keys = set(ui.THEMES["default"])
        for name, colors in ui.THEMES.items():
            assert set(colors) == keys, name


class TestPermissionLabels:
    """状态栏的中文档名（取值来自 approval 的预设）。"""

    @pytest.mark.parametrize("raw,zh", [("never", "不拦"), ("on-request", "逐次确认"),
                                        ("untrusted", "全禁"), ("on-failure", "失败时确认")])
    def test_policy_labels(self, raw, zh):
        assert ui.label_policy(raw) == zh

    def test_unknown_policy_passthrough(self):
        assert ui.label_policy("whatever") == "whatever"
        assert ui.label_policy("") == ""

    def test_sandbox_labels_and_suffix(self):
        assert ui.label_sandbox("read-only") == "只读"
        assert ui.label_sandbox("workspace-write") == "可写"
        assert ui.label_sandbox("danger-full-access") == "完全访问"
        assert ui.label_sandbox("workspace-write+AppContainer") == "可写+AppContainer"

    def test_approval_presets_are_all_labeled(self):
        """新增权限档却没配中文名时这条会红。"""
        from agent.approval import PERMISSION_PRESETS
        for name, preset in PERMISSION_PRESETS.items():
            assert ui.label_policy(preset["mode"]) != preset["mode"], name
