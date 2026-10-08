import base64
import json
import os
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

#: 每次脚本先认领任务空间并切到真实标签页：新进程常落在空白/陈旧 tab 上
SPACE_PREAMBLE = """const __tabs = await browser.listTabs()
const __real = __tabs.find(t => !t.url.startsWith('about:') && !t.url.startsWith('chrome://')) ?? __tabs[0]
if (__real) await browser.switchTab(__real.targetId)
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
    }

    #: 未知命令要给模型看得懂的支持清单
    _READONLY = {"status", "url", "title", "text", "snapshot",
                 "screenshot", "screenshot_base64"}

    @property
    def description(self) -> str:
        """在基类描述后补一句实际后端。"""
        return (super().description
                + "\n注意：当前走 ego（Edge 内核的共享浏览器，带观察窗），"
                  "支持这些命令：" + "、".join(sorted(self._EGO_COMMANDS)))

    @property
    def schema(self) -> dict:
        """把 command 枚举收窄到 ego 支持的集合。"""
        s = super().schema
        try:
            props = s["properties"]["command"]
            if "enum" in props:
                props["enum"] = [c for c in props["enum"] if c in self._EGO_COMMANDS]
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
        self._headless = bool(BROWSER_CONFIG.get("ego_headless", False)
                              if headless is None else headless)
        self._timeout = float(timeout or BROWSER_CONFIG.get("ego_timeout", 120))

    # ================================================================
    # 脚本执行
    # ================================================================

    def _script(self, body: str) -> str:
        """拼一次执行：认领空间 → 切真实 tab → body → 哨兵回传。"""
        return (
            f"const task = await taskSpaces.useOrCreate({_js(self._space)})\n"
            + SPACE_PREAMBLE
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
                 f"任务空间: {self._space}",
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
