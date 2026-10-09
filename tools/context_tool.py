"""上下文取回工具（context）：把零驻留折叠掉的原文逐字取回。

大工具输出不再整段留在对话里（只留 `[已折叠 #句柄 12.3KB]` 指针），需要原文时用
`context recall <句柄>`；`context list`/`stats` 看本机折了哪些、省了多少。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

_HELP = """上下文取回（context）：大工具输出只留指针，原文按句柄取回。
  context recall <句柄>   逐字取回被折叠的原文（句柄见消息里的 [已折叠 #...]）
  context list [命名空间] 列出本机已折叠的条目（句柄/体积/首行）
  context stats           零驻留账本：条目数、总字节、估算省下的 token
说明：指针里的内容是**原文**，不是摘要——看到 [已折叠 #xxx] 需要细节时就 recall。"""


class ContextTool(BaseTool):
    """零驻留上下文的取回入口（只读本地落盘的原文）。"""

    risk_level: str = "low"
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"
    parallel_safe: bool = True

    @property
    def name(self) -> str:
        return "context"

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
                    "enum": ["recall", "list", "stats"],
                    "description": "recall 取回原文 / list 列条目 / stats 看账本",
                },
                "handle": {
                    "type": "string",
                    "description": "句柄（recall 必填），形如 run-1759-0007",
                },
                "namespace": {
                    "type": "string",
                    "description": "命名空间（list 可选，缺省列全部）",
                },
            },
            "required": ["action"],
        }

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`context recall <句柄>` / `context list` / `context stats`。"""
        text = str(input_str or "").strip()
        if not text:
            return self.execute_json({"action": "stats"})
        parts = text.split(None, 1)
        args = {"action": parts[0].lower()}
        if len(parts) > 1:
            key = "handle" if args["action"] == "recall" else "namespace"
            args[key] = parts[1].strip()
        return self.execute_json(args)

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        from agent import context_store as store

        action = str((arguments or {}).get("action") or "").strip().lower()

        if action == "recall":
            handle = str((arguments or {}).get("handle") or "").strip().lstrip("#")
            if not handle:
                return ToolResult(success=False, output="", error="recall 需要句柄，例如 context recall run-1-0007")
            text = store.load(handle)
            if text is None:
                return ToolResult(success=False, output="",
                                  error=f"取不回 #{handle}（句柄不存在或落盘已被清理）。")
            return ToolResult(success=True, output=text,
                              metadata={"handle": handle, "chars": len(text)})

        if action == "list":
            items = store.entries(str((arguments or {}).get("namespace") or "").strip())
            if not items:
                return ToolResult(success=True, output="暂无已折叠的条目。")
            lines = [f"已折叠条目 {len(items)} 条："]
            for handle, size, head in items[:50]:
                lines.append(f"  #{handle}  {size/1024:.1f}KB  {head[:70]}")
            if len(items) > 50:
                lines.append(f"  …（还有 {len(items) - 50} 条）")
            return ToolResult(success=True, output="\n".join(lines))

        if action == "stats":
            data = store.stats(str((arguments or {}).get("namespace") or "").strip())
            enabled = store.enabled()
            return ToolResult(
                success=True,
                output=(f"零驻留账本：{'开启' if enabled else '已关闭'}，"
                        f"折叠 {data['entries']} 条 / {data['bytes']/1024:.1f}KB，"
                        f"估算省下 ≈{data['saved_tokens_estimate']:,} token。\n"
                        f"落盘目录：{store.store_root()}"),
                metadata=data,
            )

        return ToolResult(success=False, output="",
                          error=f"未知 action: {action}（可用: recall / list / stats）")
