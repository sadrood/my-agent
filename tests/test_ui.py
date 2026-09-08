"""
UI 主题冒烟测试：主流终端风格渲染函数不抛异常且符号正确。
"""
import io

import pytest
from rich.console import Console

import agent.ui_theme as ui


@pytest.fixture
def buf():
    b = io.StringIO()
    console = Console(file=b, force_terminal=False, highlight=False)
    ui.get_console = lambda use_rich=True: console  # monkeypatch 全局 console
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
