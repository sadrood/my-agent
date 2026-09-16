"""
光标可视化浮层（电脑操控时的"看得见"提示）。

背景：Agent 用 computer 工具控制键鼠时，用户只能看到屏幕上鼠标自己动，
不知道是人为还是 Agent 所为，也难以确认点击落点。这里做一个**点击穿透、
置顶、零干扰**的光环浮层：

- 光环跟随真实光标（半径 16 的橙色圆环 + 中心点）
- 每次点击在落点画一圈扩散涟漪
- 仅 Windows；Agent 操作期间显示，空闲 COMPUTER_CURSOR_IDLE 秒后自动隐藏
- 环境变量 COMPUTER_CURSOR_OVERLAY=0 可整体关闭

实现要点：tkinter 无边框置顶窗 + `-transparentcolor` 抠出透明底，
再用 Win32 扩展样式 WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE|
WS_EX_TOOLWINDOW 让鼠标事件穿透（不挡用户操作、不进任务栏、不抢焦点）。
"""
import os
import sys
import threading
import time
from collections import deque
from typing import Optional

_TRANSPARENT_KEY = "#010203"     # 抠透明用的键色（近乎不会出现在 UI 里）
_RING_COLOR = "#ff9d2e"          # 光环颜色（与 CLI 强调色一致）
_RIPPLE_COLOR = "#ffd08a"


def overlay_enabled() -> bool:
    """是否启用光标可视化（默认开；COMPUTER_CURSOR_OVERLAY=0 关闭）。"""
    return str(os.getenv("COMPUTER_CURSOR_OVERLAY", "1")).strip().lower() not in (
        "0", "false", "off", "no")


class CursorOverlay:
    """置顶透明光环浮层；在独立线程里跑 tkinter 主循环。"""

    def __init__(self, idle_seconds: float = 6.0) -> None:
        self._idle = idle_seconds
        self._cmds: deque = deque()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._alive_until = 0.0
        self._ripples: list = []          # [(born_ts, x, y)]
        self._hwnd = 0

    # ---------------- 对外 API（线程安全、永不抛） ----------------
    @staticmethod
    def available() -> bool:
        if sys.platform != "win32" or not overlay_enabled():
            return False
        try:
            import tkinter  # noqa: F401
        except Exception:
            return False
        return True

    def touch(self) -> None:
        """Agent 正在操作：确保浮层存在并续期（空闲后自动隐藏）。"""
        if not self.available():
            return
        self._alive_until = time.time() + self._idle
        self._ensure_started()
        with self._lock:
            self._cmds.append(("touch", 0, 0))

    def ripple(self, x: int = 0, y: int = 0) -> None:
        """在 (x, y)（0,0 = 当前光标）画一圈点击涟漪。"""
        if not self.available():
            return
        self.touch()
        with self._lock:
            self._cmds.append(("ripple", int(x), int(y)))

    def stop(self) -> None:
        with self._lock:
            self._cmds.append(("quit", 0, 0))

    # ---------------- 内部实现 ----------------
    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="cursor-overlay")
        self._thread.start()
        self._ready.wait(timeout=3)

    def _run(self) -> None:
        try:
            import ctypes
            import tkinter as tk
        except Exception:
            self._ready.set()
            return
        try:
            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            try:
                root.attributes("-transparentcolor", _TRANSPARENT_KEY)
            except Exception:
                pass
            root.configure(bg=_TRANSPARENT_KEY)
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{sw}x{sh}+0+0")
            canvas = tk.Canvas(root, width=sw, height=sh, bg=_TRANSPARENT_KEY,
                               highlightthickness=0, bd=0)
            canvas.pack()
            # 点击穿透 + 不抢焦点 + 不进任务栏
            try:
                GWL_EXSTYLE = -20
                WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
                WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
                hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, GWL_EXSTYLE,
                    style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
                # 键色透明（比 tk 的 -transparentcolor 可靠）：LWA_COLORKEY
                LWA_COLORKEY = 0x1
                key_rgb = 0x00030201        # COLORREF(1,2,3) = 0x00BBGGRR
                ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, key_rgb, 0, LWA_COLORKEY)
                self._hwnd = hwnd
            except Exception:
                pass
            # 不用 withdraw/deiconify（overrideredirect + transparentcolor 下重映射不可靠）：
            # 窗口常驻，空闲时画布清空 = 键色全透明 = 看不见也点不穿。
            self._ready.set()

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            def cursor_pos():
                p = _PT()
                try:
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
                    return p.x, p.y
                except Exception:
                    return 0, 0

            def tick():
                now = time.time()
                with self._lock:
                    cmds = list(self._cmds)
                    self._cmds.clear()
                for kind, x, y in cmds:
                    if kind == "quit":
                        root.destroy()
                        return
                    if kind == "ripple":
                        cx, cy = (x, y) if (x or y) else cursor_pos()
                        self._ripples.append((now, cx, cy))
                self._ripples = [r for r in self._ripples if now - r[0] < 0.6]
                visible = now < self._alive_until
                canvas.delete("all")
                if visible:
                    cx, cy = cursor_pos()
                    canvas.create_oval(cx - 16, cy - 16, cx + 16, cy + 16,
                                       outline=_RING_COLOR, width=3)
                    canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                                       fill=_RING_COLOR, outline="")
                    for born, rx, ry in self._ripples:
                        age = now - born
                        radius = 14 + int(age * 160)
                        canvas.create_oval(rx - radius, ry - radius, rx + radius, ry + radius,
                                           outline=_RIPPLE_COLOR, width=2)
                root.after(16, tick)

            root.after(16, tick)
            root.mainloop()
        except Exception as e:  # noqa: BLE001
            print(f"[cursor_overlay] 启动失败: {e!r}", file=sys.stderr)
            self._ready.set()


_overlay: Optional[CursorOverlay] = None
_overlay_lock = threading.Lock()


def get_overlay() -> Optional[CursorOverlay]:
    """进程级单例；未启用/非 Windows 时返回 None（调用方直接判空即可）。"""
    global _overlay
    if not CursorOverlay.available():
        return None
    with _overlay_lock:
        if _overlay is None:
            _overlay = CursorOverlay()
        return _overlay
