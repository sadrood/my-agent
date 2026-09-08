"""
浏览器工具模块（增强版）。
基于 Playwright 封装浏览器操作，让 Agent 像人一样操作网页。
新增：多Tab、键盘操作、弹窗处理、视觉驱动点击、JS执行。
"""
import os
import base64
import time
from typing import Optional

from tools.base import BaseTool, ToolResult
from tools.computer_use import ComputerUseMixin
from tools.intent_detector import get_intent_detector
from config import BROWSER_CONFIG


class BrowserTool(BaseTool, ComputerUseMixin):
    """
    浏览器操作工具（基于 Playwright）。

    设计原则：
    - Agent 只知道命令字符串，无需了解 Playwright 存在
    - 底层 Playwright 被完全封装
    - 支持视觉驱动操作：截图 → 分析 → 定位 → 操作
    """

    def __init__(
        self,
        headless: bool = None,
        screenshot_dir: str = "./screenshots",
        viewport_width: int = None,
        viewport_height: int = None,
        persistent: bool = None,
    ):
        self._headless = (
            headless
            if headless is not None
            else BROWSER_CONFIG.get("headless", False)
        )
        self._screenshot_dir = screenshot_dir
        self._viewport_width = viewport_width or BROWSER_CONFIG.get("viewport_width", 1280)
        self._viewport_height = viewport_height or BROWSER_CONFIG.get("viewport_height", 720)
        # 内置浏览器：持久 profile 模式（登录态跨次启动保留，网页型分身依赖）。
        # persistent 可显式传 False 创建独立临时 profile 实例（如侧栏辅助浏览器），
        # 避免与主任务浏览器争用同一 user-data-dir 锁。
        if persistent is not None:
            self._persistent = bool(persistent)
        else:
            self._persistent = bool(BROWSER_CONFIG.get("persistent", True))
        self._profile_dir = os.path.abspath(
            BROWSER_CONFIG.get("profile_dir", "./memory/browser_profile"))

        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: list = []        # 所有 page 列表
        self._current_page_idx = 0     # 当前活跃 page 索引

        os.makedirs(self._screenshot_dir, exist_ok=True)

    # ================================================================
    # 属性
    # ================================================================

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        return (
            "浏览器操作工具。支持以下命令：\n"
            "\n【生命周期】\n"
            "  launch              - 启动浏览器\n"
            "  close               - 关闭浏览器\n"
            "  status              - 查看浏览器状态\n"
            "\n【导航】\n"
            "  goto <url>          - 导航到指定网址\n"
            "  back                - 返回上一页\n"
            "  forward             - 前进到下一页\n"
            "  refresh             - 刷新页面\n"
            "\n【多Tab】\n"
            "  newtab [url]        - 打开新标签页（可选指定URL）\n"
            "  switchtab <序号>     - 切换到指定标签页（1-based）\n"
            "  closetab <序号>      - 关闭指定标签页\n"
            "  tabs                - 列出所有标签页\n"
            "\n【信息获取】\n"
            "  title               - 获取当前页面标题\n"
            "  url                 - 获取当前页面 URL\n"
            "  html                - 获取 HTML 源码（截断至8000字符）\n"
            "  text                - 提取页面可见文本\n"
            "  snapshot            - 页面结构快照（accessibility 树：按钮/输入框/链接的角色与文本，无需截图即可了解布局）\n"
            "  elementinfo <选择器>  - 获取元素信息（位置、大小、属性）\n"
            "\n【交互操作】\n"
            "  click <选择器>       - 点击指定元素（CSS选择器/文本/role）\n"
            "  visionclick <描述>   - 基于视觉描述智能点击（截图→分析→定位→点击）\n"
            "  type <选择器> <文本>  - 在输入框中输入文本\n"
            "  hover <选择器>       - 鼠标悬停到元素上\n"
            "  press <键名>         - 按下键盘按键（Enter/Escape/Tab/ArrowDown等）\n"
            "  scroll <方向> [像素]  - 滚动页面\n"
            "  js <代码>           - 在页面中执行 JavaScript\n"
            "\n【弹窗处理】\n"
            "  alert accept        - 接受弹窗（确定）\n"
            "  alert dismiss       - 拒绝弹窗（取消）\n"
            "  alert text          - 获取弹窗文字\n"
            "\n【截图】\n"
            "  screenshot          - 截取当前页面（保存到文件）\n"
            "  screenshot_base64   - 截取当前页面（返回完整base64，供视觉分析）\n"
            "\n【精确鼠标操作 Computer Use】\n"
            "  mousemove <x> <y>       - 移动鼠标到指定坐标\n"
            "  clickat <x> <y> [left|right|middle] - 在坐标处点击\n"
            "  dblclickat <x> <y>      - 在坐标处双击\n"
            "  drag <x1> <y1> <x2> <y2> [步数] - 拖拽从A到B\n"
            "  mousescroll <x> <y> [dx] [dy] - 在坐标处滚轮\n"
            "  keycombo <键1>+<键2>     - 组合键（如 Control+C）\n"
            "  typedirect <文本>      - 直接在当前焦点输入\n"
            "\n【其他】\n"
            "  wait <秒数>          - 等待指定秒数\n"
        )

    # ================================================================
    # v2：审批元数据 + JSON Schema 接口
    # ================================================================

    risk_level: str = "medium"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    # 只读类浏览器命令（read-only 沙箱下也允许）
    READONLY_COMMANDS = {
        "title", "url", "html", "text", "snapshot", "screenshot", "screenshot_base64",
        "tabs", "status", "elementinfo",
    }

    def is_parallel_safe(self, arguments):
        # 只读命令可并行（看页面/读快照）；导航/点击/输入等改动页面状态，保持串行
        cmd = str(arguments.get("command", "") or "").strip().lower().split()
        return bool(cmd) and cmd[0] in self.READONLY_COMMANDS

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "launch", "close", "status",
                        "goto", "back", "forward", "refresh",
                        "newtab", "switchtab", "closetab", "tabs",
                        "title", "url", "html", "text", "snapshot", "screenshot", "screenshot_base64",
                        "elementinfo",
                        "click", "visionclick", "type", "fill", "hover", "press", "scroll",
                        "js", "alert", "wait",
                        "mousemove", "clickat", "dblclickat", "drag", "keycombo",
                        "mousescroll", "typedirect",
                    ],
                    "description": "要执行的浏览器命令",
                },
                "args": {
                    "type": "string",
                    "description": "命令参数（如 URL、选择器、文本、坐标等，按需拼接）",
                },
            },
            "required": ["command"],
        }

    def execute_json(self, arguments):
        command = str(arguments.get("command", "")).strip()
        args = str(arguments.get("args", "") or "").strip()
        if args:
            return self.execute(f"{command} {args}")
        return self.execute(command)

    def build_approval_request(self, arguments):
        from agent.approval import ApprovalRequest
        command = str(arguments.get("command", "")).strip().lower()
        is_readonly = command in self.READONLY_COMMANDS or command.startswith("screenshot")
        is_close = command == "close"
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"browser {command} {arguments.get('args', '')}".strip(),
            risk_level="low" if (is_readonly or is_close) else "medium",
            min_sandbox_mode="read-only" if is_readonly else "workspace-write",
        )

    # ================================================================
    # 命令路由
    # ================================================================

    def execute(self, input_str: str) -> ToolResult:
        parts = input_str.strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="", error="浏览器命令为空。")

        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handlers = {
            "launch": self._launch,
            "goto": lambda a: self._goto(a),
            "title": lambda _: self._get_title(),
            "url": lambda _: self._get_current_url(),
            "html": lambda _: self._get_html(),
            "text": lambda _: self._get_text(),
            "snapshot": lambda _: self._accessibility_snapshot(),
            "screenshot": lambda _: self._screenshot(full_base64=False),
            "screenshot_base64": lambda _: self._screenshot(full_base64=True),
            "click": self._click,
            "visionclick": self._vision_click,
            "type": self._type_text,
            "fill": self._type_text,
            "hover": self._hover,
            "press": self._press_key,
            "scroll": self._scroll,
            "back": lambda _: self._go_back(),
            "forward": lambda _: self._go_forward(),
            "refresh": lambda _: self._refresh(),
            "wait": self._wait,
            "close": lambda _: self._close(),
            "status": lambda _: self._status(),
            "newtab": self._new_tab,
            "switchtab": self._switch_tab,
            "closetab": self._close_tab,
            "tabs": lambda _: self._list_tabs(),
            "alert": self._handle_alert,
            "js": self._execute_js,
            "elementinfo": self._element_info,
            # Computer Use 命令
            "mousemove": self._mousemove,
            "clickat": self._clickat,
            "dblclickat": self._dblclickat,
            "drag": self._drag,
            "keycombo": self._keycombo,
            "mousescroll": self._mousescroll,
            "typedirect": self._type_direct,
        }

        handler = handlers.get(command)
        if handler is None:
            available = ", ".join(handlers.keys())
            return ToolResult(
                success=False,
                output="",
                error=f"未知浏览器命令: '{command}'。可用命令: {available}",
            )

        try:
            return handler(args)
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"浏览器操作失败: {str(e)}"
            )

    # ================================================================
    # 生命周期管理
    # ================================================================

    def _launch(self, _args: str = "") -> ToolResult:
        if self._browser is not None and self._is_browser_alive():
            return ToolResult(success=True, output="浏览器已在运行中。")
        if self._persistent and self._context is not None and self._is_context_alive():
            return ToolResult(success=True, output="浏览器已在运行中（持久 profile）。")

        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            if self._persistent:
                # 持久模式：用户数据目录落盘，登录态（cookie/localStorage）跨次
                # 启动保留——网页型分身先 headed 手动登录一次，之后 agent 复用会话。
                # 返回值直接是 BrowserContext（没有独立 Browser 对象）。
                os.makedirs(self._profile_dir, exist_ok=True)
                self._browser = None
                self._context = self._playwright.chromium.launch_persistent_context(
                    self._profile_dir,
                    headless=self._headless,
                    viewport={"width": self._viewport_width, "height": self._viewport_height},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
            else:
                self._browser = self._playwright.chromium.launch(headless=self._headless)
                self._context = self._browser.new_context(
                    viewport={"width": self._viewport_width, "height": self._viewport_height},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
            page = self._context.new_page()
            self._pages = [page]
            self._current_page_idx = 0

            mode = "无头" if self._headless else "可视化"
            profile_note = f"，持久 profile: {self._profile_dir}" if self._persistent else ""
            return ToolResult(
                success=True,
                output=f"浏览器已启动（{mode}模式，视口 {self._viewport_width}x{self._viewport_height}{profile_note}）。",
            )
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="未安装 Playwright。请运行: pip install playwright && playwright install chromium",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"启动浏览器失败: {str(e)}")

    def _close(self, _args: str = "") -> ToolResult:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._context = None
            self._browser = None
            self._pages = []
            self._current_page_idx = 0
            self._playwright = None
        return ToolResult(success=True, output="浏览器已关闭。")

    def _ensure_page(self) -> ToolResult:
        if not self._pages or self._page is None or not self._is_browser_alive():
            result = self._launch()
            if not result.success:
                return result
        return ToolResult(success=True, output="")

    @property
    def _page(self):
        """获取当前活跃的 page 对象。"""
        if self._pages and 0 <= self._current_page_idx < len(self._pages):
            return self._pages[self._current_page_idx]
        return None

    # ================================================================
    # 页面结构快照（accessibility 树，无需截图即可"看到"页面布局）
    # ================================================================

    def _accessibility_snapshot(self, max_depth: int = 12,
                                max_nodes: int = 400) -> ToolResult:
        """
        返回当前页面的结构快照（对齐 Playwright MCP 的 browser_snapshot）：
        按钮/输入框/链接/标题等元素的角色与文本，带层级缩进与状态标注
        （required/disabled/checked/level 等）。模型据此了解页面布局并
        决定点击/填写目标，避免每次都截图走视觉模型。

        优先用新版 locator.aria_snapshot（Playwright ≥1.49 已取代
        page.accessibility）；旧版本回退 accessibility.snapshot 字典树。
        """
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure

        snapshot_text = None
        try:
            locator = self._page.locator("body")
            if hasattr(locator, "aria_snapshot"):
                snapshot_text = locator.aria_snapshot(depth=max_depth)
        except Exception:
            snapshot_text = None

        if snapshot_text is None:
            # 旧版本 Playwright 回退：accessibility.snapshot() 字典树
            try:
                tree = self._page.accessibility.snapshot()
            except Exception as e:
                return ToolResult(
                    success=False, output="",
                    error=f"页面结构快照失败: {str(e)}",
                )
            snapshot_text = (
                self._render_a11y_tree(tree, max_depth, max_nodes) if tree else ""
            )

        if not (snapshot_text or "").strip():
            return ToolResult(
                success=True,
                output="（页面无可访问性树：可能是纯 canvas/图片页面，可改用 text/html 命令了解内容）",
            )

        lines = (snapshot_text or "").splitlines()
        if len(lines) > max_nodes:
            lines = lines[:max_nodes]
            lines.append(f"…（快照过长，已截断至 {max_nodes} 行，可结合 elementinfo/截图定位具体元素）")
        try:
            page_url = self._page.url or ""
        except Exception:
            page_url = ""
        return ToolResult(
            success=True,
            output=f"[页面结构快照] {page_url}\n" + "\n".join(lines),
        )

    def _render_a11y_tree(self, tree: dict, max_depth: int,
                          max_nodes: int) -> str:
        """把旧版 accessibility.snapshot() 字典树渲染成缩进文本。"""
        lines, node_count = [], [0]

        def _walk(node: dict, depth: int):
            if depth > max_depth or node_count[0] >= max_nodes:
                return
            role = str(node.get("role") or "")
            name = str(node.get("name") or "")
            label = f'{role} "{name}"' if name else role
            extra = []
            value = node.get("value")
            if isinstance(value, (int, float)):
                extra.append(f"value={value}")
            if node.get("required"):
                extra.append("required")
            if node.get("checked") is not None:
                extra.append(f"checked={node['checked']}")
            if node.get("disabled"):
                extra.append("disabled")
            if node.get("level"):
                extra.append(f"level={node['level']}")
            if node.get("selected") is not None:
                extra.append(f"selected={node['selected']}")
            if node.get("pressed") is not None:
                extra.append(f"pressed={node['pressed']}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append("  " * depth + "- " + label[:120] + suffix)
            node_count[0] += 1
            for child in node.get("children") or []:
                _walk(child, depth + 1)

        _walk(tree, 0)
        return "\n".join(lines)

    # ================================================================
    # 多 Tab 操作
    # ================================================================

    def _new_tab(self, args: str) -> ToolResult:
        """打开新标签页。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            new_page = self._context.new_page()
            self._pages.append(new_page)
            self._current_page_idx = len(self._pages) - 1

            url = args.strip()
            if url:
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                new_page.goto(url, wait_until="domcontentloaded", timeout=30000)

            return ToolResult(
                success=True,
                output=f"已打开标签页 #{len(self._pages)} (共 {len(self._pages)} 个标签页)。"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _switch_tab(self, args: str) -> ToolResult:
        """切换到指定标签页。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            idx = int(args.strip()) - 1  # 转 1-based → 0-based
            if idx < 0 or idx >= len(self._pages):
                return ToolResult(
                    success=False, output="",
                    error=f"无效标签页序号。有效范围: 1-{len(self._pages)}"
                )
            self._current_page_idx = idx
            page = self._page
            return ToolResult(
                success=True,
                output=f"已切换到标签页 #{idx + 1}: {page.title()} ({page.url})",
            )
        except ValueError:
            return ToolResult(success=False, output="", error=f"无效序号: '{args}'")

    def _close_tab(self, args: str) -> ToolResult:
        """关闭指定标签页。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            idx = int(args.strip()) - 1
            if idx < 0 or idx >= len(self._pages):
                return ToolResult(
                    success=False, output="",
                    error=f"无效标签页序号。有效范围: 1-{len(self._pages)}"
                )
            if len(self._pages) == 1:
                return ToolResult(
                    success=False, output="",
                    error="无法关闭最后一个标签页。请使用 'close' 关闭浏览器。"
                )
            self._pages[idx].close()
            self._pages.pop(idx)
            if self._current_page_idx >= len(self._pages):
                self._current_page_idx = len(self._pages) - 1
            return ToolResult(
                success=True,
                output=f"标签页已关闭。剩余 {len(self._pages)} 个标签页。"
            )
        except ValueError:
            return ToolResult(success=False, output="", error=f"无效序号: '{args}'")

    def _list_tabs(self) -> ToolResult:
        """列出所有标签页。"""
        if not self._pages:
            return ToolResult(success=True, output="无标签页。")
        lines = [f"共 {len(self._pages)} 个标签页:"]
        for i, p in enumerate(self._pages):
            active = " <== 当前" if i == self._current_page_idx else ""
            try:
                title = p.title()
                url = p.url
            except Exception:
                title, url = "(已关闭)", ""
            lines.append(f"  [{i + 1}]{active} {title[:60]}")
            if url:
                lines.append(f"      {url[:80]}")
        return ToolResult(success=True, output="\n".join(lines))

    # ================================================================
    # 导航操作
    # ================================================================

    def _goto(self, url: str) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        url = url.strip()
        if not url:
            return ToolResult(success=False, output="", error="URL 为空。")
        if not url.startswith(("http://", "https://")):
            # 检测 URL 中是否包含中文字符，若是则尝试通过意图检测器翻译
            import re
            if re.search(r'[\u4e00-\u9fff]', url):
                detector = get_intent_detector()
                resolved = detector.resolve_url(url)
                if resolved:
                    url = "https://" + resolved
                else:
                    # 无法翻译的中文 URL，尝试直接编码
                    url = "https://" + url
            else:
                url = "https://" + url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return ToolResult(
                success=True,
                output=f"已导航到: {url}\n页面标题: {self._page.title()}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"无法访问 {url}: {str(e)}")

    def _go_back(self, _args: str = "") -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            self._page.go_back(wait_until="domcontentloaded")
            return ToolResult(success=True, output=f"已返回上一页: {self._page.url}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _go_forward(self, _args: str = "") -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            self._page.go_forward(wait_until="domcontentloaded")
            return ToolResult(success=True, output=f"已前进到: {self._page.url}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _refresh(self, _args: str = "") -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            self._page.reload(wait_until="domcontentloaded")
            return ToolResult(success=True, output="页面已刷新。")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    # ================================================================
    # 信息获取
    # ================================================================

    def _get_title(self) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            return ToolResult(success=True, output=f"页面标题: {self._page.title()}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _get_current_url(self) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            return ToolResult(success=True, output=f"当前 URL: {self._page.url}")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _get_html(self) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            html = self._page.content()
            if len(html) > 8000:
                html = html[:8000] + f"\n\n... (HTML 过长，已截断。总长度: {len(self._page.content())} 字符)"
            return ToolResult(success=True, output=html)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _get_text(self) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            text = self._page.inner_text("body")
            if len(text) > 5000:
                text = text[:5000] + "\n\n... (文本过长，已截断。)"
            return ToolResult(success=True, output=text)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    def _element_info(self, selector: str) -> ToolResult:
        """获取元素信息：位置、大小、属性等。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        selector = selector.strip()
        try:
            el = self._page.locator(selector).first
            box = el.bounding_box()
            if box is None:
                return ToolResult(success=False, output="", error=f"元素不可见: {selector}")
            info = {
                "selector": selector,
                "x": box["x"],
                "y": box["y"],
                "width": box["width"],
                "height": box["height"],
                "center_x": box["x"] + box["width"] / 2,
                "center_y": box["y"] + box["height"] / 2,
                "text": "",
                "tag": "",
            }
            try:
                info["text"] = el.inner_text()[:200]
            except Exception:
                pass
            try:
                info["tag"] = el.evaluate("el => el.tagName")
            except Exception:
                pass
            import json
            return ToolResult(success=True, output=json.dumps(info, ensure_ascii=False, indent=2))
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    # ================================================================
    # 交互操作
    # ================================================================

    def _click(self, selector: str) -> ToolResult:
        """点击元素（CSS选择器 → 文本匹配 → role匹配）。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        selector = selector.strip()
        if not selector:
            return ToolResult(success=False, output="", error="选择器为空。")
        try:
            # 1. CSS 选择器
            try:
                self._page.click(selector, timeout=10000)
                return ToolResult(success=True, output=f"已点击元素: {selector}")
            except Exception:
                pass
            # 2. 文本匹配
            element = self._page.get_by_text(selector, exact=False).first
            if element:
                element.click(timeout=10000)
                return ToolResult(success=True, output=f"已点击文本为 '{selector}' 的元素")
            # 3. Role 匹配
            for role in ("button", "link", "textbox", "combobox", "checkbox"):
                try:
                    self._page.get_by_role(role, name=selector).click(timeout=5000)
                    return ToolResult(success=True, output=f"已点击{role}: {selector}")
                except Exception:
                    continue
            return ToolResult(
                success=False, output="",
                error=f"无法找到或点击元素: '{selector}'。请使用 'visionclick <描述>' 基于视觉定位，或先 'screenshot' 查看页面。"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"点击失败: {str(e)}")

    def _vision_click(self, description: str) -> ToolResult:
        """
        基于视觉描述的智能点击。
        流程：截图 → 视觉模型定位坐标 → Playwright 点击坐标
        """
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure

        description = description.strip()
        if not description:
            return ToolResult(success=False, output="", error="元素描述为空。")

        try:
            # 1. 截图
            self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            time.sleep(0.3)
            screenshot_bytes = self._page.screenshot(full_page=False)
            base64_data = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 2. 调用视觉模型定位
            from models.vision import VisionModel
            vision = VisionModel()
            location = vision.locate_element(base64_data, description)

            if not location.get("found"):
                hint = location.get("selector_hint", "")
                return ToolResult(
                    success=False, output="",
                    error=(
                        f"视觉模型未找到元素: '{description}'。"
                        f"{' 提示: ' + hint if hint else ''}"
                    ),
                )

            x = location.get("x", 0)
            y = location.get("y", 0)
            w = location.get("width", 50)
            h = location.get("height", 30)

            # 3. 使用坐标点击
            self._page.mouse.click(x, y)
            time.sleep(0.3)

            return ToolResult(
                success=True,
                output=(
                    f"视觉驱动点击成功！\n"
                    f"元素: {description}\n"
                    f"点击位置: ({x:.0f}, {y:.0f}), 大小: {w:.0f}x{h:.0f}"
                ),
            )
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="VisionModel 不可用。需要支持多模态的 LLM（如 GPT-4o）。",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"视觉点击失败: {str(e)}")

    def _type_text(self, args: str) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(
                success=False, output="",
                error="输入格式: type <选择器> <文本>，例如: type #search 你好",
            )
        selector, text = parts[0].strip(), parts[1]
        try:
            self._page.fill(selector, text, timeout=10000)
            return ToolResult(success=True, output=f"已在 '{selector}' 中输入: {text}")
        except Exception:
            try:
                self._page.click(selector, timeout=5000)
                self._page.fill(selector, "", timeout=5000)
                self._page.type(selector, text, delay=50)
                return ToolResult(success=True, output=f"已在 '{selector}' 中输入: {text}")
            except Exception as e2:
                return ToolResult(success=False, output="", error=f"无法输入文本: {str(e2)}")

    def _hover(self, selector: str) -> ToolResult:
        """鼠标悬停。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        selector = selector.strip()
        try:
            self._page.hover(selector, timeout=10000)
            return ToolResult(success=True, output=f"已悬停到: {selector}")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"悬停失败: {str(e)}")

    def _scroll(self, args: str) -> ToolResult:
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        parts = args.strip().split()
        if not parts:
            return ToolResult(
                success=False, output="",
                error="滚动格式: scroll <方向> [像素]，例如: scroll down 300",
            )
        direction = parts[0].lower()
        pixels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 500
        scroll_map = {
            "down": f"window.scrollBy(0, {pixels})",
            "up": f"window.scrollBy(0, {-pixels})",
            "left": f"window.scrollBy({-pixels}, 0)",
            "right": f"window.scrollBy({pixels}, 0)",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
        }
        js_code = scroll_map.get(direction)
        if js_code is None:
            return ToolResult(
                success=False, output="",
                error=f"未知滚动方向: '{direction}'。支持: {', '.join(scroll_map.keys())}",
            )
        try:
            self._page.evaluate(js_code)
            time.sleep(0.3)
            return ToolResult(
                success=True,
                output=f"页面已滚动: {direction} {pixels if direction not in ('top', 'bottom') else ''}",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"滚动失败: {str(e)}")

    # ================================================================
    # 键盘、JS、弹窗
    # ================================================================

    def _press_key(self, key: str) -> ToolResult:
        """按下键盘按键。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        key = key.strip()
        if not key:
            return ToolResult(success=False, output="", error="按键名为空。")
        try:
            self._page.keyboard.press(key)
            return ToolResult(success=True, output=f"已按下按键: {key}")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"按键失败: {str(e)}")

    def _execute_js(self, code: str) -> ToolResult:
        """执行 JavaScript 代码。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        code = code.strip()
        if not code:
            return ToolResult(success=False, output="", error="JS 代码为空。")
        try:
            result = self._page.evaluate(code)
            return ToolResult(
                success=True,
                output=f"JS 执行结果: {str(result)[:500] if result is not None else '(无返回值)'}"
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"JS 执行失败: {str(e)}")

    def _handle_alert(self, args: str) -> ToolResult:
        """处理浏览器弹窗（alert/confirm/prompt）。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        action = args.strip().lower()
        try:

            def _dialog_handler(dialog):
                text = dialog.message
                if action == "accept":
                    dialog.accept()
                elif action == "dismiss":
                    dialog.dismiss()
                elif "prompt" in action or "text" not in action:
                    dialog.accept(action.split(" ", 1)[1] if " " in action else "")

            self._page.on("dialog", _dialog_handler)

            if action == "text":
                dialog_text = self._page.evaluate(
                    "() => { const d = window.__dialog_text; return d || '无活跃弹窗'; }"
                )
                return ToolResult(success=True, output=f"弹窗文字: {dialog_text}")

            return ToolResult(success=True, output=f"弹窗已处理: {action}")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"弹窗处理失败: {str(e)}")

    # ================================================================
    # 截图
    # ================================================================

    def _screenshot(self, full_base64: bool = False) -> ToolResult:
        """截取当前页面截图。"""
        ensure = self._ensure_page()
        if not ensure.success:
            return ensure
        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
            filepath = os.path.join(self._screenshot_dir, filename)

            self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            time.sleep(0.3)

            screenshot_bytes = self._page.screenshot(path=filepath, full_page=False)
            base64_data = base64.b64encode(screenshot_bytes).decode("utf-8")
            title = self._page.title()
            url = self._page.url

            if full_base64:
                return ToolResult(
                    success=True,
                    output=(
                        f"截图已保存: {filepath}\n"
                        f"页面: {title}\n"
                        f"URL: {url}\n"
                        f"大小: {len(screenshot_bytes)} 字节\n"
                        f"[FULL_BASE64]{base64_data}[/FULL_BASE64]"
                    ),
                )
            else:
                return ToolResult(
                    success=True,
                    output=(
                        f"截图已保存: {filepath}\n"
                        f"页面: {title}\n"
                        f"URL: {url}\n"
                        f"大小: {len(screenshot_bytes)} 字节"
                    ),
                )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"截图失败: {str(e)}")

    # ================================================================
    # 辅助
    # ================================================================

    def _wait(self, seconds: str) -> ToolResult:
        try:
            sec = float(seconds.strip()) if seconds.strip() else 2.0
            sec = min(sec, 30.0)
            time.sleep(sec)
            return ToolResult(success=True, output=f"已等待 {sec} 秒。")
        except ValueError:
            return ToolResult(success=False, output="", error=f"无效的等待时间: '{seconds}'")

    def _status(self) -> ToolResult:
        if not self._pages or self._page is None or not self._is_browser_alive():
            return ToolResult(success=True, output="浏览器未启动。发送 'launch' 启动浏览器。")
        try:
            mode = "无头" if self._headless else "可视化"
            return ToolResult(
                success=True,
                output=(
                    f"浏览器状态: 运行中\n"
                    f"模式: {mode}\n"
                    f"标签页: {self._current_page_idx + 1}/{len(self._pages)}\n"
                    f"当前 URL: {self._page.url}\n"
                    f"页面标题: {self._page.title()}\n"
                    f"视口: {self._viewport_width}x{self._viewport_height}"
                ),
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"获取状态失败: {str(e)}")

    def _is_browser_alive(self) -> bool:
        if self._browser is None:
            # 持久模式：没有独立 Browser 对象，探活 context（关闭后访问会抛）
            if self._persistent and self._context is not None:
                return self._is_context_alive()
            return False
        try:
            # is_connected 是本地状态检查（CDP 连接是否还活着），不会走网络挂起；
            # 旧实现访问 self._browser.contexts 在 CDP 半死时同样可能永久阻塞。
            if hasattr(self._browser, "is_connected"):
                return bool(self._browser.is_connected)
            self._browser.contexts
            return True
        except Exception:
            return False

    def _is_context_alive(self) -> bool:
        """持久模式下的 context 探活（廉价本地访问，关闭后抛异常即视为死亡）。"""
        try:
            _ = self._context.pages
            return True
        except Exception:
            return False

    def reset(self):
        """强制重置浏览器状态（工具超时后由 ToolManager.reset_tool 调用）。

        丢弃 Playwright 引用与页面列表，下次任何命令都会重新 launch。
        不尝试优雅关闭（CDP 可能已挂死，关闭调用同样会阻塞）。
        """
        self._playwright = None
        self._browser = None
        self._context = None
        self._pages = []
        self._current_page_idx = 0

    def get_page(self):
        return self._page

    def __del__(self):
        try:
            self._close()
        except Exception:
            pass
