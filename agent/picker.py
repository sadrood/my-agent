"""终端单选器（rich Live 原地重绘，对标 `claude -r` 的会话选择）。

为什么不用手写 ANSI 光标回退：部分终端会把回退序列吞掉，于是每次按键都追加一整帧
（实测出现几百行重复）；rich 的 Live 会按终端能力处理刷新，并且统一管理宽度与折行，
长标题不会把高亮撑成多行。

按键：↑/↓ 移动，输入文字过滤，回车打开，Esc/q/Ctrl+C 取消；
Home/End/PageUp/PageDown 也可用。非 TTY 返回 None，由调用方退回纯文本列表。
"""
import os
import sys

_UP, _DOWN, _ENTER, _CANCEL = "up", "down", "enter", "cancel"
_HOME, _END, _PGUP, _PGDN, _BACKSPACE = "home", "end", "pgup", "pgdn", "backspace"

#: Windows 方向键的第二字符（msvcrt 先返回 \x00/\xe0、再返回扫描码）
_WIN_KEYS = {
    "H": _UP, "P": _DOWN, "G": _HOME, "O": _END, "I": _PGUP, "Q": _PGDN,
}


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN_KEYS.get(msvcrt.getwch(), "")
        if ch in ("\r", "\n"):
            return _ENTER
        if ch in ("\x1b", "q", "Q", "\x03"):
            return _CANCEL
        if ch in ("\x08", "\x7f"):
            return _BACKSPACE
        return ch

    import select as _sel
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # 方向键/翻页键是 ESC [ ...；单独按 Esc 时后面没字符，别卡在 read 上
            if not _sel.select([sys.stdin], [], [], 0.05)[0]:
                return _CANCEL
            seq = sys.stdin.read(1)
            if seq != "[":
                return _CANCEL
            if not _sel.select([sys.stdin], [], [], 0.05)[0]:
                return _CANCEL
            tail = sys.stdin.read(1)
            if tail.isdigit():                  # PgUp/PgDn 是 [5~ / [6~
                while tail and tail[-1] != "~":
                    if not _sel.select([sys.stdin], [], [], 0.05)[0]:
                        break
                    tail += sys.stdin.read(1)
                return {"5~": _PGUP, "6~": _PGDN}.get(tail, "")
            return {"A": _UP, "B": _DOWN, "H": _HOME, "F": _END}.get(tail, "")
        if ch in ("\r", "\n"):
            return _ENTER
        if ch in ("q", "Q", "\x03"):
            return _CANCEL
        if ch in ("\x7f", "\x08"):
            return _BACKSPACE
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _console(console=None):
    if console is not None:
        return console
    from agent.ui_theme import get_console
    return get_console()


def _render(items, view, idx, start, height, title, query):
    from rich.console import Group
    from rich.text import Text

    lines = []
    if title:
        lines.append(Text(title, style="dim", no_wrap=True, overflow="ellipsis"))
    if view:
        for pos in range(start, min(start + height, len(view))):
            text = str(items[view[pos]])
            if pos == idx:
                lines.append(Text(f"❯ {text}", style="reverse", no_wrap=True,
                                  overflow="ellipsis"))
            else:
                lines.append(Text(f"  {text}", style="dim", no_wrap=True,
                                  overflow="ellipsis"))
    else:
        lines.append(Text("  （没有匹配的条目）", style="dim"))
    hint = "↑/↓ 移动 · 回车打开 · Esc 取消"
    hint += f" · 过滤: {query}" if query else " · 输入文字过滤"
    lines.append(Text(hint, style="dim", no_wrap=True, overflow="ellipsis"))
    return Group(*lines)


def select(items, *, title: str = "", height: int = 12, key_reader=None,
           console=None, interactive=None):
    """↑/↓ 选择（可输入过滤），返回选中下标；取消/非 TTY 返回 None。"""
    from rich.live import Live

    items = list(items or [])
    if not items:
        return None
    if interactive is None:
        try:
            interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:                     # noqa: BLE001
            interactive = False
    if not interactive:
        return None

    con = _console(console)
    key_reader = key_reader or _read_key
    height = max(3, int(height))
    query = ""
    view = list(range(len(items)))
    idx = start = 0

    def _recompute():
        nonlocal view, idx, start
        if not query:
            view = list(range(len(items)))
        else:
            q = query.lower()
            view = [i for i, item in enumerate(items) if q in str(item).lower()]
        idx = start = 0

    with Live(_render(items, view, idx, start, height, title, query),
              console=con, auto_refresh=False, transient=True) as live:
        while True:
            try:
                key = key_reader()
            except (EOFError, KeyboardInterrupt, OSError):
                return None
            if key == _ENTER:
                return view[idx] if view else None
            if key == _CANCEL:
                return None
            if key == _UP:
                idx = max(0, idx - 1)
            elif key == _DOWN:
                idx = min(len(view) - 1, idx + 1) if view else 0
            elif key == _PGUP:
                idx = max(0, idx - height)
            elif key == _PGDN:
                idx = min(len(view) - 1, idx + height) if view else 0
            elif key == _HOME:
                idx = 0
            elif key == _END:
                idx = max(0, len(view) - 1)
            elif key == _BACKSPACE:
                if query:
                    query = query[:-1]
                    _recompute()
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                query += key
                _recompute()
            # 窗口跟随光标
            if idx < start:
                start = idx
            elif idx >= start + height:
                start = idx - height + 1
            live.update(_render(items, view, idx, start, height, title, query),
                        refresh=True)
