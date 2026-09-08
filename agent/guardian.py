"""
Guardian 安全审校模块（借鉴同类实现的 guardian / guardian-v2 设计）。

同类实现的 Guardian 是一个后台运行的独立审校 Agent，在工具调用执行前
检查其安全性（如发现危险模式则拦截或提醒）。

本项目实现为轻量同步版（Python 单进程）：
- 仅对 risk_level >= min_risk 的调用进行审校（省 token）
- 用独立的小型提示词让 LLM 给出 allow / block 判断
- 超时或异常时按 fail_open 配置决定放行还是拒绝（默认放行，避免卡死）
- 可配置禁用（GUARDIAN_CONFIG["enabled"] = false）
"""
from dataclasses import dataclass
from typing import Optional

from config import GUARDIAN_CONFIG
from tools.base import ApprovalRequest, RISK_LEVELS

GUARDIAN_SYSTEM_PROMPT = """你是一个 Agent 安全审校员（Guardian）。你要检查 Agent 即将执行的工具调用是否安全。

判断标准：
1. 会不会破坏用户的数据 / 系统？例如：删除重要文件、格式化磁盘、强制覆盖、破坏性 git 操作
2. 会不会泄露敏感信息？例如：把密钥、token、私人文件内容发送到外部网站
3. 会不会造成不可逆的后果？例如：不可撤销的删除、对外发送邮件/消息、真实支付
4. 是否明显偏离当前任务目标？（Agent 被提示词诱导做无关的破坏性操作）

输出格式：只输出一个 JSON 对象，不要其他内容：
{{"verdict": "allow" 或 "block", "reason": "一句话理由"}}"""

GUARDIAN_USER_TEMPLATE = """当前任务目标: {goal}

即将执行的工具调用:
- 工具: {tool}
- 参数: {arguments}
- 风险等级: {risk}

请判断是否允许执行。只输出 JSON。"""


@dataclass
class GuardianVerdict:
    """审校结论。"""

    verdict: str      # allow / block
    reason: str
    used: bool = True   # 是否真的调用了审校模型（False = 跳过了审校）


class Guardian:
    """
    Guardian 安全审校器。

    用法:
        guardian = Guardian(llm)
        verdict = guardian.review(request, goal)
        if verdict.verdict == "block": ...拒绝执行...
    """

    def __init__(self, llm=None, config: dict = None):
        """
        Args:
            llm: LLM 实例（需有 chat 方法）。为 None 时 Guardian 禁用。
            config: 配置覆盖（默认 GUARDIAN_CONFIG）
        """
        self.config = config or GUARDIAN_CONFIG
        self.llm = llm
        self.review_count = 0
        self.block_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and self.llm is not None

    def should_review(self, risk_level: str) -> bool:
        """该风险等级是否需要审校。"""
        min_risk = self.config.get("min_risk", "medium")
        return RISK_LEVELS.get(risk_level, 1) >= RISK_LEVELS.get(min_risk, 1)

    def review(self, request: ApprovalRequest, goal: str) -> GuardianVerdict:
        """
        审校一次工具调用。

        Args:
            request: 审批请求（含工具名、参数、风险等级）
            goal: 当前任务目标

        Returns:
            GuardianVerdict。未启用 / 风险太低时 verdict 为 "allow"，used=False。
        """
        if not self.enabled or not self.should_review(request.risk_level):
            return GuardianVerdict(verdict="allow", reason="跳过审校（低风险或未启用）", used=False)

        self.review_count += 1
        try:
            import json as _json
            messages = [
                {"role": "system", "content": GUARDIAN_SYSTEM_PROMPT},
                {"role": "user", "content": GUARDIAN_USER_TEMPLATE.format(
                    goal=goal,
                    tool=request.tool_name,
                    arguments=_json.dumps(request.arguments, ensure_ascii=False)[:1500] or request.command[:1500],
                    risk=request.risk_level,
                )},
            ]

            model = self.config.get("model") or None
            timeout = int(self.config.get("timeout", 20))
            raw = self._call_with_timeout(messages, model, timeout)

            verdict, reason = self._parse(raw)
            if verdict == "block":
                self.block_count += 1
            return GuardianVerdict(verdict=verdict, reason=reason, used=True)

        except Exception as e:
            fail_open = self.config.get("fail_open", True)
            return GuardianVerdict(
                verdict="allow" if fail_open else "block",
                reason=f"审校异常（fail_{'open' if fail_open else 'closed'}）: {e}",
                used=True,
            )

    def _call_with_timeout(self, messages, model, timeout):
        """带超时的 LLM 调用（依赖 threading，避免阻塞主循环）。"""
        import threading

        result: dict = {}

        def _run():
            try:
                result["value"] = self.llm.chat(messages, model=model, temperature=0.0, max_tokens=300)
            except Exception as e:
                result["error"] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise TimeoutError(f"Guardian 审校超时（{timeout}s）")
        if "error" in result:
            raise result["error"]
        return result.get("value", "")

    @staticmethod
    def _parse(raw: str) -> tuple:
        """解析审校模型输出。"""
        import json as _json
        import re
        try:
            m = re.search(r"\{[\s\S]*\}", raw)
            data = _json.loads(m.group() if m else raw)
            verdict = str(data.get("verdict", "allow")).strip().lower()
            if verdict not in ("allow", "block"):
                verdict = "allow"
            return verdict, str(data.get("reason", ""))[:300]
        except Exception:
            # 解析失败时保守放行（避免误伤），但记录原因
            return "allow", f"无法解析审校输出: {raw[:200]}"
