"""
Computer Use 模块。
提供精确的鼠标、键盘、拖拽操作，让 Agent 能够像人类一样精确操控电脑。
这些命令作为 BrowserTool 的扩展命令集成。
"""
import base64
import ctypes
import os
import platform as _platform
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.base import BaseTool, ToolResult


class ComputerUseMixin:
    """
    Computer Use 混入类，为 BrowserTool 添加精确鼠标/键盘操作能力。
    
    设计原则：
    - 坐标系统：相对于视口的像素坐标，(0,0) 为左上角
    - 所有操作都是"一步到位"，Agent 描述意图，模块执行
    - 支持拖拽、组合键、精确定位点击等高级操作
    """

    # ================================================================
    # 鼠标精确操作
    # ================================================================

    def _mousemove(self, args: str) -> ToolResult:
        """
        移动鼠标到指定坐标。
        格式: mousemove <x> <y>
        """
        coords = self._parse_coords(args)
        if coords is None:
            return ToolResult(success=False, output="", 
                error="格式: mousemove <x> <y>，例如: mousemove 500 300")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.mouse.move(coords[0], coords[1])
            return ToolResult(
                success=True,
                output=f"鼠标已移动到: ({coords[0]}, {coords[1]})"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"鼠标移动失败: {str(e)}")

    def _clickat(self, args: str) -> ToolResult:
        """
        在指定坐标处点击鼠标。
        格式: clickat <x> <y> [button]
        button: left(默认) / right / middle
        """
        coords, button = self._parse_coords_with_button(args)
        if coords is None:
            return ToolResult(success=False, output="", 
                error="格式: clickat <x> <y> [left|right|middle]，例如: clickat 500 300 right")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.mouse.click(coords[0], coords[1], button=button)
            return ToolResult(
                success=True,
                output=f"已{button}键点击: ({coords[0]}, {coords[1]})"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"坐标点击失败: {str(e)}")

    def _dblclickat(self, args: str) -> ToolResult:
        """
        在指定坐标处双击鼠标。
        格式: dblclickat <x> <y>
        """
        coords = self._parse_coords(args)
        if coords is None:
            return ToolResult(success=False, output="", 
                error="格式: dblclickat <x> <y>")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.mouse.dblclick(coords[0], coords[1])
            return ToolResult(
                success=True,
                output=f"已双击: ({coords[0]}, {coords[1]})"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"双击失败: {str(e)}")

    # ================================================================
    # 拖拽操作
    # ================================================================

    def _drag(self, args: str) -> ToolResult:
        """
        从起点拖拽到终点。
        格式: drag <x1> <y1> <x2> <y2> [steps]
        steps: 可选，移动步数（默认10，越多越平滑）
        示例: drag 100 200 500 600 20
        """
        parts = args.strip().split()
        if len(parts) < 4:
            return ToolResult(success=False, output="",
                error="格式: drag <起点x> <起点y> <终点x> <终点y> [步数]")
        
        try:
            x1, y1, x2, y2 = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
            steps = int(parts[4]) if len(parts) > 4 else 10
        except ValueError:
            return ToolResult(success=False, output="", error="坐标参数必须是数字。")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.mouse.move(x1, y1)
            time.sleep(0.1)
            self._page.mouse.down()
            time.sleep(0.05)
            
            # 平滑移动
            for i in range(1, steps + 1):
                t = i / steps
                cur_x = x1 + (x2 - x1) * t
                cur_y = y1 + (y2 - y1) * t
                self._page.mouse.move(cur_x, cur_y)
                time.sleep(0.02)
            
            time.sleep(0.05)
            self._page.mouse.up()
            
            return ToolResult(
                success=True,
                output=f"已拖拽: ({x1:.0f}, {y1:.0f}) -> ({x2:.0f}, {y2:.0f})，共 {steps} 步"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"拖拽失败: {str(e)}")

    # ================================================================
    # 键盘组合键
    # ================================================================

    def _keycombo(self, args: str) -> ToolResult:
        """
        按下键盘组合键。
        格式: keycombo <键1>+<键2>[+<键3>]
        示例: keycombo Control+A   (全选)
              keycombo Control+C   (复制)
              keycombo Control+V   (粘贴)
              keycombo Shift+Tab   (反向切换)
              keycombo Alt+F4      (关闭窗口)
        支持: Control/Shift/Alt/Meta + 任意键
        """
        args = args.strip()
        if not args:
            return ToolResult(success=False, output="", 
                error="格式: keycombo <组合键>，例如: keycombo Control+C")
        
        # 支持多种分隔符：+ / 空格
        import re
        keys = re.split(r'[\s\+]+', args)
        if len(keys) < 2:
            return ToolResult(success=False, output="",
                error="组合键至少需要两个键，例如: keycombo Control+C")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            # 标准化键名
            key_map = {
                "ctrl": "Control", "control": "Control",
                "shift": "Shift", "alt": "Alt",
                "meta": "Meta", "cmd": "Meta", "command": "Meta",
                "win": "Meta", "windows": "Meta",
                "enter": "Enter", "return": "Enter",
                "esc": "Escape", "escape": "Escape",
                "tab": "Tab", "space": " ",
                "backspace": "Backspace", "delete": "Delete",
                "up": "ArrowUp", "down": "ArrowDown",
                "left": "ArrowLeft", "right": "ArrowRight",
            }
            
            normalized_keys = []
            for k in keys:
                k_lower = k.strip().lower()
                normalized_keys.append(key_map.get(k_lower, k.strip()))
            
            # 按下修饰键
            modifiers = {"Control", "Shift", "Alt", "Meta"}
            for k in normalized_keys:
                if k in modifiers:
                    self._page.keyboard.down(k)
            
            # 按下并释放主键
            main_keys = [k for k in normalized_keys if k not in modifiers]
            if main_keys:
                for k in main_keys:
                    self._page.keyboard.press(k)
                time.sleep(0.05)
            
            # 释放修饰键
            for k in normalized_keys:
                if k in modifiers:
                    self._page.keyboard.up(k)
            
            return ToolResult(
                success=True,
                output=f"已触发组合键: {'+'.join(normalized_keys)}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"组合键失败: {str(e)}")

    # ================================================================
    # 鼠标滚轮
    # ================================================================

    def _mousescroll(self, args: str) -> ToolResult:
        """
        在指定位置滚动鼠标滚轮。
        格式: mousescroll <x> <y> <delta_x> <delta_y>
        delta_y 正数向下，负数向上
        示例: mousescroll 500 400 0 -300   (在500,400处向上滚300)
        """
        parts = args.strip().split()
        if len(parts) < 2:
            return ToolResult(success=False, output="",
                error="格式: mousescroll <x> <y> <delta_x> <delta_y>")
        
        try:
            x, y = float(parts[0]), float(parts[1])
            dx = float(parts[2]) if len(parts) > 2 else 0
            dy = float(parts[3]) if len(parts) > 3 else -300
        except ValueError:
            dx, dy = 0, -300
            try:
                x, y = float(parts[0]), float(parts[1])
            except ValueError:
                return ToolResult(success=False, output="", error="坐标参数必须是数字。")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.mouse.move(x, y)
            time.sleep(0.05)
            self._page.mouse.wheel(dx, dy)
            return ToolResult(
                success=True,
                output=f"已在({x:.0f}, {y:.0f})处滚动: delta=({dx}, {dy})"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"鼠标滚轮失败: {str(e)}")

    # ================================================================
    # 直接输入（不需要选择器）
    # ================================================================

    def _type_direct(self, args: str) -> ToolResult:
        """
        直接在当前焦点位置输入文本（无需选择器）。
        格式: typedirect <文本>
        适用场景：已经点击或聚焦到输入框后直接输入。
        """
        args = args.strip()
        if not args:
            return ToolResult(success=False, output="", error="输入文本为空。")
        
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        
        try:
            self._page.keyboard.type(args, delay=50)
            return ToolResult(success=True, output=f"已输入: {args}")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"输入失败: {str(e)}")

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _parse_coords(args: str) -> Optional[tuple]:
        """解析坐标参数: x y"""
        parts = args.strip().split()
        if len(parts) < 2:
            return None
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            return None

    @staticmethod
    def _parse_coords_with_button(args: str) -> tuple:
        """解析坐标参数，支持可选按钮类型: x y [button]"""
        parts = args.strip().split()
        if len(parts) < 2:
            return (None, "left")
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            return (None, "left")
        
        button = "left"
        if len(parts) > 2:
            btn = parts[2].lower()
            valid_buttons = {"left", "right", "middle"}
            if btn in valid_buttons:
                button = btn

        return ((x, y), button)


# ================================================================
# 桌面操控工具（DesktopTool）：真·操作系统级电脑操控
# （区别于上方的 ComputerUseMixin——那个只作用于 Playwright 浏览器页面内）
#
# 与前沿 agent 的 Computer Use 能力对齐，三件套：
# 1. screenshot —— 全屏截图 → 视觉模型分析（PIL ImageGrab + models.vision）
# 2. a11y       —— 前台窗口无障碍树（Windows UIA，uiautomation 包），
#                  拿到可交互元素的类型/名称/坐标，语义定位不靠猜坐标
# 3. 鼠标/键盘  —— OS 级输入（keybd_event/mouse_event），type 支持中文
#
# 安全：risk_level=high + approval=on-request（ask 模式逐次弹审批卡片）；
#       min_sandbox_mode=danger-full-access（AppContainer 沙箱内无法操控 GUI）。
# ================================================================

_COMPUTER_SUPPORTED = _platform.system() == "Windows"

# Windows 虚拟键码（key 动作支持的键名 → VK）
_VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B,
}
for _i in range(1, 13):
    _VK_MAP[f"f{_i}"] = 0x6F + _i          # F1=0x70
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_MAP[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK_MAP[_d] = ord(_d)

_MOUSE_DOWN_UP = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


def _user32():
    import ctypes
    return ctypes.windll.user32


def _os_click(x: int, y: int, button: str = "left", double: bool = False) -> bool:
    """OS 级鼠标点击：SetCursorPos + mouse_event down/up。"""
    u = _user32()
    if not u.SetCursorPos(int(x), int(y)):
        return False
    time.sleep(0.03)
    down, up = _MOUSE_DOWN_UP.get(button, _MOUSE_DOWN_UP["left"])
    for _ in range(2 if double else 1):
        u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        u.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.02)
    return True


def _os_type_text(text: str) -> bool:
    """OS 级键盘输入（KEYEVENTF_UNICODE，支持中文等任意字符）。"""
    u = _user32()
    for ch in text:
        if ch == "\n":
            u.keybd_event(0x0D, 0, 0, 0)
            u.keybd_event(0x0D, 0, _KEYEVENTF_KEYUP, 0)
            continue
        code = ord(ch)
        if code > 0xFFFF:      # 代理对外的字符暂不支持（极少见）
            continue
        u.keybd_event(0, code, _KEYEVENTF_UNICODE, 0)
        u.keybd_event(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0)
        time.sleep(0.005)
    return True


def _os_key_combo(combo: str) -> Optional[List[int]]:
    """按下组合键（如 ctrl+s）。返回按下的 VK 列表；键名非法返回 None。"""
    keys = [k.strip().lower() for k in combo.split("+") if k.strip()]
    if not keys:
        return None
    vks = []
    for k in keys:
        if k not in _VK_MAP:
            return None
        vks.append(_VK_MAP[k])
    u = _user32()
    for vk in vks:
        u.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
    for vk in reversed(vks):
        u.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    return vks


def _os_scroll(x: int, y: int, notches: int) -> bool:
    """滚轮：notches 正=向上。"""
    u = _user32()
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    u.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(120 * notches), 0)
    return True


def _os_windows() -> List[Tuple[int, str]]:
    """可见顶层窗口列表 [(hwnd, title)]。"""
    import ctypes

    u = _user32()
    result: List[Tuple[int, str]] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                result.append((hwnd, buf.value))
        return True

    u.EnumWindows(proto(_cb), 0)
    return result


def _os_activate(title_substr: str) -> Optional[str]:
    """按标题模糊匹配并激活窗口，返回匹配到的标题。"""
    u = _user32()
    for hwnd, title in _os_windows():
        if title_substr.lower() in title.lower():
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
            try:
                u.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.2)
            return title
    return None


def _active_window_title() -> str:
    u = _user32()
    hwnd = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _take_screenshot() -> Optional[str]:
    """全屏截图保存为 PNG，返回文件路径（落在工作区 generated_images/computer/）。"""
    from PIL import ImageGrab

    try:
        from config import TOOL_CONFIG
        out_dir = TOOL_CONFIG.get("computer_screenshot_dir") or os.path.join(
            "generated_images", "computer")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"screen_{int(time.time() * 1000)}.png")
        ImageGrab.grab().save(path)
        return path
    except Exception:
        return None


def _a11y_tree(max_elements: int = 120, max_depth: int = 8) -> str:
    """前台窗口的 UIA 无障碍树（类型/名称/坐标，供模型语义定位）。"""
    try:
        import uiautomation as auto
    except ImportError:
        return ("（uiautomation 未安装，无障碍树不可用。"
                "安装：pip install uiautomation；或改用 screenshot + 坐标）")

    ctrl = auto.GetForegroundControl()
    if ctrl is None:
        return "（无法获取前台窗口）"
    lines = [f"前台窗口: {(ctrl.Name or '').strip()[:60]} [{ctrl.ControlTypeName}]"]
    count = 0

    def walk(c, depth):
        nonlocal count
        if count >= max_elements or depth > max_depth:
            return
        try:
            r = c.BoundingRectangle
            name = (c.Name or "").strip()[:40]
            type_name = c.ControlTypeName
            short = type_name[:-8] if type_name.endswith("Control") else type_name
            enabled = "" if c.IsEnabled else " [禁用]"
            interactive = short in ("Button", "Edit", "Hyperlink", "MenuItem", "CheckBox",
                                    "ComboBox", "ListItem", "TabItem", "Document", "List",
                                    "Tree", "TreeItem", "DataItem", "Slider", "RadioButton")
            if name or interactive:
                rect = f"({r.left},{r.top})-({r.right},{r.bottom})"
                lines.append(f"{'  ' * depth}- {short} '{name}' {rect}{enabled}")
                count += 1
            for child in c.GetChildren():
                walk(child, depth + 1)
        except Exception:
            pass

    walk(ctrl, 1)
    lines.append(f"（共 {count} 个元素；坐标为屏幕绝对坐标，可直接用于 click）")
    return "\n".join(lines)


_COMPUTER_SCREEN_QUESTION = """请分析这张电脑屏幕截图，并提供：
1. 当前前台应用和界面状态（窗口/弹窗/加载中/报错）
2. 可见的主要 UI 元素及位置（按钮/输入框/菜单，给出大概坐标）
3. 如果在做任务：当前状态与目标的差距、建议下一步操作
用中文回答，简洁明了。"""


class DesktopTool(BaseTool):
    """桌面操控工具：截图理解 + 无障碍树 + OS 级鼠标键盘，操作真实电脑。"""

    risk_level: str = "high"              # 操控真实电脑，高危
    approval: str = "on-request"          # ask 模式逐次弹审批卡片
    min_sandbox_mode: str = "danger-full-access"  # 沙箱内无法操控 GUI

    def __init__(self, vision_model=None):
        self._vision_model = vision_model

    @property
    def name(self) -> str:
        return "computer"

    @property
    def description(self) -> str:
        return (
            "桌面操控工具：像人一样操作电脑（区别于 browser——那个只管浏览器页面）。\n"
            "能力：screenshot（截图给视觉模型分析）、a11y（读前台窗口无障碍树，拿到"
            "按钮/输入框的类型、名称和精确坐标）、click/type/key/scroll（OS 级鼠标键盘，"
            "type 支持中文）、window（列出/激活窗口）。\n"
            "用法建议：先 a11y 或 screenshot 看清界面 → 用树里/截图里的坐标 click → "
            "type 输入 → 再 screenshot 验证效果。操作的目标窗口最好先用 window 动作激活。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "a11y", "click", "type", "key", "scroll", "window"],
                    "description": "screenshot=截图分析; a11y=读前台窗口元素树; click=点击; "
                                   "type=输入文本; key=按组合键; scroll=滚轮; window=列出/激活窗口",
                },
                "x": {"type": "number", "description": "屏幕横坐标（click/scroll 用，屏幕绝对坐标）"},
                "y": {"type": "number", "description": "屏幕纵坐标"},
                "button": {"type": "string", "enum": ["left", "right", "middle"],
                           "description": "click 的鼠标键（默认 left）"},
                "double": {"type": "boolean", "description": "click 是否双击（默认 false）"},
                "text": {"type": "string", "description": "type 要输入的文本（支持中文，\\n=回车）"},
                "combo": {"type": "string", "description": "key 的组合键，如 ctrl+s / alt+tab / enter"},
                "notches": {"type": "number", "description": "scroll 滚动格数（正=向上，默认 3）"},
                "title": {"type": "string", "description": "window 动作的窗口标题（模糊匹配；空=列出全部）"},
                "question": {"type": "string", "description": "screenshot 附加给视觉模型的具体问题"},
            },
            "required": ["action"],
        }

    def execute(self, input_str: str) -> ToolResult:
        """旧字符串接口：整串当 JSON 解析；解析失败退回 a11y 感知动作。"""
        s = (input_str or "").strip()
        if s:
            try:
                import json
                args = json.loads(s)
                if isinstance(args, dict):
                    return self.execute_json(args)
            except Exception:
                pass
        return self.execute_json({"action": "a11y"})

    def _get_vision_model(self):
        if self._vision_model is not None:
            return self._vision_model
        try:
            from models.vision import VisionModel
            self._vision_model = VisionModel()
        except Exception:
            self._vision_model = None
        return self._vision_model

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        if not _COMPUTER_SUPPORTED:
            return ToolResult(success=False, output="",
                              error="computer 工具目前仅支持 Windows。")
        action = str(arguments.get("action", "")).strip().lower()
        handlers = {
            "screenshot": self._act_screenshot,
            "a11y": self._act_a11y,
            "click": self._act_click,
            "type": self._act_type,
            "key": self._act_key,
            "scroll": self._act_scroll,
            "window": self._act_window,
        }
        handler = handlers.get(action)
        if handler is None:
            return ToolResult(success=False, output="",
                              error=f"未知 action: {action}。可用: {', '.join(handlers)}")
        try:
            return handler(arguments)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"computer {action} 失败: {str(e)[:200]}")

    def _act_screenshot(self, args: Dict[str, Any]) -> ToolResult:
        path = _take_screenshot()
        if not path:
            return ToolResult(success=False, output="", error="截图失败（ImageGrab 不可用？）")
        title = _active_window_title()
        question = str(args.get("question", "") or "").strip() or _COMPUTER_SCREEN_QUESTION
        vision = self._get_vision_model()
        if vision is None:
            return ToolResult(success=True, output=(
                f"截图已保存: {path}\n前台窗口: {title or '（未知）'}\n"
                "（视觉模型不可用，无法分析内容；建议改用 a11y 动作读取界面元素）"))
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            analysis = vision.analyze(b64, question, max_tokens=1500)
        except Exception as e:
            return ToolResult(success=True, output=(
                f"截图已保存: {path}\n前台窗口: {title or '（未知）'}\n"
                f"（视觉分析失败: {str(e)[:120]}；建议改用 a11y 动作）"))
        return ToolResult(success=True, output=(
            f"前台窗口: {title or '（未知）'}\n【屏幕分析】\n{analysis}\n[截图: {path}]"))

    def _act_a11y(self, args: Dict[str, Any]) -> ToolResult:
        try:
            from config import TOOL_CONFIG
            max_elements = int(TOOL_CONFIG.get("computer_a11y_max_elements", 120))
            max_depth = int(TOOL_CONFIG.get("computer_a11y_max_depth", 8))
        except Exception:
            max_elements, max_depth = 120, 8
        tree = _a11y_tree(max_elements=max_elements, max_depth=max_depth)
        return ToolResult(success=True, output=tree)

    def _act_click(self, args: Dict[str, Any]) -> ToolResult:
        try:
            x, y = int(float(args.get("x"))), int(float(args.get("y")))
        except (TypeError, ValueError):
            return ToolResult(success=False, output="",
                              error="click 需要 x/y 屏幕坐标（从 a11y 树或截图获取）。")
        button = str(args.get("button", "left") or "left").lower()
        if button not in _MOUSE_DOWN_UP:
            button = "left"
        ok = _os_click(x, y, button=button, double=bool(args.get("double")))
        if not ok:
            return ToolResult(success=False, output="", error=f"点击失败（坐标 {x},{y}）")
        return ToolResult(success=True, output=f"已{'双击' if args.get('double') else '点击'} ({x},{y})"
                                              f"{' 右键' if button == 'right' else ''}")

    def _act_type(self, args: Dict[str, Any]) -> ToolResult:
        text = str(args.get("text", ""))
        if not text:
            return ToolResult(success=False, output="", error="type 需要 text 参数。")
        _os_type_text(text)
        return ToolResult(success=True, output=f"已输入 {len(text)} 个字符（输入到当前焦点控件）。")

    def _act_key(self, args: Dict[str, Any]) -> ToolResult:
        combo = str(args.get("combo", "")).strip()
        vks = _os_key_combo(combo) if combo else None
        if not vks:
            return ToolResult(success=False, output="",
                              error=f"key 需要合法 combo（如 ctrl+s / enter / alt+f4），收到: {combo!r}")
        return ToolResult(success=True, output=f"已按组合键 {combo}。")

    def _act_scroll(self, args: Dict[str, Any]) -> ToolResult:
        import ctypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        try:
            x = int(float(args.get("x")))
            y = int(float(args.get("y")))
        except (TypeError, ValueError):
            p = _PT()
            _user32().GetCursorPos(ctypes.byref(p))
            x, y = p.x, p.y
        notches = args.get("notches", 3)
        try:
            notches = int(float(notches))
        except (TypeError, ValueError):
            notches = 3
        _os_scroll(x, y, notches)
        return ToolResult(success=True, output=f"已在 ({x},{y}) 向{'上' if notches > 0 else '下'}滚动 {abs(notches)} 格。")

    def _act_window(self, args: Dict[str, Any]) -> ToolResult:
        title = str(args.get("title", "") or "").strip()
        if not title:
            wins = _os_windows()
            listing = "\n".join(f"- {t}" for _, t in wins[:40]) or "（无可见窗口）"
            return ToolResult(success=True, output=f"可见窗口:\n{listing}")
        matched = _os_activate(title)
        if not matched:
            return ToolResult(success=False, output="",
                              error=f"没有标题包含 {title!r} 的可见窗口（先 window 不带 title 列出）。")
        return ToolResult(success=True, output=f"已激活窗口: {matched}")
