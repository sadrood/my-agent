"""向用户提问并等回答（`ask_user`）。

为什么需要：模型遇到真正需要人拍板的分叉（"用 A 还是 B"、"要删哪个文件"）时，此前只能把
问题写进最终回答 —— 但那样**这一轮就结束了**，用户回答等于开新一轮，上下文与进度都断。
本工具让模型在**任务中途**停下来问一句、拿到答案再继续。

与审批门的分工：审批是"这个动作准不准"（策略驱动，人被动应答）；本工具是"我不知道该怎么办"
（模型主动求信息）。所以它不改审批策略，只借同一条"提示符 + 输出闸门"的通道。

注意：TUI 模式（agent/tui.py）接管终端后，这里的 input() 会和它抢终端 —— 那时要改成把
问题投给输入框、等它回调（同 on_submit 那条路）。line 模式不受影响。
"""

import sys
from typing import Any, Dict, List

from tools.base import BaseTool, ToolResult

#: 非交互时给模型的话：必须明确"问不到人"，否则模型会以为用户默许
_UNREACHABLE = (
    "用户当前不可达（非交互运行 / 未开终端）。请**不要**再调用本工具，"
    "改为自行选择最稳妥的方案继续，并在最终结论里写明你做了哪些假设。"
)


def _interactive_ok() -> bool:
    """能不能真的问到人：要有终端，且审批策略没关掉交互。"""
    try:
        if not bool(getattr(sys.stdin, "isatty", lambda: False)()):
            return False
        from config import APPROVAL_CONFIG
        return bool(APPROVAL_CONFIG.get("interactive", True))
    except Exception:
        return False


class AskTool(BaseTool):
    """让模型中途向用户提问（可选给几个选项）。"""

    name = "ask_user"
    description = (
        "向用户提问并等待回答（任务中途需要人拍板时用）。"
        "question 是要问的话；options 可选，给几个候选（用户可回编号或直接文字）。"
        "只在真正需要人类决策/信息时用；别拿它问自己能查到或能推断的事，也别用它确认琐事。"
    )
    risk_level = "low"
    approval = "auto"
    min_sandbox_mode = "read-only"
    #: 阻塞等人，不能和其他工具并跑（否则问题会和别的输出/输入缠在一起）
    parallel_safe = False

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题（一句话说清）"},
                "options": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选的候选答案；用户可以直接回编号，也可以自己输入",
                },
                "default": {
                    "type": "string",
                    "description": "可选：用户直接回车时采用的默认答案",
                },
            },
            "required": ["question"],
        }

    # 兼容字符串入口：`ask_user 用哪个方案？`
    def execute(self, input_str: str) -> ToolResult:
        return self.execute_json({"question": str(input_str or "").strip()})

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        arguments = arguments or {}
        question = str(arguments.get("question") or "").strip()
        if not question:
            return ToolResult(success=False, output="", error="question 为空：请说明你要问什么。")
        options: List[str] = [str(o).strip() for o in (arguments.get("options") or [])
                              if str(o).strip()]
        default = str(arguments.get("default") or "").strip()

        if not _interactive_ok():
            return ToolResult(success=False, output="", error=_UNREACHABLE)

        # 复用主循环那套读取：开输出闸门（别的线程的输出先排队，别覆盖用户敲的字）、
        # 粘贴感知、Ctrl+C/EOF 都能正确处理
        from agent import input_reader

        def _ask() -> str:
            print()
            print(f"❓ {question}")
            for i, opt in enumerate(options, 1):
                print(f"   {i}) {opt}")
            tail = f"（回车=「{default}」）" if default else ""
            return input_reader.read_goal(
                prompt_primary=lambda: _prompt(f"   你的回答{tail}: "),
                prompt_continuation=lambda: _prompt("   … "),
            )

        try:
            answer = _ask() or ""
        except (EOFError, KeyboardInterrupt):
            return ToolResult(success=True, output="",
                              error="用户中断了提问（Ctrl+C），没有作答。")
        answer = answer.strip()
        if not answer:
            answer = default
        if not answer:
            return ToolResult(success=True, output="用户直接回车，未作答。请自行决定并说明假设。")

        # 用户回编号：翻成选项原文，免得模型还要自己猜编号对应哪条
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            picked = options[int(answer) - 1]
            return ToolResult(success=True, output=f"用户选择：{picked}（第 {answer} 项）")
        return ToolResult(success=True, output=f"用户回答：{answer}")


def _prompt(text: str) -> None:
    """打印提示符但不换行（与 input_reader 的回调约定一致）。"""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass
