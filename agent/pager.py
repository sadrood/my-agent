"""终端分页器：长输出按屏分页，不依赖方向键。

按键：空格 = 下一页，回车 = 下一行，q / Esc = 退出。
非 TTY（管道、重定向、测试）或拿不到按键时退回一次性全打印，不改变脚本行为。
"""
import os
import re
import shutil
import sys

_MARKUP = re.compile(r"\[/?[^\]]*\]")     # 粗估可视宽度时剥掉 rich 标签


def _visible_len(s: str) -> int:
    return len(_MARKUP.sub("", str(s)))


def _size():
    try:
        size = shutil.get_terminal_size((80, 24))
        return max(20, size.columns), max(5, size.lines - 2)
    except Exception:                     # noqa: BLE001
        return 80, 20


def _rows(line: str, width: int) -> int:
    """这一逻辑行会占几行屏幕（按可视宽度估）。"""
    return max(1, (_visible_len(line) + width - 1) // width)


def _read_key() -> str:
    """读一个按键（不回显）。"""
    if os.name == "nt":
        import msvcrt
        return msvcrt.getwch()
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def page(lines, *, print_fn=print, key_reader=None, height=None,
         interactive=None) -> bool:
    """分页打印 lines。返回 True = 已看完，False = 用户中途退出。"""
    lines = list(lines or [])
    if not lines:
        return True

    if interactive is None:
        try:
            interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:                 # noqa: BLE001
            interactive = False
    if not interactive:
        for line in lines:
            print_fn(line)
        return True

    key_reader = key_reader or _read_key
    width, full = _size()
    h = int(height or full)
    total = len(lines)
    i = 0
    while i < total:
        rows, start = 0, i
        while i < total and rows < h:
            rows += _rows(lines[i], width)
            i += 1
        for line in lines[start:i]:
            print_fn(line)
        if i >= total:
            return True
        print_fn(f"-- {start + 1}-{i}/{total} 行 | 空格=下一页  回车=下一行  q=退出 --")
        key = ""
        while key not in (" ", "\r", "\n", "q", "Q", "\x1b"):
            try:
                key = key_reader()
            except (EOFError, KeyboardInterrupt):
                return False
        if key in ("q", "Q", "\x1b"):
            print_fn(f"[分页退出：共 {total} 行，已显示到第 {i} 行]")
            return False
        # 回车 = 只再放一行，然后回到整页；空格 = 继续整页
        h = 1 if key in ("\r", "\n") else int(height or full)
    return True
