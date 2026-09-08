"""
内嵌浏览器工具：Agent 的 browser 命令转发到桌面端侧栏的 Electron <webview>。

桌面端模式下，Electron 主进程会给后端注入环境变量
MY_AGENT_EMBEDDED_BROWSER_URL（HTTP 桥地址，仅 127.0.0.1 可达），
ToolManager 检测到后用本工具替代独立 Playwright 浏览器：
Agent 操控的页面就是用户在侧栏「浏览器」标签里看到的页面——所见即所控。

- 命令协议与 BrowserTool 完全一致（对 Agent 透明，提示词无需改动）；
- 不支持多标签/弹窗类命令（单标签 webview），返回引导性错误；
- 桥不可达时（纯 CLI 场景误启用）报错并提示检查桌面端是否在运行。
"""
import json as _json
import time
import urllib.request

from tools.base import ToolResult
from tools.browser import BrowserTool
from config import BROWSER_CONFIG

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8091/browser"


def probe_bridge(timeout: float = 0.8) -> bool:
    """探测桌面端内嵌浏览器桥是否在线（健康检查，快速失败）。

    ToolManager 建浏览器工具时调用：桥在线 → 一律用内嵌浏览器，
    外部 Playwright 浏览器被禁用；离线（纯 CLI）→ 才允许外部浏览器。
    """
    base = (BROWSER_CONFIG.get("embedded_url") or DEFAULT_BRIDGE_URL).rstrip("/")
    health = base.rsplit("/", 1)[0] + "/health"
    try:
        import urllib.error  # noqa: F401
        with urllib.request.urlopen(health, timeout=timeout) as resp:
            return bool(_json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


class EmbeddedBrowserTool(BrowserTool):
    """桥接桌面端内嵌 webview 的 browser 工具（命令协议同 BrowserTool）。"""

    # 桥接支持的命令子集（其余命令给出明确错误，不静默失败）
    _BRIDGE_COMMANDS = {
        "launch", "open", "close", "status", "tabs", "switch",
        "goto", "back", "forward", "refresh",
        "title", "url", "text", "html", "snapshot", "click", "type", "fill",
        "press", "scroll", "js", "wait",
        "screenshot", "screenshot_base64",
        "mousemove", "clickat", "dblclickat", "mousescroll", "keycombo",
        "typedirect", "visionclick",
    }

    def __init__(self, bridge_url: str = None):
        super().__init__(headless=True, persistent=False)
        self._bridge_url = (
            bridge_url
            or BROWSER_CONFIG.get("embedded_url")
            or "http://127.0.0.1:8091/browser"
        ).rstrip("/")

    # ================================================================
    # 桥通信
    # ================================================================

    def _call_bridge(self, action: str, **params) -> dict:
        """POST 一条动作到 Electron 桥，返回 {ok, output?/base64?, error?}。"""
        payload = _json.dumps({"action": action, **params}).encode("utf-8")
        req = urllib.request.Request(
            self._bridge_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {
                "ok": False,
                "error": (
                    f"内嵌浏览器桥不可达（{str(e)[:120]}）。"
                    "请确认桌面端正在运行（桥端口 8091）。"
                ),
            }

    def _to_result(self, resp: dict) -> ToolResult:
        if resp.get("ok"):
            return ToolResult(success=True, output=str(resp.get("output", "")))
        return ToolResult(success=False, output="", error=str(resp.get("error", "桥调用失败")))

    # ================================================================
    # 并行安全：只读命令才可并行
    # ================================================================

    def is_parallel_safe(self, arguments) -> bool:
        cmd = str(arguments.get("command", "") or "").strip().lower().split()
        readonly = {"status", "title", "url", "text", "html", "snapshot",
                    "screenshot", "screenshot_base64"}
        return bool(cmd) and cmd[0] in readonly

    # ================================================================
    # 命令路由（覆盖基类：Playwright → HTTP 桥）
    # ================================================================

    def execute(self, input_str: str) -> ToolResult:
        parts = input_str.strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="", error="浏览器命令为空。")
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command == "wait":
            return self._wait(args)
        if command not in self._BRIDGE_COMMANDS:
            return ToolResult(
                success=False, output="",
                error=(
                    f"内嵌浏览器不支持命令 '{command}'（单标签页、无弹窗/多Tab）。"
                    f"支持: {', '.join(sorted(self._BRIDGE_COMMANDS))}"
                ),
            )

        try:
            return self._dispatch(command, args)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"内嵌浏览器操作失败: {str(e)}")

    def _dispatch(self, command: str, args: str) -> ToolResult:
        if command == "launch":
            # args 里可能带初始 URL（多标签：open 会新建 tab 或切到已开页面）
            return self._to_result(self._call_bridge("open", url=args.strip()))
        if command == "open":
            if not args.strip():
                return ToolResult(success=False, output="", error="格式: open <URL>")
            return self._to_result(self._call_bridge("open", url=args.strip()))
        if command == "tabs":
            # 列出所有已开标签（含索引/标题/URL）
            return self._to_result(self._call_bridge("tabs"))
        if command == "switch":
            if not args.strip():
                return ToolResult(success=False, output="", error="格式: switch <标签索引|tabId|URL片段>（如 switch 0）")
            return self._to_result(self._call_bridge("switch", index=args.strip()))
        if command == "close":
            return self._to_result(self._call_bridge("close"))
        if command == "goto":
            url = self._normalize_url(args)
            if not url:
                return ToolResult(success=False, output="", error="URL 为空。")
            return self._to_result(self._call_bridge("navigate", url=url))
        if command in ("back", "forward", "refresh", "title", "url", "status",
                       "text", "html", "snapshot"):
            return self._to_result(self._call_bridge(command))
        if command == "click":
            if not args.strip():
                return ToolResult(success=False, output="", error="选择器为空。")
            return self._to_result(self._call_bridge("click", selector=args.strip()))
        if command in ("type", "fill"):
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                return ToolResult(
                    success=False, output="",
                    error="输入格式: type <选择器> <文本>，例如: type #search 你好",
                )
            return self._to_result(self._call_bridge("type", selector=parts[0], text=parts[1]))
        if command == "press":
            return self._to_result(self._call_bridge("press", key=args.strip()))
        if command == "scroll":
            return self._scroll(args)
        if command == "js":
            if not args.strip():
                return ToolResult(success=False, output="", error="JS 代码为空。")
            return self._to_result(self._call_bridge("js", code=args))
        if command == "screenshot":
            return self._screenshot_via_bridge(keep_base64=False)
        if command == "screenshot_base64":
            return self._screenshot_via_bridge(keep_base64=True)
        if command == "visionclick":
            return self._vision_click(args)
        if command == "mousemove":
            x, y = self._coords(args)
            return self._to_result(self._call_bridge("mousemove", x=x, y=y))
        if command == "clickat":
            parts = args.split()
            if len(parts) < 2:
                return ToolResult(success=False, output="", error="格式: clickat <x> <y> [left|right|middle]")
            btn = parts[2] if len(parts) > 2 else "left"
            return self._to_result(self._call_bridge("clickat", x=float(parts[0]), y=float(parts[1]), button=btn))
        if command == "dblclickat":
            x, y = self._coords(args)
            return self._to_result(self._call_bridge("dblclickat", x=x, y=y))
        if command == "mousescroll":
            parts = args.split()
            x = float(parts[0]) if len(parts) > 0 else 0
            y = float(parts[1]) if len(parts) > 1 else 0
            dx = float(parts[2]) if len(parts) > 2 else 0
            dy = float(parts[3]) if len(parts) > 3 else 0
            return self._to_result(self._call_bridge("mousescroll", x=x, y=y, dx=dx, dy=dy))
        if command == "keycombo":
            return self._to_result(self._call_bridge("keycombo", keys=args.strip()))
        if command == "typedirect":
            return self._to_result(self._call_bridge("type-direct", text=args))
        return ToolResult(success=False, output="", error=f"未实现的命令: {command}")

    # ================================================================
    # 子命令实现
    # ================================================================

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        return "https://" + url

    @staticmethod
    def _coords(args: str) -> tuple:
        parts = args.split()
        if len(parts) < 2:
            raise ValueError("格式: <x> <y>")
        return float(parts[0]), float(parts[1])

    def _scroll(self, args: str) -> ToolResult:
        parts = args.strip().split()
        if not parts:
            return ToolResult(
                success=False, output="",
                error="滚动格式: scroll <方向> [像素]，例如: scroll down 300",
            )
        direction = parts[0].lower()
        pixels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 500
        if direction == "top":
            return self._to_result(self._call_bridge("js", code="window.scrollTo(0, 0); 'ok'"))
        if direction == "bottom":
            return self._to_result(self._call_bridge(
                "js", code="window.scrollTo(0, document.body.scrollHeight); 'ok'"))
        dy_map = {"down": pixels, "up": -pixels}
        dx_map = {"right": pixels, "left": -pixels}
        if direction in dy_map:
            return self._to_result(self._call_bridge("scroll", dx=0, dy=dy_map[direction]))
        if direction in dx_map:
            return self._to_result(self._call_bridge("scroll", dx=dx_map[direction], dy=0))
        return ToolResult(
            success=False, output="",
            error=f"未知滚动方向: '{direction}'。支持: down/up/left/right/top/bottom",
        )

    def _screenshot_via_bridge(self, keep_base64: bool) -> ToolResult:
        resp = self._call_bridge("screenshot")
        if not resp.get("ok"):
            return ToolResult(success=False, output="", error=str(resp.get("error", "截图失败")))
        status = self._call_bridge("status")
        header = status.get("output", "") if isinstance(status, dict) else ""
        b64 = str(resp.get("base64", ""))
        if keep_base64:
            return ToolResult(
                success=True,
                output=f"截图已获取（内嵌浏览器）。\n{header}\n"
                       f"大小: {len(b64) * 3 // 4} 字节\n"
                       f"[FULL_BASE64]{b64}[/FULL_BASE64]",
            )
        return ToolResult(success=True, output=f"截图已获取（内嵌浏览器）。\n{header}")

    def _vision_click(self, description: str) -> ToolResult:
        """视觉点击：桥截图 → 视觉模型定位 → 桥坐标点击。"""
        description = description.strip()
        if not description:
            return ToolResult(success=False, output="", error="元素描述为空。")
        shot = self._screenshot_via_bridge(keep_base64=True)
        if not shot.success:
            return shot
        b64 = shot.output.split("[FULL_BASE64]")[1].split("[/FULL_BASE64]")[0]
        from models.vision import VisionModel
        location = VisionModel().locate_element(b64, description)
        if not location.get("found"):
            hint = location.get("selector_hint", "")
            return ToolResult(
                success=False, output="",
                error=f"视觉模型未找到元素: '{description}'。{('提示: ' + hint) if hint else ''}",
            )
        x = float(location.get("x", 0))
        y = float(location.get("y", 0))
        resp = self._call_bridge("clickat", x=x, y=y, button="left")
        if not resp.get("ok"):
            return ToolResult(success=False, output="", error=str(resp.get("error", "点击失败")))
        time.sleep(0.3)
        return ToolResult(
            success=True,
            output=f"视觉驱动点击成功！\n元素: {description}\n点击位置: ({x:.0f}, {y:.0f})",
        )

    # ================================================================
    # 基类 Playwright 生命周期在此模式无意义，置空防误用
    # ================================================================

    def reset(self):
        pass

    def __del__(self):
        pass
