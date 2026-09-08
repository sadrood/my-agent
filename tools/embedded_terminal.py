"""
嵌入式终端工具（桌面端"内置终端"）。

与 EmbeddedBrowserTool 同思路：Electron 主进程在 8091 起 HTTP 桥
（POST /terminal），Python Agent 的 terminal 命令经桥在桌面端执行并回显到
内置终端面板——用户与 Agent 看到同一条命令流（所见即所跑）。

CLI 场景（无桌面桥环境变量 / 探测失败）由 ToolManager 回退本地 TerminalTool，
本模块只在桌面端生效。
"""
import json as _json
import os
import urllib.request as _urlreq
from typing import Any, Dict

from tools.base import ToolResult
from tools.terminal import TerminalTool


class EmbeddedTerminalTool(TerminalTool):
    """命令经 Electron 桥执行并回显到内置终端面板的 terminal 工具。"""

    def __init__(self, bridge_url: str = None):
        super().__init__()
        self._bridge_url = (
            bridge_url
            or os.getenv("MY_AGENT_EMBEDDED_TERMINAL_URL")
            or "http://127.0.0.1:8091/terminal"
        ).rstrip("/")

    def _call_bridge(self, command: str) -> dict:
        payload = _json.dumps({"action": "run", "command": command}).encode("utf-8")
        req = _urlreq.Request(
            self._bridge_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _urlreq.urlopen(req, timeout=320) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {
                "ok": False,
                "error": (
                    f"内嵌终端桥不可达（{str(e)[:120]}）。"
                    "请确认桌面端正在运行（桥端口 8091）。"
                ),
            }

    def execute(self, input_str: str) -> ToolResult:
        command = input_str.strip()
        if not command:
            return ToolResult(success=False, output="", error="命令为空。")
        # 后台任务管理命令走本地（后台进程本就跑在 Agent 机器上，无需桥接）
        if command.lower().startswith("bg "):
            return super().execute(command)

        # 硬性黑名单在工具层同样拦截（与本地 terminal 一致的安全底线）
        from agent.approval import CommandSafety
        if CommandSafety.classify(command) == "blocked":
            return ToolResult(
                success=False,
                output="",
                error=f"安全限制：命令 '{command[:100]}' 命中硬性黑名单，已拒绝执行。",
            )

        resp = self._call_bridge(command)
        if resp.get("ok"):
            return ToolResult(
                success=True,
                output=str(resp.get("output", "") or "(无输出)"),
                metadata={"embedded_terminal": True},
            )
        return ToolResult(
            success=False, output="", error=str(resp.get("error", "桥调用失败"))
        )

    def _run_command(self, command: str, background: bool = False) -> ToolResult:
        # 桌面端一律走桥（本地只保留 bg 管理的兜底路径）
        if background:
            return super()._run_command(command, background=True)
        return self.execute(command)

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        # 与本地 terminal 同规则：低风险（查询/只读类）命令可并行
        from agent.approval import CommandSafety
        return CommandSafety.classify(str(arguments.get("command", "") or "")) == "low"
