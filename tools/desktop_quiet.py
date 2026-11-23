"""非抢占（quiet）桌面操控原语。

与共享输入通道不同，这里所有动作都不进入系统输入队列：不移动光标、
不改前台窗口、不占用键盘。感知走 PrintWindow（单窗口），动作走 UIA
control patterns，兜底走定向窗口消息（PostMessage）。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes as wt

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WM_MOUSEACTIVATE = 0x0021
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_LBUTTONDBLCLK = 0x0203
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
PW_RENDERFULLCONTENT = 0x00000002

_BUTTON_CLASS_HINTS = ("BUTTON", "BUTTON2", "WINFORMS", "COMBOBOX")

# 每个 hwnd 上一次 inspect 展平的元素列表，供 "#3" 这类序号寻址复用
_FLAT_CACHE: dict[int, list] = {}


def _init_signatures() -> None:
    """HWND 在 64 位下是指针，不声明签名会被截成 int32。"""
    user32.PostMessageW.restype = wt.BOOL
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SendMessageW.restype = ctypes.c_long
    user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p]
    user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ChildWindowFromPointEx.restype = wt.HWND
    user32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
    user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetGUIThreadInfo.restype = wt.BOOL
    user32.GetGUIThreadInfo.argtypes = [wt.UINT, ctypes.c_void_p]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]


_init_signatures()


def _lp(x: int, y: int) -> int:
    """LPARAM：低 16 位 x、高 16 位 y（客户区坐标）。"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _gdi_ready() -> None:
    """GDI 句柄是 64 位指针，必须声明 restype，否则截断成 int32 会溢出。"""
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [wt.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wt.HWND, ctypes.c_void_p]
    user32.PrintWindow.restype = wt.BOOL
    user32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.UINT]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def window_title(hwnd: int) -> str:
    n = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, n, 512)
    return n.value


def list_windows() -> list[dict]:
    """枚举可见顶层窗口（只读，不做任何激活）。"""
    out: list[dict] = []
    proc_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _l):
        if user32.IsWindowVisible(h):
            t = window_title(h)
            if t.strip():
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                out.append({"hwnd": h, "title": t, "pid": pid.value,
                            "rect": [r.left, r.top, r.right, r.bottom]})
        return True

    user32.EnumWindows(proc_t(cb), 0)
    return out


def find_hwnd(substr: str) -> int | None:
    """按标题子串匹配窗口；优先已可见的精确匹配。"""
    if not substr:
        return None
    wins = list_windows()
    low = substr.lower()
    for w in wins:
        if w["title"].lower() == low:
            return w["hwnd"]
    for w in wins:
        if low in w["title"].lower():
            return w["hwnd"]
    return None


def capture(hwnd: int, path: str) -> dict:
    """PrintWindow 单窗口截图：不截全屏、不需要窗口在前台、不影响用户。"""
    _gdi_ready()
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return {"ok": False, "reason": "GetWindowRect 失败（窗口可能已关闭）"}
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return {"ok": False, "reason": f"窗口尺寸异常 {w}x{h}（可能已最小化）"}
    dc = user32.GetWindowDC(hwnd)
    if not dc:
        return {"ok": False, "reason": "GetWindowDC 失败（窗口被销毁或无权限）"}
    mem = bmp = old = None
    try:
        mem = gdi32.CreateCompatibleDC(dc)
        bmp = gdi32.CreateCompatibleBitmap(dc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
        bi = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                               biPlanes=1, biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if got == 0:
            return {"ok": False, "reason": "GetDIBits 失败"}
        from PIL import Image
        img = Image.frombytes("RGB", (w, h), buf.raw, "raw", "BGRX")
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        img.save(path)
        px = list(img.getdata())[:: max(1, (w * h) // 400)]
        blank = len(set(px)) <= 2
        return {"ok": True, "path": path, "width": w, "height": h,
                "blank": blank, "note": "画面近乎单色：该窗口可能不响应 PrintWindow（GPU 合成/DRM 内容）" if blank else ""}
    finally:
        if mem and old:
            gdi32.SelectObject(mem, old)
        if mem:
            gdi32.DeleteDC(mem)
        if bmp:
            gdi32.DeleteObject(bmp)
        user32.ReleaseDC(hwnd, dc)


def _patterns(ctrl) -> list[str]:
    import uiautomation as auto
    got = []
    for name in ("InvokePattern", "TogglePattern", "ValuePattern", "ExpandCollapsePattern",
                 "SelectionItemPattern", "ScrollPattern", "RangeValuePattern"):
        try:
            if ctrl.GetPattern(getattr(auto.PatternId, name)):
                got.append(name.replace("Pattern", ""))
        except Exception:
            pass
    return got


def inspect(hwnd: int, max_elements: int = 60, max_depth: int = 8) -> dict:
    """读取窗口 UIA 元素树（只读，不激活、不移动光标）。"""
    try:
        import uiautomation as auto
    except ImportError:
        return {"ok": False, "reason": "缺少 uiautomation 库"}
    try:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return {"ok": False, "reason": "该窗口没有可访问的 UIA 树"}
        flat: list = []

        def walk(c, depth):
            if len(flat) >= max_elements or depth > max_depth:
                return
            flat.append(c)
            try:
                kids = c.GetChildren()
            except Exception:
                kids = []
            for k in kids:
                walk(k, depth + 1)

        walk(root, 0)
        _FLAT_CACHE[hwnd] = flat
        out = []
        for i, c in enumerate(flat):
            try:
                box = c.BoundingRectangle
                geo = [box.left, box.top, box.right, box.bottom]
            except Exception:
                geo = None
            try:
                pats = _patterns(c)
            except Exception:
                pats = []
            out.append({"i": i, "type": c.ControlTypeName, "name": (c.Name or "")[:80],
                        "id": (c.AutomationId or "")[:40], "class": (c.ClassName or "")[:40],
                        "box": geo, "patterns": pats})
        return {"ok": True, "count": len(flat), "title": window_title(hwnd), "elements": out}
    except Exception as exc:
        return {"ok": False, "reason": f"UIA 读取失败：{type(exc).__name__}: {exc}"}


def resolve(hwnd: int, target: str):
    """把 target 字符串解析成 UIA 元素。

    支持：``#3`` 序号（上一次 inspect 的顺序）、``id:OKButton``、``name:保存``、
    ``type:Button``、``class:Edit``；裸字符串先当 Name，再退化为 AutomationId。
    """
    if not target:
        return None, "缺少 target"
    flat = _FLAT_CACHE.get(hwnd)
    if flat is None:
        r = inspect(hwnd)
        if not r.get("ok"):
            return None, r.get("reason", "无法读取元素树")
        flat = _FLAT_CACHE[hwnd]
    t = target.strip()
    if t.startswith("#"):
        try:
            idx = int(t[1:])
        except ValueError:
            return None, f"序号格式错误：{t}"
        if 0 <= idx < len(flat):
            return flat[idx], None
        return None, f"序号越界（共 {len(flat)} 个元素）"
    kind, _, val = t.partition(":")
    kind = kind.lower() if _ else "name"
    val = val if _ else t
    low = val.lower()

    def key(c):
        if kind == "id":
            return (c.AutomationId or "").lower()
        if kind == "type":
            return (c.ControlTypeName or "").lower()
        if kind == "class":
            return (c.ClassName or "").lower()
        return (c.Name or "").lower()

    for c in flat:
        try:
            if key(c) == low:
                return c, None
        except Exception:
            continue
    for c in flat:
        try:
            if low and low in key(c):
                return c, None
        except Exception:
            continue
    return None, f"没找到匹配 {kind}={val!r} 的元素；先 inspect 看清单"


def _client_pt(hwnd: int, x: int, y: int) -> tuple[int, int]:
    """屏幕坐标 -> 该窗口客户区坐标（窗口原点含标题栏/边框，不能直接相减）。"""
    p = wt.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(p))
    return p.x, p.y


def _child_hwnd_at(hwnd: int, x: int, y: int) -> int:
    """把屏幕坐标换算到父窗口客户区，返回应接收消息的最深子窗口。"""
    cx, cy = _client_pt(hwnd, x, y)
    pt = wt.POINT(cx, cy)
    try:
        child = user32.ChildWindowFromPointEx(hwnd, pt, 0x0001 | 0x0004)  # SKIPINVISIBLE|SKIPTRANSPARENT
    except Exception:
        child = 0
    return child or hwnd


def _focused_child(hwnd: int) -> int:
    """目标线程当前焦点控件（GetFocus 只对本线程有效，跨进程要用 GUIThreadInfo）。"""
    pid = wt.DWORD()
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                    ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                    ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]

    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    try:
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
            return info.hwndFocus
    except Exception:
        pass
    return hwnd


def _class_of(hwnd: int) -> str:
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def pick(hwnd: int, x: int, y: int, button: str = "left", double: bool = False) -> dict:
    """定向坐标点击：消息直接投给目标窗口，真实光标不动、焦点不抢。"""
    th = _child_hwnd_at(hwnd, x, y)
    cx, cy = _client_pt(th, x, y)
    cls = _class_of(th).upper()
    down = {"left": WM_LBUTTONDOWN, "right": WM_RBUTTONDOWN, "middle": WM_MBUTTONDOWN}[button]
    up = {"left": WM_LBUTTONUP, "right": WM_RBUTTONUP, "middle": WM_MBUTTONUP}[button]
    wp = {"left": MK_LBUTTON, "right": 0x0002, "middle": 0x0010}[button]
    user32.PostMessageW(th, WM_MOUSEMOVE, 0, _lp(cx, cy))
    for _ in range(2 if double else 1):
        user32.PostMessageW(th, down, wp, _lp(cx, cy))
        time.sleep(0.03)
        user32.PostMessageW(th, up, 0, _lp(cx, cy))
        time.sleep(0.05)
    hint = ""
    if "BUTTON" in cls and not any(k in cls for k in ("EDIT",)):
        user32.PostMessageW(th, BM_CLICK, 0, 0)
        hint = "；按钮类控件已追加 BM_CLICK"
    return {"ok": True, "hwnd": th, "class": cls, "client": [cx, cy],
            "note": "非抢占点击（未移动真实光标）" + hint}


def fill(hwnd: int, target: str, text: str) -> dict:
    """零键盘写入：优先 UIA ValuePattern，兜底 WM_SETTEXT。"""
    ctrl, err = resolve(hwnd, target)
    if err:
        return {"ok": False, "reason": err}
    import uiautomation as auto
    try:
        p = ctrl.GetPattern(auto.PatternId.ValuePattern)
        if p is not None:
            if getattr(p, "IsReadOnly", False):
                return {"ok": False, "reason": "该控件只读，ValuePattern 不可写",
                        "type": ctrl.ControlTypeName}
            p.SetValue(text)
            time.sleep(0.05)
            return {"ok": True, "via": "ValuePattern", "readback": p.Value,
                    "name": ctrl.Name, "note": "未产生任何按键事件"}
    except Exception as exc:
        last = f"{type(exc).__name__}: {exc}"
    else:
        last = "无 ValuePattern"
    th = ctrl.NativeWindowHandle
    if th:
        buf = ctypes.create_unicode_buffer(text)
        if user32.SendMessageW(th, WM_SETTEXT, 0, ctypes.byref(buf)):
            return {"ok": True, "via": "WM_SETTEXT", "note": "定向窗口消息，未占用键盘"}
    return {"ok": False, "reason": f"{last}，且 WM_SETTEXT 失败"}


def invoke(hwnd: int, target: str) -> dict:
    """零输入触发元素：按 Invoke → Toggle → ExpandCollapse → Select → 定向点击 依次尝试。"""
    ctrl, err = resolve(hwnd, target)
    if err:
        return {"ok": False, "reason": err}
    import uiautomation as auto
    label = f"{ctrl.ControlTypeName} name={ctrl.Name!r} id={ctrl.AutomationId!r}"
    for pat_name, method in (("InvokePattern", "Invoke"),
                             ("TogglePattern", "Toggle"),
                             ("ExpandCollapsePattern", "Expand"),
                             ("SelectionItemPattern", "Select")):
        try:
            p = ctrl.GetPattern(getattr(auto.PatternId, pat_name))
        except Exception:
            p = None
        if p is not None:
            try:
                getattr(p, method)()
                time.sleep(0.08)
                return {"ok": True, "via": f"{pat_name}.{method}", "target": label,
                        "note": "未移动光标、未注入按键、未切换前台窗口"}
            except Exception as exc:
                return {"ok": False, "reason": f"{pat_name}.{method} 失败：{type(exc).__name__}: {exc}",
                        "target": label}
    th = ctrl.NativeWindowHandle
    if th and "BUTTON" in _class_of(th).upper():
        user32.PostMessageW(th, BM_CLICK, 0, 0)
        time.sleep(0.05)
        return {"ok": True, "via": "BM_CLICK", "target": label, "hwnd": th,
                "note": "定向窗口消息，未移动光标"}
    box = getattr(ctrl, "BoundingRectangle", None)
    if box is not None:
        return pick(hwnd, (box.left + box.right) // 2, (box.top + box.bottom) // 2)
    return {"ok": False, "reason": f"该元素不支持任何可触发 pattern：{label}"}


_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "shift": 0x10,
    "win": 0x5B, "meta": 0x5B, "capslock": 0x14, "printscreen": 0x2C,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def sendkey(hwnd: int, combo: str) -> dict:
    """定向按键：消息只投给指定窗口，系统键盘队列不受影响。"""
    parts = [p.strip().lower() for p in combo.replace("+", " ").split() if p.strip()]
    if not parts:
        return {"ok": False, "reason": "空按键"}
    keys = []
    for p in parts:
        if p in _VK:
            keys.append(_VK[p])
        elif len(p) == 1:
            keys.append(ord(p.upper()))
        else:
            return {"ok": False, "reason": f"不支持的按键 {p!r}"}
    mods = [k for k in keys if k in (0x10, 0x11, 0x12, 0x5B)]
    main = [k for k in keys if k not in mods]
    th = _focused_child(hwnd)
    for m in mods:
        user32.PostMessageW(th, WM_KEYDOWN, m, 0)
        time.sleep(0.01)
    for k in main:
        user32.PostMessageW(th, WM_KEYDOWN, k, 0)
        time.sleep(0.01)
        if 0x20 <= k <= 0x7E:
            user32.PostMessageW(th, WM_CHAR, ord(chr(k).lower()), 0)
        user32.PostMessageW(th, WM_KEYUP, k, 0)
        time.sleep(0.01)
    for m in reversed(mods):
        user32.PostMessageW(th, WM_KEYUP, m, 0)
    return {"ok": True, "hwnd": th, "combo": combo,
            "note": "定向投递，未使用 SendInput/keybd_event"}


def scroll(hwnd: int, x: int, y: int, amount: int) -> dict:
    """定向滚轮：不移动光标。amount 正数向上、负数向下。"""
    delta = int(120 * (1 if amount > 0 else -1) * min(3, abs(amount) or 1))
    wp = (delta & 0xFFFF) << 16
    th = _child_hwnd_at(hwnd, x, y)
    for _ in range(min(3, abs(amount) or 1)):
        user32.PostMessageW(th, WM_MOUSEWHEEL, wp, _lp(x, y))
        time.sleep(0.03)
    return {"ok": True, "hwnd": th, "notches": amount, "note": "未移动真实光标"}


def input_state() -> dict:
    """返回真实光标与前台窗口，用于向用户证明输入通道未被占用。"""
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    fg = user32.GetForegroundWindow()
    return {"cursor": [p.x, p.y], "foreground": fg, "foreground_title": window_title(fg) if fg else ""}
