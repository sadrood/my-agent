"""终端 TUI 骨架：输出区（可滚动）+ 常驻输入框 + 状态栏。

为什么另做一个而不是改现有的行式渲染：行式输出在"跑任务时"和"等待输入时"抢同一片屏幕，
所以现在只能二选一（提示符激活时用 output_gate 把输出排队）。TUI 把屏幕分成三块，
框常驻、输出自己滚，这两个问题从根上没了。

与 line 模式**并存**：管道/CI/SSH 下 TUI 没法用，`main.py --tui` 才走这里。

本文件目前是**骨架**：接的是假事件流（`python -m agent.tui --demo`），
用来先确认观感；渲染器逐个迁移见后续提交。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

#: 与 line 模式共用同一套字形（⏺ / ⎿），免得两套界面各说各话
TOOL_MARK = "⏺"
RESULT_MARK = "⎿"


def _compact_args(arguments, limit: int = 80) -> str:
    """把工具参数压成一行（与 ui_theme 的同名函数同语义，TUI 里不需要 markup）。"""
    if not isinstance(arguments, dict):
        return str(arguments)[:limit]
    parts = []
    for key, value in arguments.items():
        text = str(value).replace("\n", " ")
        if len(text) > 40:
            text = text[:37] + "…"
        parts.append(f"{key}={text}")
    out = ", ".join(parts)
    return out[:limit] + ("…" if len(out) > limit else "")


class PromptInput(Input):
    """常驻输入框（跑任务时也留在屏幕上，只是置灰）。"""

    def set_busy(self, busy: bool) -> None:
        self.disabled = busy
        self.placeholder = ("任务运行中…（Ctrl+C 中断）" if busy
                            else "输入目标…（/help 看命令）")


class AgentTUI(App):
    """三区布局：输出（滚动）/ 输入（常驻）/ 状态栏。"""

    TITLE = "my_agent"
    SUB_TITLE = "TUI 骨架"
    CSS = """
    Screen { layout: vertical; }
    #log { height: 1fr; padding: 0 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #prompt { height: 3; }
    """
    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+l", "clear_log", "清屏"),
        ("ctrl+end", "scroll_bottom", "到底"),
    ]

    def __init__(self, status: str = "", on_submit: Optional[Callable[[str], None]] = None):
        super().__init__()
        self._status_text = status or "就绪"
        self._on_submit = on_submit
        self._busy = False
        #: 已写出的行（供测试断言；屏幕上的那份在 RichLog 里，会自动滚到底）
        self.lines: list[str] = []

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        # RichLog 自带"追加即滚到底"，正是 TUI 相比行式输出的好处之一（不用再和提示符抢屏幕）
        yield RichLog(id="log", markup=True, wrap=True, highlight=False)
        yield PromptInput(placeholder="输入目标…（/help 看命令）", id="prompt")
        yield Static(self._status_line(self._status_text), id="status")

    @staticmethod
    def _status_line(text: str) -> str:
        """状态栏文案：运行状态 + 常驻快捷键提示（两处入口共用，避免一个有一个没有）。"""
        return f"{text}   [dim]· ctrl+c 退出 · ctrl+l 清屏[/dim]"

    # ------------------------------------------------------------------
    # 对外接口：事件流 → 屏幕
    # ------------------------------------------------------------------
    def write(self, text: str = "") -> None:
        """追加一行（markup 语法与 rich 一致）。"""
        self.lines.append(text)
        try:
            self.query_one("#log", RichLog).write(text)
        except Exception:
            pass                      # 还在 compose 之前（测试里先喂事件）就先只记着

    def add_tool_call(self, tool: str, arguments=None) -> None:
        self.write(f"[bold]{TOOL_MARK}[/bold] {tool}({_compact_args(arguments)})")

    def add_tool_result(self, success: bool, output: str = "", max_lines: int = 4) -> None:
        lines = [l for l in str(output or "（无输出）").splitlines() if l.strip()]
        shown, hidden = lines[:max_lines], max(0, len(lines) - max_lines)
        style = "green" if success else "red"
        for i, line in enumerate(shown):
            prefix = RESULT_MARK if i == 0 else " "
            self.write(f"[{style}]{prefix}[/{style}] {line}")
        if hidden:
            self.write(f"[dim]  …（还有 {hidden} 行；/transcript 看完整输出）[/dim]")

    def add_todo(self, todos) -> None:
        items = [t for t in (todos or []) if isinstance(t, dict)]
        if not items:
            self.write("[dim]── 待办已清空 ──[/dim]")
            return
        done = sum(1 for t in items if str(t.get("status") or "") == "done")
        self.write(f"[dim]── 待办 {done}/{len(items)} ──[/dim]")
        for t in items:
            status = str(t.get("status") or "todo").lower()
            title = str(t.get("title") or "").strip()
            mark = {"done": "[green]✓[/green]", "in_progress": "[cyan]▶[/cyan]"}.get(
                status, "[dim]○[/dim]")
            self.write(f"  {mark} {title}")

    def set_status(self, text: str) -> None:
        """状态栏：左边是运行状态（模型/策略/token），右边常驻快捷键提示。"""
        self._status_text = text
        try:
            self.query_one("#status", Static).update(self._status_line(text))
        except Exception:
            pass

    def set_busy(self, busy: bool) -> None:
        """任务运行中：输入框置灰并改提示语（框**常驻**，不再消失）。"""
        self._busy = bool(busy)
        try:
            self.query_one("#prompt", PromptInput).set_busy(self._busy)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def on_mount(self) -> None:
        """启动就把光标放进输入框 —— 不聚焦的话回车不会提交（实测）。"""
        try:
            self.query_one("#prompt", PromptInput).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        goal = str(event.value or "").strip()
        if not goal:
            return
        event.input.value = ""
        self.write(f"[bold cyan]>[/bold cyan] {goal}")
        if self._on_submit is not None:
            self._on_submit(goal)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.lines.clear()

    def action_scroll_bottom(self) -> None:
        self._scroll_bottom_now()

    def _scroll_bottom_now(self) -> None:
        try:
            self.query_one("#log", RichLog).scroll_end(animate=False)
        except Exception:
            pass


# ----------------------------------------------------------------------
# 骨架演示：假事件流（不接模型），只为看观感
# ----------------------------------------------------------------------
_DEMO_EVENTS = [
    ("write", ("[dim]── 本轮开始 ──[/dim]",)),
    ("status", ("deepseek-flash · never · danger-full-access · ↑3.2k ↓480",)),
    ("todo", ([{"id": "t1", "title": "读事件流接口", "status": "done"},
               {"id": "t2", "title": "把工具行搬进 TUI", "status": "in_progress"},
               {"id": "t3", "title": "接真事件", "status": "todo"}],)),
    ("tool_call", ("terminal", {"command": "pytest tests -q"})),
    ("tool_result", (True, "120 passed in 3.4s\nwarnings summary\n…",)),
    ("tool_call", ("edit", {"file_path": "agent/tui.py", "old_string": "x", "new_string": "y"})),
    ("tool_result", (True, "+12 -3  agent/tui.py",)),
    ("write", ("[green]✻[/green] 骨架跑通了：输出区在滚，输入框常驻，状态栏在底。",)),
]


def run_demo(steps: Optional[int] = None) -> None:
    """按顺序把假事件投进 TUI（速度放慢，好让人看清每一行从哪来）。"""
    import threading

    app = AgentTUI(status="demo · 假事件流")

    def _feed() -> None:
        for i, (kind, args) in enumerate(_DEMO_EVENTS):
            if steps is not None and i >= steps:
                break
            time.sleep(0.6)
            if kind == "write":
                app.write(*args)
            elif kind == "status":
                app.set_status(*args)
            elif kind == "todo":
                app.add_todo(*args)
            elif kind == "tool_call":
                app.add_tool_call(*args)
            elif kind == "tool_result":
                app.add_tool_result(*args)

    def _worker() -> None:
        time.sleep(1.0)
        app.set_busy(True)
        _feed()
        app.set_busy(False)
        app.set_status("demo 结束 · 输入框已恢复")

    threading.Thread(target=_worker, daemon=True, name="tui-demo").start()
    app.run()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="TUI 骨架演示（假事件流）")
    ap.add_argument("--steps", type=int, default=None, help="只投前 N 条事件")
    _args = ap.parse_args()
    # TUI 必须在真终端里跑：管道/重定向下写出去的全是光标转义序列，只会污染日志
    if not sys.stdout.isatty():
        sys.exit("TUI 需要真终端（当前 stdout 不是终端）。用 Windows Terminal 或 cmd 窗口跑。")
    run_demo(_args.steps)
