"""Agent UI 主题模块：主流 CLI 风格极简界面。"""
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

# 同一组语义色，换一套取值就是一套主题（`/theme` 切换，只在会话内生效）
THEMES = {
    "default": dict(COLORS),
    "ocean": {                       # 冷色：偏青蓝，终端冷光屏更舒服
        "primary":     "#38BDF8", "success":     "#34D399",
        "warning":     "#FBBF24", "error":       "#F87171",
        "info":        "#818CF8", "muted":       "#64748B",
        "highlight":   "#F0ABFC", "accent":      "#22D3EE",
        "bg_dark":     "#0F172A",
    },
    "paper": {                       # 浅色终端（白底）用：深字低饱和
        "primary":     "#1D4ED8", "success":     "#047857",
        "warning":     "#B45309", "error":       "#B91C1C",
        "info":        "#4F46E5", "muted":       "#57606A",
        "highlight":   "#BE185D", "accent":      "#0E7490",
        "bg_dark":     "#E2E8F0",
    },
    "mono": {                        # 灰阶：录屏/截图时避免颜色干扰
        "primary":     "#E5E7EB", "success":     "#9CA3AF",
        "warning":     "#D1D5DB", "error":       "#F3F4F6",
        "info":        "#CBD5E1", "muted":       "#6B7280",
        "highlight":   "#9CA3AF", "accent":      "#D1D5DB",
        "bg_dark":     "#111827",
    },
    "claude": {                      # 克制风：近白灰打底，只在成功/警告/错误处上色
        "primary":     "#C8C8C8", "success":     "#7FA867",
        "warning":     "#C79A4B", "error":       "#C96A6A",
        "info":        "#9AA9BD", "muted":       "#6E6E6E",
        "highlight":   "#B79AD1", "accent":      "#8FAFB8",
        "bg_dark":     "#1A1A1A",
    },
}

# 默认主题：claude（暗灰打底 + 少量语义色，观感更静）。`/theme default` 切回高饱和配色。
COLORS.update(THEMES["claude"])
_current_theme = "claude"


def theme_names() -> list:
    """可用主题名（`/theme` 列表用）。"""
    return sorted(THEMES)


def current_theme() -> str:
    return _current_theme


# ============================================================
# Rich Theme 定义
# ============================================================
def build_theme(colors: dict) -> Theme:
    """语义标签 → rich 样式。颜色变了重建一次即可。"""
    return Theme({
        "primary":     f"bold {colors['primary']}",
        "success":     f"bold {colors['success']}",
        "warning":     f"bold {colors['warning']}",
        "error":       f"bold {colors['error']}",
        "info":        f"bold {colors['info']}",
        "muted":       f"{colors['muted']}",
        "dim":         f"dim {colors['muted']}",
        "highlight":   f"bold {colors['highlight']}",
        "accent":      f"bold {colors['accent']}",
        "title":       f"bold white",
        "subtitle":    f"bold {colors['accent']}",
        "step_num":    f"bold {colors['primary']}",
        "step_name":   f"bold white",
        "tool_name":   f"bold white",
        "result_ok":   colors['success'],
        "result_fail": colors['error'],
        "progress":    f"bold {colors['accent']}",
    })


AGENT_THEME = build_theme(COLORS)

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
    """获取全局 Console 实例。

    提示符正在等待输入时返回**排队替身**：并发输出会覆盖用户正在敲的那一行，
    所以先排队、等回车后再放（见 agent/output_gate.py）。
    """
    global _console, _legacy_console
    from agent import output_gate
    if use_rich:
        if _console is None:
            _console = Console(
                theme=AGENT_THEME,
                highlight=False,
                force_terminal=True if sys.platform == "win32" else None,
                color_system="auto",
            )
        return output_gate.DeferringConsole(_console) if output_gate.is_prompt_active() else _console
    else:
        if _legacy_console is None:
            _legacy_console = Console(no_color=True, highlight=False)
        return (output_gate.DeferringConsole(_legacy_console)
                if output_gate.is_prompt_active() else _legacy_console)


def set_theme(name: str) -> str:
    """切换配色主题（会话内生效）。原地更新 COLORS，已有的 `from ... import COLORS` 引用跟着变。"""
    global AGENT_THEME, _console, _current_theme
    key = (name or "").strip().lower()
    if key not in THEMES:
        raise ValueError(f"未知主题 {name!r}（可用: {' / '.join(theme_names())}）")
    COLORS.clear()
    COLORS.update(THEMES[key])
    AGENT_THEME = build_theme(COLORS)
    _console = None          # 下次 get_console 用新主题重建
    _current_theme = key
    return key


def _rule(c: Console, char: str = "─", width: int = 60):
    """细分隔线（主流风格的弱分隔）。"""
    c.print(f"[dim]{char * width}[/dim]")


def print_header(title: str, goal: str = "", use_rich: bool = True,
                 status_items: list = None, status_text: str = ""):
    """打印 Agent 启动头信息（极简）。"""
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


def print_folded_calls(key: str, total: int, folded: int, use_rich: bool = True):
    """连续同名调用的汇总行：`⏺ terminal ×10（7 次同名调用已折叠）`。
    替代被折叠的那些 `⏺ 工具(args)` 与 `⎿ 结果` 行（完整过程见 rollout 记录）。"""
    c = get_console(use_rich)
    tail = f"（{folded} 次同名调用已折叠）"
    if use_rich:
        from rich.text import Text
        line = Text()
        line.append("⏺ ", style="muted")
        line.append(str(key), style="tool_name")
        line.append(f" ×{total}", style="muted")
        line.append(tail, style="dim")
        c.print(line)
    else:
        c.print(f"⏺ {key} ×{total}{tail}")


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
    """Git 风格 unified diff 渲染（对齐同类实现的修改展示）："""
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
                      max_lines: int = 4, max_chars: int = 400,
                      label: str = "", index: int = 0, total: int = 0):
    """工具结果行：⎿ ...，失败时红色。多行输出截取前几行。
    label/index/total 给结果块加归属——同名批次（如两条 terminal）光看工具名分不清哪块是谁的。"""
    c = get_console(use_rich)
    if not output:
        output = "（无输出）"

    lines = [l.rstrip() for l in output.split("\n")]
    shown = lines[:max_lines]
    text = "\n".join(shown)
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    if len(lines) > max_lines:
        text += f"\n…（共 {len(lines)} 行；完整输出见 /transcript）"

    # 归属头只加在首行；单调用批次不加序号，避免噪音
    head = ""
    if label:
        head = f"[{index}/{total}] {label}: " if total > 1 else f"{label}: "

    for i, line in enumerate(text.split("\n")):
        prefix = f"⎿ {head}" if i == 0 else "  "
        if use_rich:
            from rich.text import Text
            style = "muted" if success else "error"
            # 用 Text 而不是 f-string 拼 markup：工具输出/文件内容里的 `[doc](url)`会被 rich 当成标签**静默吞掉文本**。
            c.print(Text(f"{prefix}{line}", style=style))
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
    """流式 Markdown 渲染器：把逐 token 到达的文本实时渲染到终端。"""

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


#: 欢迎面板底部的轮换小提示（同类 CLI 的习惯：一次只给一条，别堆成说明书）
_TIPS = (
    "用 /diff 看本次会话改了哪些文件",
    "长任务会把连续同名调用折叠成一行，/transcript 可展开完整过程",
    "/theme 换配色：claude（默认）/ default / ocean / paper / mono",
    "整块粘贴会合并成一条消息；行尾 \\ 回车可手工换行",
    "/shortcuts 看快捷键面板",
)
_tip_index = 0


def _pick_tip() -> str:
    """按顺序轮换（不用 random：同一进程里可预期，便于截图与测试）。"""
    global _tip_index
    tip = _TIPS[_tip_index % len(_TIPS)]
    _tip_index += 1
    return tip


def print_todos(todos, use_rich: bool = True):
    """待办清单（todo 工具每次变更时刷新一遍）。

    字形与 tools/todo.py 的 `_render` 保持一致（✓ / ▶ / ○），否则模型看到的和
    用户看到的会是两套符号。
    """
    items = [t for t in (todos or []) if isinstance(t, dict)]
    if not items:
        c = get_console(use_rich)
        c.print("[dim]── 待办已清空 ──[/dim]")
        return
    c = get_console(use_rich)
    done = sum(1 for t in items if str(t.get("status") or "todo").lower() == "done")
    c.print(f"[dim]── 待办 {done}/{len(items)} ──[/dim]")
    for t in items:
        status = str(t.get("status") or "todo").strip().lower()
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        if status == "done":
            c.print(f"  [success]✓[/success] [dim]{title}[/dim]")
        elif status == "in_progress":
            c.print(f"  [primary]▶[/primary] {title}")
        else:
            c.print(f"  [dim]○ {title}[/dim]")


def _box_width(c: Console) -> int:
    """输入框总宽度：跟终端宽度走，留一列给行尾（避免正好撞上自动换行）。"""
    return max(24, int(getattr(c, "width", 80) or 80) - 1)


def print_prompt_box_top(use_rich: bool = True):
    """输入框上边框。

    诚实说明：rich 是行式渲染，做不到同类 CLI 那种"光标真的在框内、整框随输入重绘"。
    这里画上边框 + 用 `│ ` 作提示符 + 下边框，读起来是一个框。
    """
    c = get_console(use_rich)
    w = _box_width(c)
    if use_rich:
        c.print(f"[dim]╭{'─' * (w - 2)}╮[/dim]")
    else:
        c.print("+" + "-" * (w - 2) + "+")


def print_prompt_box_bottom(use_rich: bool = True,
                            hint: str = "/shortcuts 快捷键 · /help 命令 · Ctrl+C 中断"):
    """输入框下边框（提示语嵌在框线上）。

    填充量 = 总宽 - 左框角 - 提示语 - 右框角；中文按**显示宽度**算（cell_len），
    否则右边缘会比上框线多出一列。
    """
    from rich.cells import cell_len
    c = get_console(use_rich)
    w = _box_width(c)
    head, tail = "╰─ ", " ╯"
    pad = max(0, w - cell_len(head) - cell_len(hint) - cell_len(tail))
    if use_rich:
        c.print(f"[dim]{head}[/dim]{hint} [dim]{'─' * pad}{tail.strip()}[/dim]")
    else:
        c.print("+- " + hint + " " + "-" * pad + "+")


def print_welcome(use_rich: bool = True, info: dict = None):
    """打印欢迎界面。
    info 为 None 时保持极简欢迎（旧调用 / 窄终端回退）；"""
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
    workspace = str(info.get("workspace") or "")
    tip = _pick_tip()

    rule = "[dim]" + "─" * 26 + "[/dim]"
    status_line = (
        f"[bold white]Status:[/bold white] [success]Ready[/success]"
        + ("[dim]（已恢复该对话记录）[/dim]" if restored else "")
    )
    body = "\n".join([
        f"[primary]{PERSONALITY['emoji']}[/primary] [bold white]{PERSONALITY['name']} · my_agent[/bold white]",
        status_line,
        f"[dim]Session: {escape(session_id)}[/dim]",
        f"[dim]cwd:     {escape(workspace)}[/dim]",
        "",
        f"[bold white]── 快速开始[/bold white]{rule}",
        "[dim]1.[/dim] 输入目标（支持多行粘贴）",
        "[dim]   行尾 [white]\\\\[/white] + 回车 = 手工换行[/dim]",
        "[dim]2.[/dim] /new 新建  /sessions 历史  /open <ID>  /help 全部命令",
        "",
        f"[dim]⚙ 当前模型:[/dim] [white]{escape(model)}[/white]",
        f"[dim]🔑 API Key:[/dim] [white]{escape(api_key)}[/white] [dim](输入 /config 查看)[/dim]",
        "",
        f"[dim]💡 {escape(tip)}[/dim]",
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


def print_git_diff(diff_text: str, use_rich: bool = True, max_lines: int = 200):
    """工作区改动渲染：@@ 青 / + 绿 / - 红 / 文件头灰；超长截断。"""
    c = get_console(use_rich)
    lines = (diff_text or "").splitlines()
    if not lines:
        if use_rich:
            from rich.text import Text
            c.print(Text("  （工作区无未提交改动）", style="muted"))
        else:
            c.print("  （工作区无未提交改动）")
        return

    from rich.text import Text
    for line in lines[:max_lines]:
        shown = line[:200]
        if not use_rich:
            c.print(shown)
            continue
        if shown.startswith("@@"):
            style = "cyan"
        elif shown.startswith("+"):
            style = "green"
        elif shown.startswith("-"):
            style = "red"
        elif shown.startswith(("diff --git", "index ", "new file", "deleted file",
                              "similarity", "rename", "old mode", "new mode")):
            style = "dim"
        else:
            style = "grey62"
        # Text 而非 markup 字符串：diff 里的 `[...]` 会被 rich 当标签吞掉
        c.print(Text(shown, style=style))
    if len(lines) > max_lines:
        c.print(Text(f"  …（共 {len(lines)} 行，仅显示前 {max_lines} 行）", style="dim"))


def _transcript_fields(event):
    """兼容两种事件来源：RolloutEvent 对象 / rollout jsonl 里的 dict。"""
    if isinstance(event, dict):
        return event.get("ts"), event.get("event", ""), event.get("data") or {}
    return getattr(event, "ts", None), getattr(event, "event_type", ""), \
        (getattr(event, "data", None) or {})


def print_transcript(events, use_rich: bool = True, max_events: int = 40):
    """事件回放：折叠摘要的"展开入口"——逐条打印工具完整参数与结果。"""
    import json as _json
    from datetime import datetime

    items = list(events or [])
    c = get_console(use_rich)
    if not items:
        print_info("本轮没有 rollout 事件（可能是本轮还没跑，或 ROLLOUT_ENABLED=false）。",
                   style="info", use_rich=use_rich)
        return
    shown = items if (not max_events or max_events <= 0) else items[-max_events:]
    head = f"◈ 事件回放 {len(shown)}/{len(items)} 条"
    c.print()
    c.print(Text(head, style="bold white") if use_rich else head)
    if len(items) > len(shown):
        tip = f"  …（更早 {len(items) - len(shown)} 条省略；/transcript all 看全部）"
        c.print(Text(tip, style="dim") if use_rich else tip)

    for e in shown:
        ts, etype, data = _transcript_fields(e)
        stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--:--"
        if etype == "tool_call":
            args = data.get("args")
            if args is None:
                args = data.get("input") or data.get("action") or ""
            if not isinstance(args, str):
                args = _json.dumps(args, ensure_ascii=False)
            args = args.replace("\n", "\n    ")      # 多行参数（edit 正文）缩进对齐
            line = f"⏺ {stamp} {data.get('tool', '?')}({args})"
            style = "cyan"
        elif etype == "tool_result":
            ok = data.get("success")
            mark = "✓" if ok else "✗"
            body = str(data.get("output") or data.get("error") or "").rstrip()
            line = f"⎿ {stamp} {mark} {data.get('tool', '?')}"
            style = "green" if ok else "red"
            if body:
                line += "\n" + "\n".join("     " + ln for ln in body.splitlines())
        elif etype in ("run_start", "run_loop_start"):
            line = f"▶ {stamp} 开始 {_json.dumps(data, ensure_ascii=False)[:160]}"
            style = "bold white"
        elif etype in ("run_end", "run_loop_end"):
            line = f"■ {stamp} 结束 {_json.dumps(data, ensure_ascii=False)[:160]}"
            style = "bold white"
        elif etype in ("guardian", "guardian_overridden", "tool_blocked", "ops_warning"):
            line = f"! {stamp} {etype} {_json.dumps(data, ensure_ascii=False)[:160]}"
            style = "yellow"
        else:
            line = f"· {stamp} {etype} {_json.dumps(data, ensure_ascii=False)[:160]}"
            style = "grey62"
        # Text 而非 markup 字符串：参数里的 `[xxx]` 会被 rich 当标签吞掉
        c.print(Text(line, style=style) if use_rich else line)


def print_theme(use_rich: bool = True):
    """主题面板：列出可用配色与当前生效项（`/theme` 无参时）。"""
    c = get_console(use_rich)
    rows = []
    for name in theme_names():
        colors = THEMES[name]
        mark = "●" if name == _current_theme else " "
        rows.append(f"{mark} {name:<9} primary {colors['primary']} · accent {colors['accent']} "
                    f"· success {colors['success']}")
    c.print()
    for line in rows:
        c.print(Text(line, style="white") if use_rich else line)
    print_info(f"切换: /theme <{'|'.join(theme_names())}>（本会话生效，不写 .env）",
               style="info", use_rich=use_rich)


# 权限档中文名：取值来自 agent/approval.py 的 PERMISSION_PRESETS（未知值原样显示）
POLICY_LABELS = {
    "on-request": "逐次确认", "never": "不拦", "untrusted": "全禁",
    "on-failure": "失败时确认", "always": "每次都问",
}
SANDBOX_LABELS = {
    "read-only": "只读", "workspace-write": "可写", "danger-full-access": "完全访问",
}


def label_policy(mode: str) -> str:
    """审批策略的中文名（如 never → 不拦）。"""
    return POLICY_LABELS.get(mode, mode or "")


def label_sandbox(mode: str) -> str:
    """沙箱等级的中文名，保留 `+AppContainer` 这类后缀。"""
    if not mode:
        return ""
    base, sep, suffix = mode.partition("+")
    return SANDBOX_LABELS.get(base, base) + (sep + suffix if sep else "")


def print_shortcuts(use_rich: bool = True):
    """快捷键/命令面板（等价同类 CLI 的 `?` 面板）。
    只列终端真能收到的键：cmd 下 Shift+Tab、Ctrl+O 这类修饰键拿不到可靠序列，
    对应功能一律给斜杠命令入口。"""
    groups = [
        ("输入", [
            ("Enter", "提交；行尾 \\ + 回车 = 手工换行"),
            ("Ctrl+C", "中断当前输入或任务（会话每轮自动落盘）"),
            ("exit / quit / q", "退出（Ctrl+Z 回车、EOF 亦可）"),
        ]),
        ("对话", [
            ("/new", "新开一个对话"),
            ("/sessions", "历史对话列表"),
            ("/open <ID>", "打开并恢复历史对话"),
            ("/history [N]", "回看当前对话的记录"),
            ("/rename <名> /title", "改名 / 让模型重新起名"),
        ]),
        ("查看", [
            ("/diff", "工作区未提交改动（含本次 edit）"),
            ("/transcript [N|all]", "回放本轮工具调用与输出（折叠摘要的展开入口）"),
            ("/tools", "全部工具（含 JSON Schema）"),
            ("/config /model /permission", "配置 / 切模型 / 权限档"),
            ("/theme [名字]", "终端配色：default / ocean / paper / mono"),
            ("/help", "全部斜杠命令"),
        ]),
    ]
    c = get_console(use_rich)
    if not use_rich:
        for title, rows in groups:
            c.print(f"【{title}】")
            for key, desc in rows:
                c.print(f"  {key:<26} {desc}")
        return

    from rich.table import Table
    from rich.text import Text
    # 第二列 fold：默认的 ellipsis 会把描述截成"…"，面板就白做了
    table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="white", overflow="fold")
    for title, rows in groups:
        table.add_row(Text(title, style="bold white"), "")
        for key, desc in rows:
            table.add_row(Text(key, style="cyan"), Text(desc, style="grey62"))
    c.print()
    c.print(table)


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
