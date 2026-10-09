import base64
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict

from tools import human_check
from tools.base import ToolResult
from tools.browser import BrowserTool

SENTINEL = "@@EGO_RESULT@@"

#: 插件自带的运行时（本机 DSH 用的就是它）；没装 DSH 时回落到 PATH 上的 ego-browser
DEFAULT_CLI = os.path.expanduser(
    "~/.dsh/profiles/web/node_modules/dsh-ego-browser/runtime/ego-linux/bin/ego-browser.mjs")

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: Linux 移植版靠 EGO_LINUX_CHROME 找浏览器；Windows 上必须显式给 Edge 路径
CHROME_ENV = ("EGO_LINUX_CHROME", "BROWSER_EGO_CHROME")

#: 每次脚本先认领**本会话自己的任务空间**，并用本会话记住的标签页。
#: 不这么做的话，两个 agent 会话（或 agent 与宿主）会抢同一个标签页：
#: 运行时按任务空间收窄 `listTabs()`，空间独立才是真隔离。
#:
#: 快路径（重要）：切空间与 switchTab 都走 `Target.activateTarget`，会把浏览器窗口
#: **拉到最前**——每条命令都切一次就会一直弹窗、抢用户的操作焦点。所以已经在本会话
#: 的标签页上时直接跳过这两步：只有真要换页面/开新页时才抢一次前台。
SPACE_PREAMBLE = """const __spaceName = __EGO_SPACE__
const __want = __EGO_TAB__
let __cur = null
try { __cur = await browser.currentTab() } catch (e) {}
const __onMine = Boolean(__want && __cur && __cur.targetId === __want)
let __space = null
let __tabs = []
let __tab = __onMine ? __cur : null
if (!__onMine) {
  try { __space = await taskSpaces.useOrCreate(__spaceName) } catch (e) {}
  try { __tabs = await browser.listTabs() } catch (e) {}
  __tab = __want ? __tabs.find(t => t.targetId === __want) : null
  if (!__tab && __tabs.length === 0) {
    try { const __made = await ego.createTab('about:blank'); __tab = { targetId: __made.targetId } } catch (e) {}
  }
  if (!__tab) __tab = __tabs[0]
  if (__tab && (!__cur || __cur.targetId !== __tab.targetId)) {
    try { await browser.switchTab(__tab.targetId) } catch (e) {}
  }
}
globalThis.__egoBind = { space: __spaceName, spaceId: __space && __space.id,
                         tab: __tab ? __tab.targetId : '', reused: __onMine }
"""


def resolve_cli() -> str:
    """ego-browser CLI 路径（env 覆盖 → DSH 插件自带 → PATH）。"""
    for key in ("BROWSER_EGO_CLI", "EGO_BROWSER_CLI"):
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    if os.path.isfile(DEFAULT_CLI):
        return DEFAULT_CLI
    return shutil.which("ego-browser") or ""


def resolve_chrome() -> str:
    """Edge/Chrome 可执行文件路径（env 覆盖 → 常见 Edge 安装位置）。"""
    for key in CHROME_ENV:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def ego_available() -> bool:
    """ego 后端能否用：CLI + node + 浏览器三样都在。"""
    return bool(resolve_cli() and shutil.which("node") and resolve_chrome())


def _js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class EgoBrowserTool(BrowserTool):
    """命令协议同 BrowserTool，执行体换成 ego-lite。"""

    _EGO_COMMANDS = {
        "status", "goto", "url", "title", "text", "snapshot",
        "screenshot", "screenshot_base64", "click", "type", "fill",
        "press", "scroll", "js", "wait", "humancheck", "live",
        "tabs", "newtab", "switchtab", "spaces", "usespace", "upload",
    }

    #: 未知命令要给模型看得懂的支持清单
    _READONLY = {"status", "url", "title", "text", "snapshot",
                 "screenshot", "screenshot_base64", "tabs", "spaces"}

    @property
    def description(self) -> str:
        """在基类描述后补一句实际后端。"""
        return (super().description
                + "\n注意：当前走 ego（Edge 内核的共享浏览器，带观察窗），"
                  "支持这些命令：" + "、".join(sorted(self._EGO_COMMANDS))
                + "\n本会话有**自己的任务空间**（一组窗口与标签页）："
                  "`tabs` 看本空间已打开的页面、`newtab <url>` 再开一个、`switchtab <索引|id>` 切过去；"
                  "`spaces` 能看到所有空间（含你之前打开的站点所在的旧空间），"
                  "`usespace <名字|id|own>` 切到那个空间去操作它——"
                  "只会操作\"刚打开的那个页面\"通常是因为忘了先看 `tabs`/`spaces`。")

    @property
    def schema(self) -> dict:
        """把 command 枚举收成 ego 实际支持的命令（含基类没有的 spaces/usespace/live/humancheck）。"""
        s = super().schema
        try:
            props = s["properties"]["command"]
            if "enum" in props:
                # 只做减法会漏掉 ego 独有命令（模型在 function calling 里根本看不到它们）
                props["enum"] = sorted(self._EGO_COMMANDS)
        except Exception:
            pass
        return s

    def __init__(self, cli: str = None, space: str = None,
                 headless: bool = None, timeout: float = None):
        super().__init__(headless=True, persistent=False)
        from config import BROWSER_CONFIG
        self._cli = cli or resolve_cli()
        self._chrome = resolve_chrome()
        self._space = space or BROWSER_CONFIG.get("ego_space") or "my-agent"
        # 会话隔离：每个 agent 进程（或每个会话）一个任务空间，互不抢标签页
        self._isolate = bool(BROWSER_CONFIG.get("ego_isolate", True))
        self._space_name = self._session_space()
        self._headless = bool(BROWSER_CONFIG.get("ego_headless", False)
                              if headless is None else headless)
        self._timeout = float(timeout or BROWSER_CONFIG.get("ego_timeout", 120))
        # 光标浮层的名字：不设就用运行时默认值 "DeepSeek"，会被画进截图（徽标 + 页面标签）
        self._cursor_name = str(BROWSER_CONFIG.get("ego_cursor_name", "my_agent") or "")
        self._cursor_on = bool(BROWSER_CONFIG.get("ego_cursor", True))

    # ================================================================
    # 会话空间与标签页归属
    # ================================================================

    def _session_key(self) -> str:
        """本会话的稳定标识：宿主注入的会话 id 优先，其次进程 id。"""
        for key in ("MY_AGENT_SESSION_ID", "DSH_SESSION_ID"):
            value = str(os.getenv(key, "") or "").strip()
            if value:
                clean = re.sub(r"[^A-Za-z0-9_\-]", "", value)[:32].strip("-_")
                if clean:
                    return clean
        return f"p{os.getpid()}"

    def _session_space(self) -> str:
        """任务空间名：隔离关闭时用共享名（旧行为）。"""
        if not self._isolate:
            return self._space
        return f"{self._space}-{self._session_key()}"

    def _state_path(self) -> str:
        from config import PROJECT_ROOT
        name = getattr(self, "_space_name", "") or self._session_space()
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", name)
        return os.path.join(PROJECT_ROOT, "memory", "ego_tabs", f"{safe}.json")

    def _remembered_tab(self) -> str:
        """本会话在**当前活动空间**里记住的标签页（标签页按空间分开记，切空间不会串）。"""
        tabs = self._state().get("tabs")
        space = self._active_space()
        if isinstance(tabs, dict):
            return str(tabs.get(space) or "")
        # 兼容旧状态：只有 {space, tab} 两个字段时，只在空间对得上时才认那个标签页
        data = self._state()
        return str(data.get("tab") or "") if data.get("space") == space else ""

    def _state(self) -> dict:
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **fields) -> None:
        """把本会话的空间/标签页绑定落盘（下一次命令是另一个进程，要靠它复用）。"""
        path = self._state_path()
        data = self._state()
        data.update({k: v for k, v in fields.items() if v is not None})
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    def _remember_tab(self, tab: str) -> None:
        if not tab:
            return
        space = self._active_space()
        tabs = self._state().get("tabs")
        tabs = dict(tabs) if isinstance(tabs, dict) else {}
        tabs[space] = tab
        self._save_state(tabs=tabs, space=space)

    def _active_space(self) -> str:
        """本会话当前操作的空间：默认自己的隔离空间，`usespace` 可以切到别的（并记住）。"""
        override = str(getattr(self, "_space_override", "") or "")
        if override:
            return override
        return str(self._state().get("active") or self._space_name)

    def _bind_from_stdout(self, stdout: str) -> None:
        """从脚本输出里取回本次实际绑定的空间/标签页并记住（下一次进程复用同一个标签页）。"""
        marker = "__EGO_BIND__"
        for line in (stdout or "").splitlines():
            idx = line.find(marker)
            if idx < 0:
                continue
            try:
                payload = json.loads(line[idx + len(marker):])
            except ValueError:
                continue
            self._remember_tab(str(payload.get("tab") or ""))
            return

    # ================================================================
    # 脚本执行
    # ================================================================

    def _script(self, body: str) -> str:
        """拼一次执行：认领本会话空间 → 绑定自己的 tab → body → 哨兵回传。"""
        preamble = (SPACE_PREAMBLE
                    .replace("__EGO_SPACE__", _js(self._active_space()))
                    .replace("__EGO_TAB__", _js(self._remembered_tab())))
        return (
            preamble
            + "try { console.log('__EGO_BIND__' + JSON.stringify(globalThis.__egoBind)) } catch (e) {}\n"
            + "let __out = null\n"
            + "try {\n" + body + "\n"
            + f"  console.log({_js(SENTINEL)} + JSON.stringify({{ ok: true, __out }}))\n"
            + "} catch (e) {\n"
            + f"  console.log({_js(SENTINEL)} + JSON.stringify({{ ok: false, error: String((e && e.message) || e) }}))\n"
            + "}\n"
        )

    def _run(self, body: str, timeout: float = None) -> dict:
        """执行脚本，返回 {ok, __out|error}；CLI/node 缺失或超时也走同一形状。"""
        if not self._cli:
            return {"ok": False,
                    "error": "找不到 ego-browser CLI（设 BROWSER_EGO_CLI，或装 dsh-ego-browser）"}
        node = shutil.which("node")
        if not node:
            return {"ok": False, "error": "找不到 node（ego 运行时需要 Node ≥ 22）"}
        env = dict(os.environ)
        if self._chrome:
            env["EGO_LINUX_CHROME"] = self._chrome
        if self._headless:
            env["EGO_LINUX_HEADLESS"] = "1"
        # 浮层徽标默认叫 "DeepSeek"（运行时写死），改成自己的名字/或整体关掉
        if self._cursor_name:
            env["EGO_LINUX_CURSOR_NAME"] = self._cursor_name
        if not self._cursor_on:
            env["EGO_LINUX_CURSOR"] = "0"
        limit = float(timeout or self._timeout)
        try:
            proc = subprocess.run([node, self._cli], input=self._script(body), env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=limit)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"ego 脚本超时（>{limit:.0f}s）"}
        except OSError as e:
            return {"ok": False, "error": f"拉起 ego 运行时失败: {e}"}
        payload = None
        for line in (proc.stdout or "").splitlines():
            idx = line.find(SENTINEL)
            if idx >= 0:
                try:
                    payload = json.loads(line[idx + len(SENTINEL):])
                except ValueError:
                    payload = None
        self._bind_from_stdout(proc.stdout or "")     # 记住本次绑定的标签页，下次复用
        if payload is None:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return {"ok": False, "error": "ego 没有回传结果: " + " / ".join(tail)[:300]}
        return payload

    # ================================================================
    # 命令路由
    # ================================================================

    def execute(self, input_str: str) -> ToolResult:
        parts = (input_str or "").strip().split(maxsplit=1)
        if not parts:
            return ToolResult(success=False, output="", error="浏览器命令为空。")
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        if command not in self._EGO_COMMANDS:
            return ToolResult(
                success=False, output="",
                error=(f"ego 后端不支持命令 '{command}'。支持: "
                       f"{', '.join(sorted(self._EGO_COMMANDS))}"))
        try:
            return self._dispatch(command, args)
        except Exception as e:                       # noqa: BLE001
            return ToolResult(success=False, output="", error=f"ego 操作失败: {str(e)[:200]}")

    def is_parallel_safe(self, arguments) -> bool:
        cmd = str(arguments.get("command", "") or "").strip().lower().split()
        return bool(cmd) and cmd[0] in self._READONLY

    def _dispatch(self, command: str, args: str) -> ToolResult:
        if command == "status":
            return self._status()
        if command == "usespace":
            target = (args or "").strip()
            if not target:
                return ToolResult(success=False, output="",
                                  error="usespace 需要空间名或 id（用 spaces 看有哪些；own = 回到本会话空间）")
            if target.lower() in ("own", "mine", "本空间"):
                target = self._space_name
            body = self._body_for("usespace", target)
            if body is None:
                return ToolResult(success=False, output="", error="usespace 参数不合法。")
            # 先落状态再执行：脚本回传的标签页要记到**目标空间**名下，而不是切换前的那个
            self._save_state(active=target, space=target)
            result = self._run(body)
            payload = (result or {}).get("__out") or {}
            if result.get("ok") and payload.get("space"):
                self._save_state(active=str(payload["space"]), space=str(payload["space"]))
            else:
                self._save_state(active=self._space_name, space=self._space_name)
            return self._finish(result)
        if command == "goto":
            url = self._normalize_url(args)
            if not url:
                return ToolResult(success=False, output="", error="URL 为空。")
            return self._finish(self._run(self._goto_body(url)))
        if command == "screenshot_base64":
            return self._screenshot_base64()
        body = self._body_for(command, args)
        if body is None:
            return ToolResult(success=False, output="", error=f"ego 未实现命令: {command}")
        return self._finish(self._run(body))

    @staticmethod
    def _normalize_url(url: str) -> str:
        """补全省略的协议；data:/file:/about: 等带协议的地址原样放行。"""
        import re

        value = (url or "").strip()
        if not value:
            return ""
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", value):
            return value
        return "https://" + value

    @staticmethod
    def _goto_body(url: str) -> str:
        return (f"  await page.goto({_js(url)}, {{ waitUntil: 'domcontentloaded' }})\n"
                "  const __info = await page.info()\n"
                "  __out = { url: __info.url, title: __info.title }\n")

    def _body_for(self, command: str, args: str):
        """命令 → 脚本 body（返回 None 表示需要参数而参数不合法）。"""
        if command == "tabs":
            return ("  const __list = await browser.listTabs()\n"
                    "  const __cur = await browser.currentTab()\n"
                    "  __out = { space: globalThis.__egoBind && globalThis.__egoBind.space,\n"
                    "            tabs: __list.map((t, i) => ({ index: i, id: t.targetId, active: __cur ? t.targetId === __cur.targetId : false, title: t.title, url: t.url })) }\n")
        if command == "newtab":
            url = self._normalize_url(args) or "about:blank"
            return (f"  const __made = await ego.createTab({_js(url)})\n"
                    "  await browser.switchTab(__made.targetId)\n"
                    "  globalThis.__egoBind = { ...(globalThis.__egoBind || {}), tab: __made.targetId }\n"
                    "  __out = { id: __made.targetId, url: " + _js(url) + " }\n")
        if command == "switchtab":
            target = (args or "").strip()
            if not target:
                return None
            return ("  const __list = await browser.listTabs()\n"
                    f"  const __want = {_js(target)}\n"
                    "  const __hit = /^\\d+$/.test(__want) ? __list[Number(__want)]\n"
                    "      : __list.find(t => t.targetId === __want || t.targetId.startsWith(__want))\n"
                    "  if (!__hit) { __out = { error: 'no such tab', tabs: __list.map(t => t.targetId) } }\n"
                    "  else {\n"
                    "    await browser.switchTab(__hit.targetId)\n"
                    "    globalThis.__egoBind = { ...(globalThis.__egoBind || {}), tab: __hit.targetId }\n"
                    "    __out = { id: __hit.targetId, url: __hit.url }\n"
                    "  }\n")
        if command == "spaces":
            # 只看元数据：以前挨个 switch 进去列标签页，会把每个窗口都拉到前台闪一遍
            return ("  const __all = await ego.listTaskSpaces()\n"
                    "  const __rows = (__all.taskSpaces || []).map(s => ({\n"
                    "    id: s.id, name: s.name, ownership: s.ownership,\n"
                    "    tabs: (s.targetIds || []).length,\n"
                    "    urls: s.urls || [], titles: s.recentTabTitles || [] }))\n"
                    "  __out = { mine: globalThis.__egoBind && globalThis.__egoBind.space, spaces: __rows }\n")
        if command == "usespace":
            target = (args or "").strip()
            if not target:
                return None
            return ("  const __all = await ego.listTaskSpaces()\n"
                    f"  const __want = {_js(target)}\n"
                    "  const __list = __all.taskSpaces || []\n"
                    "  const __hit = /^\\d+$/.test(__want) ? __list.find(s => String(s.id) === __want)\n"
                    "      : __list.find(s => s.name === __want) || __list.find(s => s.name.startsWith(__want))\n"
                    "  if (!__hit) { __out = { error: 'no such space', spaces: __list.map(s => s.name) } }\n"
                    "  else {\n"
                    "    await taskSpaces.switch(__hit.id)\n"
                    "    await taskSpaces.claim(__hit.id)\n"
                    "    const __tabs = await browser.listTabs()\n"
                    "    let __tab = __tabs.find(t => t.targetId === __want) || __tabs[0]\n"
                    "    if (!__tab) { try { const __m = await ego.createTab('about:blank'); __tab = { targetId: __m.targetId } } catch (e) {} }\n"
                    "    if (__tab) { try { await browser.switchTab(__tab.targetId) } catch (e) {} }\n"
                    "    globalThis.__egoBind = { space: __hit.name, spaceId: __hit.id, tab: __tab ? __tab.targetId : '' }\n"
                    "    __out = { space: __hit.name, id: __hit.id, tabs: __tabs.map(t => ({ id: t.targetId, url: t.url })) }\n"
                    "  }\n")
        if command == "url":
            return ("  const __info = await page.info()\n"
                    "  __out = { url: __info.url, title: __info.title }\n")
        if command == "title":
            return ("  const __info = await page.info()\n"
                    "  __out = { title: __info.title, url: __info.url }\n")
        if command == "text":
            return "  __out = { text: await page.evaluate('document.body.innerText.slice(0, 8000)') }\n"
        if command == "snapshot":
            return "  __out = { snapshot: await page.snapshot() }\n"
        if command == "screenshot":
            return "  __out = { path: await page.screenshot() }\n"
        if command == "humancheck":
            return ("  const __info = await page.info()\n"
                    "  __out = { url: __info.url, snapshot: await page.snapshot() }\n")
        if command == "live":
            return "  __out = { note: 'ego 的实时观察窗在 DSH 侧，agent 侧无需开启' }\n"
        if command == "wait":
            try:
                seconds = min(float(args.strip() or 2.0), 30.0)
            except ValueError:
                return None
            return (f"  await page.waitForTimeout({int(seconds * 1000)})\n"
                    f"  __out = {{ waited: {seconds} }}\n")
        if command == "click":
            selector = args.strip()
            if not selector:
                return None
            return (f"  await page.locator({_js(selector)}).click()\n"
                    f"  __out = {{ clicked: {_js(selector)} }}\n")
        if command == "upload":
            # 文件选择框没法用键盘驱动：走 CDP 的 DOM.setFileInputFiles
            selector, _, path = args.partition(" ")
            selector, path = selector.strip(), path.strip().strip('"').strip("'")
            if not selector or not path:
                return None
            return (f"  await page.locator({_js(selector)}).setInputFiles({_js(path)})\n"
                    f"  __out = {{ uploaded: {_js(path)}, selector: {_js(selector)} }}\n")
        if command in ("type", "fill"):
            selector, _, text = args.partition(" ")
            if not selector or not text:
                return None
            return (f"  await page.locator({_js(selector)}).fill({_js(text)})\n"
                    f"  __out = {{ filled: {_js(selector)}, chars: {len(text)} }}\n")
        if command == "press":
            key = args.strip()
            if not key:
                return None
            return (f"  await page.keyboard.press({_js(key)})\n"
                    f"  __out = {{ pressed: {_js(key)} }}\n")
        if command == "scroll":
            return self._scroll_body(args)
        if command == "js":
            if not args.strip():
                return None
            return f"  __out = {{ result: await page.evaluate({_js(args)}) }}\n"
        return None

    @staticmethod
    def _scroll_body(args: str) -> str:
        """scroll <down|up|top|bottom|像素>: 用 scrollBy，不依赖 harness 的滚轮 API。"""
        token = (args or "").strip().lower() or "down"
        if token in ("top", "bottom"):
            expr = "0" if token == "top" else "document.body.scrollHeight"
            return (f"  await page.evaluate('window.scrollTo(0, {expr})')\n"
                    f"  __out = {{ scrolled: {_js(token)} }}\n")
        if token in ("down", "up"):
            delta = 600 if token == "down" else -600
        else:
            try:
                delta = int(float(token)) * 100
            except ValueError:
                return "  __out = { scrolled: false }\n"
        return (f"  await page.evaluate('window.scrollBy(0, {delta})')\n"
                f"  __out = {{ scrolled: {delta} }}\n")

    # ================================================================
    # 结果渲染
    # ================================================================

    def _finish(self, payload: dict) -> ToolResult:
        if not payload.get("ok"):
            return ToolResult(success=False, output="",
                              error=str(payload.get("error") or "ego 执行失败"))
        data = payload.get("__out")
        if not isinstance(data, dict):
            return ToolResult(success=True, output=str(data if data is not None else "完成。"))
        if "snapshot" in data:
            return self._snapshot_result(str(data.get("snapshot") or ""), str(data.get("url") or ""))
        if "text" in data:
            return ToolResult(success=True, output=str(data["text"]))
        if "path" in data:
            path = str(data["path"])
            size = os.path.getsize(path) if os.path.exists(path) else 0
            return ToolResult(success=True,
                              output=f"截图已保存: {path}（{size} 字节）",
                              metadata={"path": path})
        return ToolResult(success=True, output="\n".join(f"{k}: {v}" for k, v in data.items()))

    def _snapshot_result(self, snapshot: str, url: str = "") -> ToolResult:
        """快照附带人机验证提示（命中就该交给人）。"""
        hint = human_check.hint_for_url(url or "")
        text = snapshot or "（快照为空）"
        if hint:
            info = human_check.verdict(url=url, text=text)
            return ToolResult(success=True,
                              output=f"{info['hint']}\n命中信号: {'；'.join(info['reasons'])}\n\n{text[:8000]}",
                              metadata={"human_check": True, "reasons": info["reasons"]})
        return ToolResult(success=True, output=text[:12000])

    def _screenshot_base64(self) -> ToolResult:
        """截图 → 读文件 → base64 放 metadata（不走 output，免得被截断成坏图）。"""
        payload = self._run("  __out = { path: await page.screenshot() }\n")
        if not payload.get("ok"):
            return ToolResult(success=False, output="",
                              error=str(payload.get("error") or "ego 截图失败"))
        path = str((payload.get("__out") or {}).get("path") or "")
        if not path or not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"ego 截图文件不存在: {path!r}")
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        return ToolResult(
            success=True,
            output=f"截图已获取（ego）。\n文件: {path}\n大小: {len(data) * 3 // 4} 字节",
            metadata={"screenshot_base64": data, "path": path})

    def _status(self) -> ToolResult:
        """后端自检：CLI/node/浏览器路径 + 浏览器连接状态。"""
        lines = [f"后端: ego（Edge 内核，共享浏览器）",
                 f"CLI: {self._cli or '（未找到）'}",
                 f"浏览器: {self._chrome or '（未找到）'}",
                 f"任务空间: {self._active_space()}"
                 + (f"（本会话空间 {self._space_name}；用 browser spaces 看全部，"
                    f"browser usespace <名字> 切过去）" if self._active_space() != self._space_name
                    else f"（隔离: {'开' if self._isolate else '关'}）"),
                 f"本空间记住的页面: {self._remembered_tab() or '（还没有，下一条命令会自动开一个）'}",
                 f"无头: {self._headless}"]
        if not self._cli:
            return ToolResult(success=True, output="\n".join(lines + ["状态: CLI 缺失"]))
        node = shutil.which("node")
        if node:
            try:
                proc = subprocess.run([node, self._cli, "--status"], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=30)
                lines.append("状态: " + (proc.stdout or proc.stderr or "").strip()[:300])
            except Exception as e:                   # noqa: BLE001
                lines.append(f"状态: 查询失败（{str(e)[:120]}）")
        return ToolResult(success=True, output="\n".join(lines))
