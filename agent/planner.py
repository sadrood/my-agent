"""
规划器模块。
负责调用 LLM 将用户目标分解为可执行的步骤列表。
"""
from typing import Optional

from models.llm import LLM
from models.prompts import (
    PLANNER_SYSTEM_PROMPT,
    PLANNER_USER_PROMPT_TEMPLATE,
    REPLAN_SYSTEM_PROMPT,
    REPLAN_USER_PROMPT_TEMPLATE,
)


class Planner:
    """任务规划器，将目标分解为步骤计划。"""

    def __init__(self, llm: Optional[LLM] = None):
        """
        Args:
            llm: LLM 实例，如不传入则自动创建。
        """
        self.llm = llm or LLM()

    def create_plan(self, goal: str, experience_context: str = "",
                     strategy_hints: str = "",
                     conversation_context: str = "") -> list[str]:
        """
        根据目标生成执行计划。

        Args:
            goal: 用户目标描述（可能已包含意图检测增强信息）。
            experience_context: 历史经验上下文（从 memory 召回）。
            strategy_hints: 策略建议（从 memory 召回）。
            conversation_context: 对话上下文（跨轮次记忆）。

        Returns:
            步骤列表，如 ["1. 打开浏览器", "2. 搜索资料", ...]
        """
        # 组装完整的用户提示
        user_content = PLANNER_USER_PROMPT_TEMPLATE.format(
            goal=goal,
            experience_context=experience_context,
            strategy_hints=strategy_hints,
            conversation_context=conversation_context,
        )
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        response = self.llm.chat(messages)
        return self._parse_plan(response)

    def replan(
        self,
        goal: str,
        original_plan: list[str],
        completed_steps: list[dict],
        failed_step: str,
        error_message: str,
        failure_warnings: str = "",
    ) -> list[str]:
        """
        当某步骤失败时，重新规划剩余步骤。

        Args:
            goal: 原始目标。
            original_plan: 原始计划步骤列表。
            completed_steps: 已完成的步骤及其结果。
            failed_step: 失败的步骤描述。
            error_message: 失败原因。
            failure_warnings: 已知失败模式警告（用于避免重蹈覆辙）。

        Returns:
            新的剩余步骤列表。
        """
        completed_str = "\n".join(
            f"- {s['step']}: {s.get('result', '已完成')}"
            for s in completed_steps
        )
        original_str = "\n".join(original_plan)

        messages = [
            {"role": "system", "content": REPLAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": REPLAN_USER_PROMPT_TEMPLATE.format(
                    goal=goal,
                    original_plan=original_str,
                    completed_steps=completed_str or "无",
                    failed_step=failed_step,
                    error_message=error_message,
                    failure_warnings=failure_warnings,
                ),
            },
        ]

        response = self.llm.chat(messages)
        return self._parse_plan(response)

    @staticmethod
    def _parse_plan(raw_response: str) -> list[str]:
        """
        解析 LLM 返回的计划文本，提取为步骤列表。

        Args:
            raw_response: LLM 原始响应文本。

        Returns:
            步骤列表。
        """
        lines = raw_response.strip().split("\n")
        steps = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 匹配 "1. xxx" 或 "1) xxx" 或 "- xxx" 格式
            if line[0].isdigit():
                # 去掉编号前缀，如 "1. " 或 "1) "
                for sep in (". ", ") ", ".", ")"):
                    idx = line.find(sep)
                    if 0 < idx <= 3:
                        line = line[idx + len(sep):].strip()
                        break
                if line:
                    steps.append(line)
            elif line.startswith("- ") or line.startswith("* "):
                steps.append(line[2:].strip())
        return steps
