"""
光标可视化浮层（电脑操控时的"看得见"提示）。

背景：Agent 用 computer 工具控制键鼠时，用户只能看到屏幕上鼠标自己动，
不知道是人为还是 Agent 所为，也难以确认点击落点。这里做一个**点击穿透、
置顶、零干扰**的光环浮层：

- 光环跟随真实光标（半径 16 的橙色圆环 + 中心点）
- 每次点击在落点画一圈扩散涟漪
- 仅 Windows；Agent 操作期间显示，空闲 COMPUTER_CURSOR_IDLE 秒后自动隐藏
- 默认关闭，`COMPUTER_CURSOR_OVERLAY=1` 才开（见 config.COMPUTER_USE_CONFIG）

实现要点：tkinter 无边框置顶窗 + `-transparentcolor` 抠出透明底，
再用 Win32 扩展样式 WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE|
WS_EX_TOOLWINDOW 让鼠标事件穿透（不挡用户操作、不进任务栏、不抢焦点）。
"""
import sys
import threading
import time
from collections import deque
from typing import Optional

from config import COMPUTER_USE_CONFIG

_TRANSPARENT_KEY = "#010203"     # 抠透明用的键色（近乎不会出现在 UI 里）
_RING_COLOR = "#ff9d2e"          # 光环颜色（与 CLI 强调色一致）
_RIPPLE_COLOR = "#ffd08a"


def overlay_enabled() -> bool:
    """是否启用光标可视化（开关见 config.COMPUTER_USE_CONFIG）。

    默认**关闭**：全屏置顶浮层一旦点击穿透失效会吞掉整屏鼠标事件并抢焦点，
    风险高于收益，改为按需开启。
    """
    return bool(COMPUTER_USE_CONFIG.get("cursor_overlay"))


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
        #: 持住 Tk 根窗引用：销毁与析构都会拆 Tcl，跨线程做会把解释器带崩（见 stop()）
        self._root = None

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
        """隐藏浮层（不销毁窗口）：跨线程拆 Tcl 会在任意时刻把解释器带崩。
        窗口常驻、空闲时画布清空即全透明，随 daemon 线程结束即可。
        """
        self._alive_until = 0.0

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
            self._root = root
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
            # 点击穿透 + 不抢焦点 + 不进任务栏：必须给顶层**和所有子窗口**都设
            # WS_EX_TRANSPARENT（Tk 的 Canvas 是子窗口，只设顶层会吞掉整屏鼠标事件），
            # 并在设置后读回校验，任一失败即放弃显示。
            GWL_EXSTYLE = -20
            WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
            WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
            WANT = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            LWA_COLORKEY = 0x1
            key_rgb = 0x00030201            # COLORREF(1,2,3)
            u32 = ctypes.windll.user32
            HWND_PARENT = u32.GetParent(root.winfo_id()) or root.winfo_id()
            self._hwnd = HWND_PARENT

            handles = [HWND_PARENT]
            try:
                CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def _collect(h, _l):
                    handles.append(int(h))
                    return True
                u32.EnumChildWindows(HWND_PARENT, CB(_collect), 0)
            except Exception:
                pass

            ok = True
            for h in handles:
                try:
                    st = u32.GetWindowLongW(h, GWL_EXSTYLE)
                    u32.SetWindowLongW(h, GWL_EXSTYLE, st | WANT)
                    if h == HWND_PARENT:
                        u32.SetLayeredWindowAttributes(h, key_rgb, 0, LWA_COLORKEY)
                    back = u32.GetWindowLongW(h, GWL_EXSTYLE)
                    if (back & WS_EX_TRANSPARENT) == 0 or (back & WS_EX_NOACTIVATE) == 0:
                        ok = False
                except Exception:
                    ok = False
            if not ok:
                print("[cursor_overlay] 点击穿透校验失败，已放弃显示（避免吞掉鼠标事件）", file=sys.stderr)
                # 不销毁窗口：拆 Tcl 会在随后任意时刻把解释器带崩（见 stop()）。
                # 此时还没 ShowWindow，保持不映射即可；self._root 持着引用防析构
                self._ready.set()
                return
            try:
                u32.ShowWindow(HWND_PARENT, 4)      # SW_SHOWNOACTIVATE：显示但不激活
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
            _overlay = CursorOverlay(
                idle_seconds=float(COMPUTER_USE_CONFIG.get("cursor_idle_seconds", 6)))
        return _overlay
