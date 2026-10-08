"""浏览器工具模块（增强版）。"""
import os
import base64
import time
import threading
from typing import Optional

from tools.base import BaseTool, ToolResult
from tools.computer_use import ComputerUseMixin
from tools import human_check
from tools.intent_detector import get_intent_detector
from config import BROWSER_CONFIG, PROJECT_ROOT, resolve_under_root

# 持久 profile 里**纯遥测/缓存**的子目录：删掉不影响登录态（cookies/localStorage 在 Default/ 下），但会随每次浏览器启动各长几 MB。


def _resolve_under_root(path: str) -> str:
    """兼容入口：实现已收进 config.resolve_under_root（全项目只保留一处真相）。"""
    return resolve_under_root(path)


_PROFILE_JUNK_DIRS = (
    "DeferredBrowserMetrics",
    "BrowserMetrics",
    "Crashpad/reports",
    "ShaderCache",
    "GrShaderCache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnWebGPUCache",
    "Default/DawnGraphiteCache",
)


def _prune_profile_dir(profile_dir: str, max_age_days: float = None,
                       max_total_mb: float = None) -> int:
    """清理持久 profile 里的遥测/缓存垃圾，返回释放的字节数。"""
    import shutil
    from datetime import datetime

    max_age_days = float(BROWSER_CONFIG.get("profile_max_age_days", 7)
                         if max_age_days is None else max_age_days)
    max_total_mb = float(BROWSER_CONFIG.get("profile_max_mb", 150)
                         if max_total_mb is None else max_total_mb)
    root = os.path.abspath(profile_dir)
    if not os.path.isdir(root):
        return 0
    freed = 0
    cutoff = time.time() - max_age_days * 86400

    def _dir_size(p: str) -> int:
        total = 0
        for dp, _, fs in os.walk(p):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return total

    for rel in _PROFILE_JUNK_DIRS:
        target = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isdir(target):
            continue
        # Crashpad/reports 与 *Metrics 目录都是"一堆历史文件"，按年龄删；缓存目录整体删（浏览器会重建）。
        if rel in ("Default/Code Cache", "Default/GPUCache",
                   "Default/DawnWebGPUCache", "Default/DawnGraphiteCache"):
            freed += _dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            continue
        for name in os.listdir(target):
            p = os.path.join(target, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    if os.path.isdir(p):
                        freed += _dir_size(p)
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        freed += os.path.getsize(p)
                        os.remove(p)
            except OSError:
                pass

    # 总量兜底：仍超限就整体丢掉遥测目录（缓存会重建，登录态不受影响）
    total = _dir_size(root)
    if total > max_total_mb * 1024 * 1024:
        for rel in ("DeferredBrowserMetrics", "BrowserMetrics", "Crashpad/reports"):
            target = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isdir(target):
                freed += _dir_size(target)
                shutil.rmtree(target, ignore_errors=True)
    return freed


class BrowserTool(BaseTool, ComputerUseMixin):
    """浏览器操作工具（基于 Playwright）。"""

    def __init__(
        self,
        headless: bool = None,
        screenshot_dir: str = "./screenshots",
        viewport_width: int = None,
        viewport_height: int = None,
        persistent: bool = None,
        download_dir: str = None,
    ):
        self._headless = (
            headless
            if headless is not None
            else BROWSER_CONFIG.get("headless", False)
        )
        self._screenshot_dir = _resolve_under_root(screenshot_dir)
        self._download_dir = _resolve_under_root(
            download_dir or BROWSER_CONFIG.get("download_dir", "./downloads"))
        self._viewport_width = viewport_width or BROWSER_CONFIG.get("viewport_width", 1280)
        self._viewport_height = viewport_height or BROWSER_CONFIG.get("viewport_height", 720)
        # 内置浏览器：持久 profile 模式（登录态跨次启动保留，网页型分身依赖）。 persistent 可显式传 False 创建独立临时 profile 实例（如侧栏辅助浏览器），避免与主任务浏览器争用同一 user-data-dir 锁。
        if persistent is not None:
            self._persistent = bool(persistent)
        else:
            self._persistent = bool(BROWSER_CONFIG.get("persistent", True))
        self._profile_dir = _resolve_under_root(
            BROWSER_CONFIG.get("profile_dir", "./memory/browser_profile"))

        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: list = []        # 所有 page 列表
        self._current_page_idx = 0     # 当前活跃 page 索引
        # 世代号：会话重建（超时/崩溃自愈）时 +1，用来作废"在飞的旧任务" ——
        # 卡死的 worker 之后才跑完的任务不能把结果/状态写回新会话。
        self._gen = 0
        self._state_lock = threading.RLock()

        # 专属 worker 线程：Playwright sync API 把 asyncio 事件循环绑定在启动线程上，而 Executor 每次调用工具都在新的 daemon 线程执行。
        self._worker: Optional[threading.Thread] = None
        self._worker_queue = None       # queue.Queue（惰性创建）
        self._worker_broken = False     # 上次执行疑似卡死被废弃，下次命令重建

        os.makedirs(self._screenshot_dir, exist_ok=True)
        os.makedirs(self._download_dir, exist_ok=True)

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
            "  download <选择器>    - 点击它触发下载并把文件落盘，返回保存路径\n"
            "  humancheck          - 检测页面是否有人机验证（命中就该交给人来做）\n"
            "  live on|off|status   - 实时画面开关（dashboard 页面的「实时画面」面板观看）\n"
            "\n【精确鼠标操作 Computer Use】\n"
            "  mousemove <x> <y>       - 移动鼠标到指定坐标\n"
            "  clickat <x> <y> [left|right|middle] - 在坐标处点击\n"
            "  dblclickat <x> <y>      - 在坐标处双击\n"
            "  注：以上坐标是**页面视口 CSS 像素**（页面内坐标），不是屏幕坐标；"
            "要按屏幕坐标操作桌面请用 computer 工具。\n"
            "  drag <x1> <y1> <x2> <y2> [步数] - 拖拽从A到B\n"
            "  mousescroll <x> <y> [dx] [dy] - 在坐标处滚轮\n"
            "  keycombo <键1>+<键2>     - 组合键（如 Control+C）\n"
            "  typedirect <文本>      - 直接在当前焦点输入\n"
            "\n【其他】\n"
            "  wait <秒数>          - 等待指定秒数\n"
        )

    # ================================================================
    # 审批元数据 + JSON Schema 接口
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
            "download": self._download,
            "humancheck": lambda _: self._human_check(),
            "live": self._live,
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
            result = self._dispatch(handler, args)
        except Exception as e:
            result = ToolResult(
                success=False, output="", error=f"浏览器操作失败: {str(e)}"
            )
        # 会话失效（浏览器被杀/CDP 断开/worker 线程 loop 不可复用）→ 自动重建重试一次，不再要求用户重启整个项目。
        if not getattr(result, "success", False) and self._looks_dead(
            f"{getattr(result, 'error', '') or ''} {getattr(result, 'output', '') or ''}"
        ):
            recovered = self._recover_and_retry(command, handler, args)
            if recovered is not None:
                return recovered
        return result

    # ================================================================
    # 专属 worker 线程（Playwright 生命周期绑定单线程）
    # ================================================================

    def _start_worker(self) -> None:
        """创建/重建专属 worker 线程。
        Playwright sync API 底层把 asyncio 事件循环绑定到启动它的线程；
        浏览器命令，playwright 对象始终在同一线程创建和使用。"""
        import queue

        self._worker_queue = queue.Queue()
        self._worker_broken = False

        def _loop():
            while True:
                job = self._worker_queue.get()
                if job is None:
                    return
                try:
                    job()
                except Exception:
                    pass  # job 内部已捕获异常并放入结果 box

        self._worker = threading.Thread(
            target=_loop, daemon=True, name="browser-worker")
        self._worker.start()

    # 浏览器会话"已死"的特征串：命中即作废旧 worker/引用并自动重建重试
    _DEAD_SIGNALS = (
        "has been closed",
        "Target closed",
        "Target page, context or browser",
        "Browser closed",
        "Connection closed",
        "Sync API inside the asyncio loop",
        "cannot switch to a different thread",
        "browser has been closed",
        "会话已重建",
    )

    @classmethod
    def _looks_dead(cls, text: str) -> bool:
        t = str(text or "")
        return any(sig.lower() in t.lower() for sig in cls._DEAD_SIGNALS)

    def _stop_playwright(self) -> None:
        """停掉 Playwright 的**驱动进程**（node.exe）。"""
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def _invalidate(self, cleanup_residual: bool = True) -> None:
        """作废当前 worker 与 Playwright 引用：下一个命令会重建全新 worker。

        连带清理残留浏览器进程——卡死时驱动进程停不掉 Chromium，只丢引用会泄漏进程。
        """
        with self._state_lock:
            self._gen += 1                     # 作废在飞任务
            had_session = self._had_session()
            self._worker_broken = True
            self._worker = None
            self._worker_queue = None
            self._stop_playwright()
            self._browser = None
            self._context = None
            self._pages = []
            self._current_page_idx = 0
        if cleanup_residual and had_session:
            try:
                self._force_cleanup_residual()
            except Exception:                    # noqa: BLE001
                pass

    def _recover_and_retry(self, command: str, handler, args: str):
        """会话失效后的自愈：重建 worker（+必要时重开浏览器）并重试一次原命令。
        旧 worker 线程的 asyncio loop 无法复用时，不再把错误直接抛给模型。"""
        try:
            self._invalidate()
            if command not in ("launch", "close"):
                try:
                    self._dispatch(self._launch, "")     # 重建后重开浏览器
                except Exception:
                    pass
            res = self._dispatch(handler, args)
            if res is not None and res.success:
                out = (res.output or "").strip()
                note = "(检测到浏览器会话已失效，已自动重建浏览器并重试成功)"
                return ToolResult(
                    success=True,
                    output=((out + chr(10)) if out else "") + note,
                    metadata=getattr(res, "metadata", None),
                )
            return res
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"浏览器会话失效，且自动恢复失败: {str(e)[:200]}")

    def _dispatch(self, fn, *args, wait_timeout: float = None, **kwargs):
        """把浏览器命令投递到 worker 线程执行并等待结果。
        worker（daemon 线程随进程退出），由 _force_cleanup_residual 兜底。"""
        # worker 缺失/已死/上次卡死被废弃 → 重建。附加情况：worker 线程虽然还活着，但绑定的浏览器/上下文引用已经死掉（浏览器进程被杀、CDP 会话关闭等）。
        needs_rebuild = (
            self._worker is None
            or not self._worker.is_alive()
            or self._worker_broken
        )
        if not needs_rebuild:
            try:
                browser_alive = self._is_browser_alive()
            except Exception:
                browser_alive = False
            try:
                context_alive = bool(
                    self._context is not None and self._is_context_alive())
            except Exception:
                context_alive = False
            if not browser_alive and not context_alive:
                needs_rebuild = True
        if needs_rebuild:
            # 防御性丢弃旧 playwright 引用（若未被 reset 清空），避免新 worker 复用绑定在已死线程上的事件循环。
            if self._playwright is not None or self._context is not None:
                self._stop_playwright()
                self._browser = None
                self._context = None
                self._pages = []
                self._current_page_idx = 0
            self._start_worker()

        box: dict = {}
        done = threading.Event()
        gen = self._gen

        def _job():
            # 世代号变了 = 这条命令属于已经作废的会话：直接丢弃，
            # 不让卡死的旧 worker 把结果/状态写回新会话。
            if gen != self._gen:
                box["stale"] = True
                done.set()
                return
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as e:   # noqa: BLE001
                box["error"] = e
            finally:
                done.set()

        try:
            self._worker_queue.put(_job)
        except Exception as e:
            self._worker_broken = True
            self._worker = None
            raise RuntimeError(f"浏览器 worker 队列异常: {e}") from e

        if wait_timeout is None:
            done.wait()
        else:
            done.wait(wait_timeout)
            if not done.is_set():
                # 短等待超时（析构场景）：放弃该 worker，交给兜底清理
                self._worker_broken = True
                return None
        if box.get("stale"):
            raise RuntimeError("浏览器会话已重建（上一个 worker 已作废），请重试该命令。")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ================================================================
    # 生命周期管理
    # ================================================================

    def _launch(self, _args: str = "") -> ToolResult:
        if self._browser is not None and self._is_browser_alive():
            return ToolResult(success=True, output="浏览器已在运行中。")
        if self._persistent and self._context is not None and self._is_context_alive() and self._page_alive():
            return ToolResult(success=True, output="浏览器已在运行中（持久 profile）。")

        # context 仍活着但页面已死/缺失（如外部关闭了标签页、CDP 会话中断后页面失效）：只重建页面列表，不要重启 Playwright——旧 worker 线程的 asyncio loop 已被绑定。
        if self._persistent and self._context is not None and self._is_context_alive():
            try:
                existing = [p for p in self._context.pages if not p.is_closed()]
                if not existing:
                    existing = [self._context.new_page()]
                self._pages = existing
                self._current_page_idx = 0
                return ToolResult(
                    success=True,
                    output="浏览器已在运行中（持久 profile，已重建失效页面）。",
                )
            except Exception:
                # 重建失败则按全新启动流程处理（下方会重建 worker）
                pass

        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            if self._persistent:
                # 持久模式：用户数据目录落盘，登录态（cookie/localStorage）跨次启动保留——网页型分身先 headed 手动登录一次，之后 agent 复用会话。返回值直接是 BrowserContext（没有独立 Browser 对象）。
                os.makedirs(self._profile_dir, exist_ok=True)
                _prune_profile_dir(self._profile_dir)
                self._browser = None
                self._context = self._playwright.chromium.launch_persistent_context(
                    self._profile_dir,
                    headless=self._headless,
                    viewport={"width": self._viewport_width, "height": self._viewport_height},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                    # crashpad 管道初始化失败（TransactNamedPipe: 管道已结束）会让 chromium 启动即退出（exitCode 21），加此参数禁用崩溃上报模块后恢复。crashpad 仅用于崩溃收集，禁掉不影响功能。
                    args=["--disable-crashpad"],
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
            # launch_persistent_context 会自带一个初始 about:blank 页面，直接复用而不是再 new_page()——否则浏览器里会出现两个空白标签页。
            existing = list(self._context.pages)
            if existing:
                self._pages = existing
            else:
                self._pages = [self._context.new_page()]
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

    def _close(self, _args: str = "", _wait: float = None) -> ToolResult:
        """优雅关闭浏览器（实际关闭逻辑在 worker 线程执行，避免跨线程）。
        正常调用无限等待，外层 Executor 有硬超时兜底。"""
        if threading.current_thread() is self._worker:
            # 已在 worker 线程内（理论上不会发生，防御处理）
            return self._close_impl()
        result = self._dispatch(self._close_impl, wait_timeout=_wait)
        if result is None:
            # 短等待超时（析构场景）：兜底清理残留进程（没启动过则无事可做）
            killed = self._force_cleanup_residual() if self._had_session() else 0
            return ToolResult(
                success=True,
                output=f"浏览器已关闭（兜底清理 {killed} 个残留进程）。",
            )
        return result

    def _close_impl(self) -> ToolResult:
        graceful = True
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            # CDP 连接异常（如管理线程已退出）时优雅关闭会抛错，此时浏览器进程可能仍驻留并占着 profile_dir 锁——兜底清理。
            graceful = False
        finally:
            self._context = None
            self._browser = None
            self._pages = []
            self._current_page_idx = 0
            self._playwright = None
        killed = self._force_cleanup_residual()
        if graceful and killed == 0:
            return ToolResult(success=True, output="浏览器已关闭。")
        note = f"（优雅关闭失败，已兜底清理 {killed} 个残留进程）" if not graceful else ""
        return ToolResult(success=True, output=f"浏览器已关闭。{note}")

    def _force_cleanup_residual(self) -> int:
        """兜底：清理仍占用 profile_dir 的残留浏览器进程，返回清掉的个数。"""
        killed = 0
        for pid in self._residual_pids():
            if self._kill_residual(pid):
                killed += 1
        return killed

    def _residual_pids(self) -> list:
        """列出命令行里带本工具 `--user-data-dir` 的浏览器进程号（按平台分派）。"""
        return (self._residual_pids_win() if os.name == "nt"
                else self._residual_pids_posix())

    def _residual_pids_win(self) -> list:
        import subprocess
        profile = self._profile_dir.replace("'", "''")  # PowerShell 单引号转义
        cmd = (
            "powershell -NoProfile -Command "
            f"\"Get-CimInstance Win32_Process -Filter \\\"Name='chrome.exe'\\\" | "
            f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
            f"Select-Object -ExpandProperty ProcessId\""
        )
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30, shell=True).stdout
        except Exception:
            return []
        return [p.strip() for p in out.split() if p.strip().isdigit()]

    def _residual_pids_posix(self) -> list:
        """POSIX 侧：`ps` 取完整命令行，realpath **全等**比对 profile 目录。
        全等而不是子串：避免 "profile" 前缀误伤 "profile2" 这类无关实例。"""
        import subprocess
        try:
            out = subprocess.run(["ps", "-A", "-o", "pid=,args="],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30).stdout
        except Exception:
            return []
        want = os.path.realpath(self._profile_dir)
        pids = []
        for line in (out or "").splitlines():
            line = line.strip()
            if "--user-data-dir=" not in line:
                continue
            pid, _, args = line.partition(" ")
            if not pid.isdigit():
                continue
            value = args.split("--user-data-dir=", 1)[1].strip().split(" ", 1)[0]
            value = value.strip("\"'")                     # 有的启动方式会带引号
            if value and os.path.realpath(value) == want:
                pids.append(pid)
        return pids

    def _kill_residual(self, pid: str) -> bool:
        """杀掉一个残留进程（按平台分派）。"""
        return (self._kill_residual_win(pid) if os.name == "nt"
                else self._kill_residual_posix(pid))

    @staticmethod
    def _kill_residual_win(pid: str) -> bool:
        import subprocess
        try:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
            return True
        except Exception:
            return False

    @staticmethod
    def _kill_residual_posix(pid: str) -> bool:
        """先 SIGTERM，给 0.5s 自行收尾（Chromium 会带走子进程），仍活着再 SIGKILL。"""
        import signal
        # SIGKILL 在 Windows 上不存在；POSIX 各平台都是 9（取默认值以便被单测覆盖）
        sigkill = getattr(signal, "SIGKILL", 9)
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            return True                                    # 已经没了，也算清掉
        except Exception:
            return False
        for _ in range(5):
            time.sleep(0.1)
            try:
                os.kill(int(pid), 0)                       # 探活，不真发信号
            except ProcessLookupError:
                return True
            except Exception:
                return True
        try:
            os.kill(int(pid), sigkill)
        except ProcessLookupError:
            return True                                    # 刚好在这一刻退出了
        except Exception:
            return False                                   # 没杀掉就是没杀掉，别报成功
        return True

    def _ensure_page(self) -> ToolResult:
        if not self._pages or self._page is None or not self._is_browser_alive() or not self._page_alive():
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
        """返回当前页面的结构快照（对齐 Playwright MCP 的 browser_snapshot）：
        决定点击/填写目标，避免每次都截图走视觉模型。
        page.accessibility）；旧版本回退 accessibility.snapshot 字典树。"""
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
                html = html[:8000] + f"\n\n... (HTML 过长，已截断。总长度: {len(html)} 字符)"
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
            # 2. 文本匹配⚠️ 不能用 `element = get_by_text(...).first。
            try:
                self._page.get_by_text(selector, exact=False).first.click(timeout=3000)
                return ToolResult(success=True, output=f"已点击文本为 '{selector}' 的元素")
            except Exception:
                pass
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
        """基于视觉描述的智能点击。"""
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
            # 弹窗文字：唯一可靠的来源是 dialog 事件回调。旧实现读的是`window.__dialog_text`——该变量在全仓库只出现在这一行、**从未被赋值**，所以 `alert text` 永远返回"无活跃弹窗"。
            captured = {}

            def _dialog_handler(dialog):
                captured["text"] = dialog.message
                if action == "text":
                    # 只查询不解弹窗：保持弹窗打开，交由后续 accept/dismiss
                    return
                if action == "accept":
                    dialog.accept()
                elif action == "dismiss":
                    dialog.dismiss()
                elif "prompt" in action or "text" not in action:
                    dialog.accept(action.split(" ", 1)[1] if " " in action else "")

            # 关键：只能注册**一次性**监听器。之前每次 alert 调用都 `page.on(...)`且永不摘除：监听器越堆越多，而且一旦注册了 dialog 监听器， Playwright 就不再自动 dismiss → 弹窗挂着。
            self._page.once("dialog", _dialog_handler)

            if action == "text":
                # 给弹窗事件一点时间到达；没有弹窗就如实说明
                for _ in range(20):
                    if captured.get("text") is not None:
                        break
                    self._page.wait_for_timeout(50)
                text = captured.get("text")
                if text is None:
                    return ToolResult(success=True, output="当前没有活跃弹窗。")
                return ToolResult(success=True, output=f"弹窗文字: {text}")

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
            # 毫秒精度：旧实现只到秒，同一秒内两次截图会**互相覆盖**，而两次调用都返回 success 和同一个路径（与 tts/video_gen/image_gen/video_edit 里已经修过的同款问题一致）。
            timestamp = time.strftime("%Y%m%d_%H%M%S") + "_" + f"{int(time.time() * 1000) % 1000:03d}"
            filename = f"screenshot_{timestamp}.png"
            filepath = os.path.join(self._screenshot_dir, filename)
            save_to_disk = not full_base64

            self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            time.sleep(0.3)

            if save_to_disk:
                screenshot_bytes = self._page.screenshot(path=filepath, full_page=False)
            else:
                screenshot_bytes = self._page.screenshot(full_page=False)
            base64_data = base64.b64encode(screenshot_bytes).decode("utf-8")
            title = self._page.title()
            url = self._page.url

            head = f"截图已保存: {filepath}\n" if save_to_disk else ""
            info = (f"页面: {title}\n"
                    f"URL: {url}\n"
                    f"大小: {len(screenshot_bytes)} 字节")
            # URL 就能看出是验证页时直接提示（不做额外的浏览器往返）
            hint = human_check.hint_for_url(url)
            metadata = {"human_check": True} if hint else {}
            if full_base64:
                # 负载同时放 metadata：实时画面/直接调用绕过工具层时也能拿到完整图
                metadata["screenshot_base64"] = base64_data
                return ToolResult(
                    success=True,
                    output=f"{head}{info}\n{hint}\n[FULL_BASE64]{base64_data}[/FULL_BASE64]",
                    metadata=metadata)
            return ToolResult(success=True, output=f"{head}{info}\n{hint}".rstrip(),
                              metadata=metadata)
        except Exception as e:
            return ToolResult(success=False, output="", error=f"截图失败: {str(e)}")

    # ================================================================
    # 辅助
    # ================================================================
    def _live(self, args: str) -> ToolResult:
        """实时画面开关：注册给 dashboard 的 SSE 端点用的抓帧器。"""
        from tools import screencast

        mode = (args or "").strip().lower() or "status"
        if mode in ("on", "start"):
            screencast.ACTIVE.set_capturer(self._capture_frame_for_live)
            action = "已开启"
        elif mode in ("off", "stop"):
            screencast.ACTIVE.set_capturer(None)
            action = "已关闭"
        elif mode == "status":
            action = "状态"
        else:
            return ToolResult(success=False, output="", error="用法: live on|off|status")
        stats = screencast.ACTIVE.stats()
        return ToolResult(
            success=True,
            output=(f"实时画面{action}：观看者 {stats['viewers']} 人，已推送 {stats['emitted']} 帧，"
                    f"丢弃 {stats['dropped']} 帧（最小间隔 {stats['min_gap_ms']:.0f}ms）。\n"
                    "打开 dashboard 页面的「实时画面」面板即可观看。"))

    def _capture_frame_for_live(self) -> str:
        """给实时画面抓一帧；浏览器没在跑就返回空（不因有人观看而拉起浏览器）。"""
        try:
            if not self._is_browser_alive() or not self._page_alive():
                return ""
        except Exception:                        # noqa: BLE001
            return ""
        result = self.execute("screenshot_base64")
        if not result.success:
            return ""
        return str((result.metadata or {}).get("screenshot_base64") or "")

    def _human_check(self) -> ToolResult:
        """检测当前页面是否有人机验证；命中就明确"需要人工完成"。"""
        url = str(getattr(self._page, "url", "") or "")
        hits = []
        for selector in human_check.DOM_SELECTORS:
            try:
                if self._page.query_selector(selector) is not None:
                    hits.append(selector)
            except Exception:                    # noqa: BLE001
                continue
        text = ""
        try:
            text = str(self._page.inner_text("body") or "")[:5000]
        except Exception:                        # noqa: BLE001
            pass
        info = human_check.verdict(url=url, dom_hits=hits, text=text)
        if not info["needs_human"]:
            return ToolResult(success=True, output="未检测到人机验证，可以继续操作。",
                              metadata={"human_check": False})
        return ToolResult(
            success=True,
            output=f"{info['hint']}\n命中信号: {'；'.join(info['reasons'])}",
            metadata={"human_check": True, "reasons": info["reasons"]})

    def _download(self, args: str) -> ToolResult:
        """点击 args 指定的元素，把触发的下载落盘并返回路径。"""
        selector = (args or "").strip()
        if not selector:
            return ToolResult(success=False, output="",
                              error="用法: download <选择器或文字>（点击它触发下载）")
        timeout = float(BROWSER_CONFIG.get("download_timeout", 30))
        os.makedirs(self._download_dir, exist_ok=True)
        try:
            with self._page.expect_download(timeout=timeout * 1000) as waiter:
                clicked = self._click(selector)
                if not clicked.success:
                    return clicked
            download = waiter.value
            name = download.suggested_filename or f"download-{int(time.time())}"
            path = os.path.join(self._download_dir, name)
            stem, ext = os.path.splitext(path)
            seq = 1
            while os.path.exists(path):          # 同名不覆盖
                path = f"{stem}-{seq}{ext}"
                seq += 1
            download.save_as(path)
        except Exception as e:                   # noqa: BLE001
            return ToolResult(
                success=False, output="",
                error=(f"下载失败: {str(e)[:150]}。可能是该元素不触发下载，"
                       f"或超过 {timeout:g}s 没开始下载。"))
        size = os.path.getsize(path) if os.path.exists(path) else 0
        return ToolResult(
            success=True,
            output=f"已下载: {os.path.basename(path)}（{size} 字节）\n保存路径: {path}",
            metadata={"path": path, "size": size})

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
            # is_connected 是本地状态检查（CDP 连接是否还活着），不会走网络挂起；旧实现访问 self._browser.contexts 在 CDP 半死时同样可能永久阻塞。

            if hasattr(self._browser, "is_connected"):
                conn = self._browser.is_connected
                return bool(conn() if callable(conn) else conn)
            self._browser.contexts
            return True
        except Exception:
            return False

    def _is_context_alive(self) -> bool:
        """持久模式下的 context 探活。
        `page.is_closed()` —— 那才是真实的探活信号（关闭后同为 True）。"""
        try:
            if self._context.pages:
                return True
        except Exception:
            return False
        page = self._page
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    def _page_alive(self) -> bool:
        """当前活跃 page 探活（关闭后 is_closed() 为 True 即视为死亡）。"""
        page = self._page
        if page is None:
            return False
        try:
            return not page.is_closed()
        except Exception:
            return False

    def reset(self):
        """强制重置浏览器状态（工具超时后由 ToolManager.reset_tool 调用）。
        线程标记废弃并通知退出（若未卡死），下次命令自动重建；在飞任务一并作废。"""
        self._gen += 1                 # 作废在飞任务，防止卡死的 worker 事后写回状态
        had_session = self._had_session()
        self._worker_broken = True
        wq = self._worker_queue
        self._worker_queue = None
        if wq is not None:
            try:
                wq.put(None)   # 通知旧 worker 退出（卡死则忽略，daemon 随进程退出）
            except Exception:
                pass
        self._worker = None
        self._stop_playwright()
        self._browser = None
        self._context = None
        self._pages = []
        self._current_page_idx = 0
        # 必须真正清掉残留的 Chromium/node 进程：旧实现只丢引用，而每次工具超时都会走到这里 → 每超时一次就泄漏一个 Chromium + Playwright node。没启动过浏览器则无需清理。
        if not had_session:
            return
        try:
            killed = self._force_cleanup_residual()
            if killed:
                print(f"[Browser] 已清理残留浏览器进程 {killed} 个")
        except Exception:
            pass

    def get_page(self):
        return self._page

    def __del__(self):
        try:
            # 从没启动过浏览器就没有残留可清：跳过既避免析构里等 5 秒，
            # 也避免半初始化实例被回收时误跑进程清理
            if not self._had_session():
                return
            # 析构场景短等待：worker 卡死时不被拖住，交给兜底清理
            self._close(_wait=5)
        except Exception:
            pass

    def _had_session(self) -> bool:
        """是否真的启动过浏览器（决定要不要做进程兜底清理）。"""
        return any(getattr(self, attr, None) is not None
                   for attr in ("_playwright", "_browser", "_context"))
