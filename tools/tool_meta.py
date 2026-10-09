"""工具目录工具（tools）：按需装载低频工具，别让它们的 schema 常驻提示词。

门控名单见 `tools.tool_manager.GATED_TOOLS`：那些工具默认不发 schema，模型装载后
从下一轮起就能正常 function calling 调用。装载只影响"发不发 schema"，不影响工具能力。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

_HELP = """工具目录（tools）：低频工具的 schema 默认不常驻提示词，需要时先装载再用。
  tools list              查看未常驻的工具（含一句话说明、是否已装载）
  tools load <名字...>     装载（多个用空格或逗号分隔）→ 下一轮起可调用
  tools unload <名字...>   卸下不再需要的，省回提示词
说明：浏览器/终端/文件/编辑/Python/视觉等常用工具本来就常驻，无需装载。"""


class ToolsTool(BaseTool):
    """按需装载门控工具：只决定 schema 发不发，不改变工具本身的能力。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"
    parallel_safe: bool = False         # 会改装载状态，别与别的调用并行

    def __init__(self, manager=None):
        self._manager = manager

    @property
    def name(self) -> str:
        return "tools"

    @property
    def description(self) -> str:
        return _HELP

    @property
    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "load", "unload"],
                    "description": "list 查看 / load 装载 / unload 卸下",
                },
                "names": {
                    "type": "string",
                    "description": "工具名，多个用空格或逗号分隔（list 时可省略）",
                },
            },
            "required": ["action"],
        }

    @staticmethod
    def _split(names: Any) -> list:
        if isinstance(names, (list, tuple)):
            parts = [str(n) for n in names]
        else:
            parts = str(names or "").replace(",", " ").replace("，", " ").split()
        return [p.strip() for p in parts if p.strip()]

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`tools list` / `tools load zhihu video_edit`。"""
        text = str(input_str or "").strip()
        if not text:
            return self.execute_json({"action": "list"})
        parts = text.split(None, 1)
        return self.execute_json({
            "action": parts[0].lower(),
            "names": parts[1] if len(parts) > 1 else "",
        })

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        manager = self._manager
        if manager is None:
            return ToolResult(success=False, output="", error="工具目录不可用（未绑定管理器）。")
        action = str((arguments or {}).get("action") or "list").strip().lower()
        names = self._split((arguments or {}).get("names"))

        if action == "list":
            return ToolResult(success=True, output=manager.tools_hint(detail=True))

        if action in ("load", "unload"):
            if not names:
                return ToolResult(success=False, output="",
                                  error="请给出工具名，例如 tools load zhihu video_edit。")
            if action == "load":
                loaded, unknown = manager.mark_loaded(names)
            else:
                loaded, unknown = manager.mark_unloaded(names)
            verb = "已装载" if action == "load" else "已卸下"
            lines = [f"{verb}: {', '.join(loaded)}" if loaded else "没有变化。"]
            if unknown:
                lines.append(f"未知或非法工具名（忽略）: {', '.join(unknown)}")
            lines.append("装载的工具从下一轮起可直接调用。" if action == "load"
                         else "卸下的工具不再出现在提示词里，需要时再 load。")
            return ToolResult(success=True, output="\n".join(lines),
                              metadata={"loaded": loaded, "unknown": unknown})

        return ToolResult(success=False, output="",
                          error=f"未知 action: {action}（可用: list / load / unload）")
