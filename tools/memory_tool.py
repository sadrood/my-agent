"""记忆库管理工具（memory）：经验库体检 + 经验压缩。

为什么单独一个工具：经验库是"越用越值钱"的资产，但它同时会越用越脏——实测某次
体检发现 159 条里 117 条是同一类零散记录、37% 是一句话问答。这个工具让 **agent
自己**也能定期做两件事：

    memory status          经验库体检（条数/类别分布/噪音占比）
    memory distill         把零散经验按类别总结成高层条目（**原始记录先归档**）
    memory distill --dry-run  只预览会压成什么，不落盘

安全：蒸馏前先把原始记录整份归档到 `experiences_raw_<时间>.json`，归档失败就
放弃压缩——压缩绝不能以丢历史为代价。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

_HELP = """记忆库管理（memory 工具）：
  memory status            经验库体检：条数、类别分布、蒸馏/原始占比
  memory distill           把零散经验按类别总结成高层经验条目（原始记录先归档，不丢）
  memory distill --dry-run 预览压缩结果（不写盘）
说明：压缩是**可逆**的——原始记录整份存到 experiences_raw_<时间>.json；
      少于 3 条的类别不动（不值得为两条调模型）。"""


class MemoryTool(BaseTool):
    """经验库体检与压缩。"""

    risk_level: str = "medium"          # 会调用模型并改写经验库（有归档兜底）
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"
    parallel_safe: bool = False         # 改写共享的记忆文件，不能并发

    def __init__(self, memory=None, llm=None):
        self._memory = memory
        self._llm = llm

    def set_memory(self, memory) -> None:
        """注入**当前会话**的 Memory（经验库是按会话分文件的：
        memory/<chat_id>/experiences.json——不注入就会操作到 default 那份）。"""
        self._memory = memory

    def set_llm(self, llm) -> None:
        self._llm = llm

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "记忆库管理：经验库体检与**经验压缩**。\n"
            "  memory status            看经验库有多少条、类别分布、多少是零散噪音\n"
            "  memory distill           把零散经验按类别总结成高层经验（做法/坑/依据），"
            "原始记录先归档到 experiences_raw_*.json，压缩可回溯\n"
            "  memory distill --dry-run 先预览压缩结果\n"
            "什么时候用：经验库很乱、召回总是不相关、或用户说「把经验总结一下/压缩一下」时。"
            "压缩后召回同样 3 条能装下真正可复用的知识。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["status", "distill"],
                              "description": "status=体检；distill=压缩经验库"},
                "dry_run": {"type": "boolean",
                            "description": "distill 时只预览不写盘（默认 false）"},
                "min_group": {"type": "integer",
                              "description": "少于这么多条的类别不压缩（默认 3）"},
            },
            "required": ["operation"],
        }

    # ------------------------------------------------------------

    def _get_memory(self):
        if self._memory is not None:
            return self._memory
        try:
            from agent.memory import Memory
            self._memory = Memory()
        except Exception:                       # noqa: BLE001
            self._memory = None
        return self._memory

    def _get_llm(self):
        """压缩用的 LLM：优先 LEARN_DISTILL_* 配置，其次注入的 llm，最后自建。"""
        from agent.memory import build_distill_llm
        llm = build_distill_llm(self._llm)
        if llm is None:
            try:
                from models.llm import LLM
                llm = LLM()
            except Exception:                   # noqa: BLE001
                llm = None
        self._llm = llm
        return llm

    @staticmethod
    def _render_status(stats: dict) -> str:
        cats = "、".join(f"{k} {v}" for k, v in
                         sorted(stats["categories"].items(), key=lambda kv: -kv[1]))
        lines = [
            f"经验库：{stats['total']} 条（其中蒸馏条目 {stats['distilled']}、"
            f"原始记录 {stats['raw']}）",
            f"过短目标（<8 字，疑似闲聊）：{stats['short_goals']} 条",
            f"类别分布：{cats or '（空）'}",
        ]
        if stats["total"] and stats["raw"] > 20:
            lines.append("建议：跑一次 `memory distill` 把零散记录压成高层经验"
                         "（原始记录会先归档）。")
        return "\n".join(lines)

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "") or "").strip().lower()
        if op in ("help", ""):
            return ToolResult(success=True, output=_HELP)
        memory = self._get_memory()
        if memory is None:
            return ToolResult(success=False, output="", error="记忆模块不可用。")

        if op == "status":
            return ToolResult(success=True, output=self._render_status(
                memory.experience_stats()))

        if op != "distill":
            return ToolResult(success=False, output="",
                              error=f"未知操作 {op!r}（status / distill）")

        llm = self._get_llm()
        if llm is None:
            return ToolResult(success=False, output="",
                              error="总结需要一个可用的 LLM（检查 .env 的 LLM_*）。")
        dry = bool(arguments.get("dry_run"))
        try:
            min_group = int(arguments.get("min_group") or 3)
        except (TypeError, ValueError):
            min_group = 3
        try:
            result = memory.distill_experiences(llm, min_group=min_group, dry_run=dry)
        except Exception as e:                  # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"经验压缩失败: {type(e).__name__}: {str(e)[:200]}")

        if not result.get("groups"):
            return ToolResult(success=True, output=result.get("note") or "无需压缩。")

        lines = [("（预览，未写盘）" if dry else "") +
                 f"经验压缩：{result['before']} 条 → {result['after']} 条"]
        for g in result["groups"]:
            lines.append(f"  [{g['category']}] {g['records']} 条记录 → {g['made']} 条经验")
            for title in g["titles"]:
                lines.append(f"      · {title}")
        if result.get("archived"):
            lines.append(f"  原始记录已归档: {result['archived']}")
        if result.get("error"):
            lines.append(f"  部分未处理: {result['error']}")
        if dry and result.get("preview"):
            lines.append("\n--- 预览 ---")
            lines.extend(result["preview"])
        return ToolResult(
            success=True, output="\n".join(lines),
            metadata={"before": result["before"], "after": result["after"],
                      "archived": result.get("archived", ""), "dry_run": dry})

    def execute(self, input_str: str) -> ToolResult:
        """文本协议：status / distill [--dry-run]"""
        text = (input_str or "").strip().lower()
        if not text or text in ("help", "?"):
            return ToolResult(success=True, output=_HELP)
        if text.startswith("status"):
            return self.execute_json({"operation": "status"})
        if text.startswith("distill") or text.startswith("压缩") or text.startswith("总结"):
            return self.execute_json({"operation": "distill",
                                      "dry_run": "--dry-run" in text or "预览" in text})
        return ToolResult(success=False, output="", error=f"未知命令 {text[:20]!r}（status / distill）")
