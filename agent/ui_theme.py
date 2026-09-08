"""
Agent UI 主题模块（v2：主流 CLI 风格极简界面）。

设计原则（对齐主流 CLI 的终端审美）：
- 无重型边框面板：内容即界面（唯一例外：启动欢迎面板，对齐主流 CLI 双栏启动框）
- 工具调用灰色内联：``⏺ tool(args)``，结果 ``⎿ ...``（主流 CLI 符号体系）
- 答案直接输出，无"最终结果"大框
- 单一强调色 + 大量 muted/dim 灰阶
- 等待模型时显示 "✻ 思考中…" 转圈状态
"""
import sys
import os

# Windows 控制台 UTF-8 兼容处理
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass  # Python 3.5 及以下不支持 reconfigure
    # 设置 Rich 使用 Windows 原生渲染（支持更多颜色）
    os.environ.setdefault("TERM", "xterm-256color")

from rich.console import Console
from rich.theme import Theme
from rich.style import Style
from rich.text import Text
from rich.markdown import Markdown
from typing import Optional

# ============================================================
# 颜色方案（低饱和、灰阶为主，单一强调色）
# ============================================================
COLORS = {
    "primary":     "#3B82F6",   # 蓝色 - 主色调
    "success":     "#10B981",   # 绿色 - 成功
    "warning":     "#F59E0B",   # 橙色 - 警告
    "error":       "#EF4444",   # 红色 - 错误
    "info":        "#6366F1",   # 紫色 - 信息
    "muted":       "#6B7280",   # 灰色 - 次要信息
    "highlight":   "#F472B6",   # 粉色 - 高亮
    "accent":      "#06B6D4",   # 青色 - 强调
    "bg_dark":     "#1E293B",   # 深色背景
}

# ============================================================
# Rich Theme 定义
# ============================================================
AGENT_THEME = Theme({
    "primary":     f"bold {COLORS['primary']}",
    "success":     f"bold {COLORS['success']}",
    "warning":     f"bold {COLORS['warning']}",
    "error":       f"bold {COLORS['error']}",
    "info":        f"bold {COLORS['info']}",
    "muted":       f"{COLORS['muted']}",
    "dim":         f"dim {COLORS['muted']}",
    "highlight":   f"bold {COLORS['highlight']}",
    "accent":      f"bold {COLORS['accent']}",
    "title":       f"bold white",
    "subtitle":    f"bold {COLORS['accent']}",
    "step_num":    f"bold {COLORS['primary']}",
    "step_name":   f"bold white",
    "tool_name":   f"bold white",
    "result_ok":   COLORS['success'],
    "result_fail": COLORS['error'],
    "progress":    f"bold {COLORS['accent']}",
})

# ============================================================
# 人格配置
# ============================================================
PERSONALITY = {
    "name": "小悟",
    "emoji": "✻",
    "tagline": "聪明、高效、有人情味的 AI 助手",
    "traits": [
        "专业但不冷冰冰 —— 像靠谱的工程师朋友聊天",
        "高效且直接 —— 不废话，但也不会干巴巴",
        "偶尔带点小幽默 —— 让协作不那么沉闷",
        "诚实面对失败 —— 出错了就直说，不兜圈子",
        "有人情味的回答 —— 用自然对话的方式，不是编号列表",
    ],
    "speaking_style": (
        "你的说话风格：\n"
        "- 用自然中文对话，像朋友聊天一样，不要冷冰冰的编号列表\n"
        "- 简洁直接，但带点温度。开头可以打个招呼，结尾可以加一句关切的总结\n"
        "- 偶尔可以用一点轻松的比喻或幽默（但要保持专业）\n"
        "- 失败时坦诚地说\"这个我没搞成，咱们换个思路\"，不要用被动语态推脱\n"
        "- 不要用\"尊敬的用户\"\"根据您的需求\"这种套话，直接说话就行"
    ),
}

# ============================================================
# Rich Console 实例
# ============================================================
_console: Optional[Console] = None
_legacy_console: Optional[Console] = None  # 无颜色的纯文本 console


def get_console(use_rich: bool = True) -> Console:
    """获取全局 Console 实例。"""
    global _console, _legacy_console
    if use_rich:
        if _console is None:
            _console = Console(
                theme=AGENT_THEME,
                highlight=False,
                force_terminal=True if sys.platform == "win32" else None,
                color_system="auto",
            )
        return _console
    else:
        if _legacy_console is None:
            _legacy_console = Console(no_color=True, highlight=False)
        return _legacy_console


def _rule(c: Console, char: str = "─", width: int = 60):
    """细分隔线（主流风格的弱分隔）。"""
    c.print(f"[dim]{char * width}[/dim]")


def print_header(title: str, goal: str = "", use_rich: bool = True,
                 status_items: list = None, status_text: str = ""):
    """
    打印 Agent 启动头信息（极简）。

    Args:
        status_text: 状态说明文本（单行灰字，推荐；无 emoji）
        status_items: 旧式徽章列表 [(label, value), ...]（兼容保留，不推荐）
    """
    c = get_console(use_rich)

    if use_rich:
        c.print()
        c.print(f"[primary]{PERSONALITY['emoji']}[/primary] [bold white]{PERSONALITY['name']}[/bold white]"
                f"[muted] · my_agent[/muted]")
        c.print(f"[bold white]目标[/bold white]  [white]{goal}[/white]")
        if status_text:
            c.print(f"[dim]{status_text}[/dim]")
        elif status_items:
            parts = "  ·  ".join(f"{label} [white]{value}[/white]" for label, value in status_items)
            c.print(f"[dim]{parts}[/dim]")
        _rule(c)
    else:
        c.print()
        c.print(f"{PERSONALITY['name']} · my_agent")
        c.print(f"目标: {goal}")
        if status_text:
            c.print(status_text)
        elif status_items:
            c.print("  ·  ".join(f"{label} {value}" for label, value in status_items))
        c.print("-" * 60)


def print_goal_echo(goal: str, use_rich: bool = True):
    """单次执行的目标回显（> 用户消息，无边框无状态行）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[dim]>[/dim] [white]{goal}[/white]")
    else:
        c.print(f"> {goal}")


def print_stage(stage: str, use_rich: bool = True):
    """打印阶段标记（弱化：小圆点 + 灰字）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[dim]· {stage}[/dim]")
    else:
        c.print(f"· {stage}")


def print_step_header(step_num: int, total: int, description: str, use_rich: bool = True):
    """打印步骤头（无边框）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print()
        c.print(f"[step_num]▸ {step_num}/{total}[/step_num] [step_name]{description}[/step_name]")
    else:
        c.print(f"\n▸ 步骤 {step_num}/{total}: {description}")


def print_step_result(status: str, output: str = "", error: str = "", use_rich: bool = True):
    """打印步骤执行结果。"""
    c = get_console(use_rich)
    if use_rich:
        if status == "completed":
            c.print(f"  [success]✓ 完成[/success]")
        elif status == "failed":
            c.print(f"  [error]✗ 失败[/error]")
        elif status == "continue":
            c.print(f"  [accent]→ 继续[/accent]")
        else:
            c.print(f"  [?] {status}")

        if output:
            short = output[:250].replace("\n", " ").strip()
            c.print(f"  [muted]{short}[/muted]")
        if error:
            short_err = error[:200].replace("\n", " ").strip()
            c.print(f"  [error]错误: {short_err}[/error]")
    else:
        icon_map = {"completed": "✓", "failed": "✗", "continue": "→"}
        icon = icon_map.get(status, "?")
        c.print(f"  [{icon}] 状态: {status}")
        if output:
            c.print(f"       输出: {output[:200].replace(chr(10), ' ')}")
        if error:
            c.print(f"       错误: {error[:200]}")


# ============================================================
# 主流风格的工具调用渲染
# ============================================================

def _compact_args(arguments: dict) -> str:
    """把工具参数压成一行（长值截断），形如 {code: "print(7*8)"}。"""
    import json as _json
    if not arguments:
        return ""
    parts = []
    for k, v in arguments.items():
        try:
            s = _json.dumps(v, ensure_ascii=False)
        except TypeError:
            s = str(v)
        if len(s) > 80:
            s = s[:80] + "…"
        parts.append(f"{k}: {s}")
    return ", ".join(parts)


def print_tool_call(tool: str, arguments: dict = None, use_rich: bool = True):
    """工具调用行：⏺ tool(args)，灰色内联。"""
    c = get_console(use_rich)
    args_str = _compact_args(arguments or {})
    if use_rich:
        if args_str:
            c.print(f"[muted]⏺ [/muted][tool_name]{tool}[/tool_name][muted]({args_str})[/muted]")
        else:
            c.print(f"[muted]⏺ [/muted][tool_name]{tool}[/tool_name]")
    else:
        c.print(f"⏺ {tool}({args_str})")


def print_edit_call(file_path: str, use_rich: bool = True):
    """文件修改调用行：✏ edit <文件>（主流风格，醒目）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[primary]✏ edit[/primary] [bold]{file_path}[/bold]")
    else:
        c.print(f"✏ edit {file_path}")


def print_file_write_call(file_path: str, use_rich: bool = True):
    """文件写入调用行：✏ 写入 <文件>。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[primary]✏ 写入[/primary] [bold]{file_path}[/bold]")
    else:
        c.print(f"✏ 写入 {file_path}")


def print_edit_diff(old_string: str, new_string: str, use_rich: bool = True,
                    max_lines: int = 4):
    """
    修改 diff 预览（旧版简式，参数内 old/new 片段）：红 - 旧行 / 绿 + 新行。
    """
    c = get_console(use_rich)
    old_lines = old_string.splitlines() if old_string else []
    new_lines = new_string.splitlines() if new_string else []

    if not old_lines and not new_lines:
        return
    for line in old_lines[:max_lines]:
        shown = line[:120] + ("…" if len(line) > 120 else "")
        if use_rich:
            c.print(f"[error]  - {shown}[/error]")
        else:
            c.print(f"  - {shown}")
    if len(old_lines) > max_lines:
        c.print(f"  …（旧文本共 {len(old_lines)} 行）")
    for line in new_lines[:max_lines]:
        shown = line[:120] + ("…" if len(line) > 120 else "")
        if use_rich:
            c.print(f"[success]  + {shown}[/success]")
        else:
            c.print(f"  + {shown}")
    if len(new_lines) > max_lines:
        c.print(f"  …（新文本共 {len(new_lines)} 行）")


def print_unified_diff(old_text: str, new_text: str, use_rich: bool = True,
                       max_diff_lines: int = 60, context_lines: int = 3):
    """
    Git 风格 unified diff 渲染（对齐同类实现的修改展示）：

        @@ -10,3 +10,3 @@        ← 块头（青色，含行号）
          不变的上下文行          ← 灰色
        - 删除的行               ← 红色
        + 新增的行               ← 绿色
    """
    import difflib

    c = get_console(use_rich)
    diff = list(difflib.unified_diff(
        (old_text or "").splitlines(),
        (new_text or "").splitlines(),
        fromfile="a", tofile="b", lineterm="", n=context_lines,
    ))
    # 去掉 ---/+++ 文件头（外层已有 ✏ edit 文件头）
    diff = [l for l in diff if not l.startswith(("---", "+++"))]

    if not diff:
        c.print("[dim]  （无差异）[/dim]" if use_rich else "  （无差异）")
        return

    shown = diff[:max_diff_lines]
    for line in shown:
        if line.startswith("@@"):
            if use_rich:
                c.print(f"[accent]{line}[/accent]")
            else:
                c.print(line)
        elif line.startswith("+"):
            if use_rich:
                c.print(f"[success]{line}[/success]")
            else:
                c.print(line)
        elif line.startswith("-"):
            if use_rich:
                c.print(f"[error]{line}[/error]")
            else:
                c.print(line)
        else:
            if use_rich:
                c.print(f"[dim]{line}[/dim]")
            else:
                c.print(line)
    if len(diff) > max_diff_lines:
        tail = f"  …（diff 共 {len(diff)} 行，已截断）"
        c.print(f"[dim]{tail}[/dim]" if use_rich else tail)


def print_tool_result(success: bool, output: str = "", use_rich: bool = True,
                      max_lines: int = 4, max_chars: int = 400):
    """工具结果行：⎿ ...，失败时红色。多行输出截取前几行。"""
    c = get_console(use_rich)
    if not output:
        output = "（无输出）"

    lines = [l.rstrip() for l in output.split("\n")]
    shown = lines[:max_lines]
    text = "\n".join(shown)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    if len(lines) > max_lines:
        text += f"\n…（共 {len(lines)} 行）"

    for i, line in enumerate(text.split("\n")):
        prefix = "⎿ " if i == 0 else "  "
        if use_rich:
            style = "muted" if success else "error"
            c.print(f"[{style}]{prefix}{line}[/{style}]")
        else:
            c.print(f"{prefix}{line}")


def print_final_result(result: str, use_rich: bool = True):
    """最终回答：✻ 小悟 后直接输出内容（无边框）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print()
        c.print(f"[primary]{PERSONALITY['emoji']}[/primary] [bold white]{PERSONALITY['name']}[/bold white]")
        try:
            c.print(Markdown(result or "(无输出)"))
        except Exception:
            c.print(result or "(无输出)")
        c.print()
    else:
        c.print()
        c.print(f"{result or '(无输出)'}")
        c.print()


# ============================================================
# 流式 Markdown 内联渲染（加粗在流式中生效）
# ============================================================

class StreamingMarkdown:
    """
    流式 Markdown 渲染器：把逐 token 到达的文本实时渲染到终端。

    支持的内联语法（v1）：
    - **加粗**（跨增量边界也能正确配对）
    - 其余内容原样输出（代码块/标题等保持原文本）
    """

    def __init__(self, use_rich: bool = True):
        self.use_rich = use_rich
        self._buffer = ""          # 等待配对闭合的缓冲
        self._pending_bold = False

    def _print_raw(self, text: str):
        if not text:
            return
        c = get_console(self.use_rich)
        if self.use_rich:
            c.print(text, end="", markup=False, soft_wrap=True)
        else:
            c.print(text, end="")

    def _print_bold(self, text: str):
        if not text:
            return
        c = get_console(self.use_rich)
        if self.use_rich:
            c.print(text, end="", markup=False, style="bold", soft_wrap=True)
        else:
            c.print(text, end="")

    def feed(self, delta: str):
        """喂入一段增量文本。"""
        self._buffer += delta
        while True:
            if self._pending_bold:
                idx = self._buffer.find("**")
                if idx < 0:
                    return  # 等待闭合
                self._print_bold(self._buffer[:idx])
                self._buffer = self._buffer[idx + 2:]
                self._pending_bold = False
                continue
            idx = self._buffer.find("**")
            if idx < 0:
                # 没有加粗标记：输出全部，但保留末尾可能的半个 "**"（即一个 "*"）
                hold = 1 if self._buffer.endswith("*") else 0
                if hold:
                    self._print_raw(self._buffer[:-1])
                    self._buffer = self._buffer[-1:]
                else:
                    self._print_raw(self._buffer)
                    self._buffer = ""
                return
            # 找到开始标记：输出前缀，进入加粗态
            self._print_raw(self._buffer[:idx])
            self._buffer = self._buffer[idx + 2:]
            self._pending_bold = True

    def flush(self):
        """结束：输出剩余缓冲（未闭合的 ** 原样输出）。"""
        if self._pending_bold:
            self._print_raw("**")
            self._pending_bold = False
        if self._buffer:
            self._print_raw(self._buffer)
            self._buffer = ""


def print_welcome(use_rich: bool = True, info: dict = None):
    """打印欢迎界面。

    info 为 None 时保持极简欢迎（旧调用 / 窄终端回退）；
    提供 info（model/session_id/api_key）时渲染单面板启动框：
    状态行 + 快速开始（多行粘贴/对话管理/常用命令）+ 模型与 key。
    """
    c = get_console(use_rich)
    if not use_rich or not info:
        # 极简版（兼容旧调用；非 rich 模式不渲染面板）
        if use_rich:
            c.print()
            c.print(f"[primary]{PERSONALITY['emoji']}[/primary] [bold white]my_agent[/bold white]"
                    f"[muted] · {PERSONALITY['tagline']}[/muted]")
            c.print("[dim]输入目标开始 · /sessions 历史对话 · /open <ID> 恢复 · /new 新对话 · /help 帮助 · exit 退出[/dim]")
            c.print()
        else:
            c.print()
            c.print(f"my_agent - {PERSONALITY['tagline']}")
            c.print("输入目标开始 / /sessions 历史对话 / /open <ID> 恢复 / /new 新对话 / /help 帮助 / exit 退出")
            c.print()
        return

    width = getattr(c, "width", 80) or 80
    if width < 48:
        # 窄终端放不下面板 → 极简版
        print_welcome(use_rich=True)
        return

    from rich.panel import Panel
    from rich.box import ROUNDED
    from rich.markup import escape

    model = str(info.get("model") or "?")
    session_id = str(info.get("session_id") or "")
    api_key = str(info.get("api_key") or "（未设置）")
    restored = info.get("restored", False)

    rule = "[dim]" + "─" * 26 + "[/dim]"
    status_line = (
        f"[bold white]Status:[/bold white] [success]Ready[/success]"
        + ("[dim]（已恢复该对话记录）[/dim]" if restored else "")
    )
    body = "\n".join([
        f"[primary]{PERSONALITY['emoji']}[/primary] [bold white]{PERSONALITY['name']} · my_agent[/bold white]",
        status_line,
        f"[dim]Session: {escape(session_id)}[/dim]",
        "",
        f"[bold white]── 快速开始[/bold white]{rule}",
        "[dim]1.[/dim] 输入目标（支持多行粘贴）",
        "[dim]   行尾 [white]\\\\[/white] + 回车 = 手工换行[/dim]",
        "[dim]2.[/dim] 对话管理",
        "[dim]   /new 新建  /sessions 历史  /open <ID>[/dim]",
        "[dim]3.[/dim] 更多命令",
        "[dim]   /model  /config  /help  exit[/dim]",
        "",
        f"[dim]⚙ 当前模型:[/dim] [white]{escape(model)}[/white]",
        f"[dim]🔑 API Key:[/dim] [white]{escape(api_key)}[/white] [dim](输入 /config 查看)[/dim]",
    ])

    c.print()
    c.print(Panel(body, box=ROUNDED, border_style=f"bold {COLORS['primary']}",
                  title="my_agent", padding=(0, 1)))
    c.print()


def print_goodbye(use_rich: bool = True):
    """打印退出信息。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"\n[muted]👋 再见，有需要随时找我～[/muted]\n")
    else:
        c.print("\n再见！")


def print_info(msg: str, style: str = "primary", use_rich: bool = True):
    """打印通用信息（弱化前缀，主流风格）。"""
    c = get_console(use_rich)
    if use_rich:
        if style == "success":
            c.print(f"[success]✓[/success] [muted]{msg}[/muted]")
        elif style == "warning":
            c.print(f"[warning]![/warning] [muted]{msg}[/muted]")
        elif style == "error":
            c.print(f"[error]✗[/error] [muted]{msg}[/muted]")
        else:
            c.print(f"[dim]· {msg}[/dim]")
    else:
        c.print(f"· {msg}")


def print_plan(plan: list[str], use_rich: bool = True):
    """打印步骤计划列表（弱化）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[dim]· 计划（{len(plan)} 步）[/dim]")
        for i, step in enumerate(plan, 1):
            c.print(f"[dim]  {i}.[/dim] [muted]{step}[/muted]")
    else:
        c.print(f"· 计划（{len(plan)} 步）")
        for i, step in enumerate(plan, 1):
            c.print(f"  {i}. {step}")


def print_status_bar(text: str, use_rich: bool = True):
    """常驻状态栏：每轮模型调用前刷新的灰色细行（token/沙箱/策略）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[dim]⎿ {text}[/dim]")
    else:
        c.print(f"⎿ {text}")


def print_warning(msg: str, use_rich: bool = True):
    """打印警告。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[warning]! {msg}[/warning]")
    else:
        c.print(f"[警告] {msg}")


def print_error(msg: str, use_rich: bool = True):
    """打印错误。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[error]✗ {msg}[/error]")
    else:
        c.print(f"[错误] {msg}")


def print_evolution(msg: str, use_rich: bool = True):
    """打印自我进化信息（弱化）。"""
    c = get_console(use_rich)
    if use_rich:
        c.print(f"[dim]· {msg}[/dim]")
    else:
        c.print(f"· {msg}")
