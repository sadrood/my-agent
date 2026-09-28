"""任务监管者（Supervisor）：在 agent 想收尾时，由一个**独立的模型**审"目标到底做完没有"，
一个坏掉的裁判不该把正常任务卡死；宁可漏放，不可误卡。"""
import json
import re
import threading
from dataclasses import dataclass
from typing import Optional

from config import SUPERVISOR_CONFIG

SUPERVISOR_SYSTEM_PROMPT = """你是任务的**独立监管者**。执行者认为任务已经完成、准备交付，
你要对照**原始目标**判断：真的做完了吗？

判断标准（只有满足才算 done）：
1. 目标的**每一项**都已完成——目标里写了"三章"就必须有三章，写了"并测试"就必须有测试结果；
2. 产出是**可验证的**（文件已写、命令已跑、结果已给出），不是"我打算做"或"接下来会做"；
3. 没有把"中间产物"当成交付物（例如只写了一章就说完成、只建了目录就说迁移完成）。

以下情况**必须判 continue**：
- 目标包含多个部分/章节/步骤，而交付里只覆盖了一部分；
- 明确写着"待续""下一步""稍后继续""需要你确认后再做"之类的自我中断；
- 交付里出现"要不要我继续""需要我接着做吗"这类**反问用户**——除非目标本身要求先请示。

判 continue 时，`next_instruction` 要写成**可直接执行的一条指令**：说清"继续做什么、
做完到什么程度、这一轮不要停下来问"，不要泛泛说"请继续完善"。
判 done 时 `next_instruction` 留空。

只输出 JSON：
{{"verdict": "done" 或 "continue",
 "reason": "一句话判断依据（指出还缺什么）",
 "next_instruction": "要执行者立刻去做的具体指令（done 时留空）"}}"""

SUPERVISOR_USER_TEMPLATE = """原始目标：
{goal}

执行者的交付（它的最终回答）：
{final}

本轮已发生的事实（工具使用与产物，供你核对，不是自述）：
{evidence}

当前清单状态：
{checklist}

请判断任务是否真的完成。只输出 JSON。"""


@dataclass
class SupervisorVerdict:
    """监管结论。"""
    verdict: str = "done"          # done / continue
    reason: str = ""
    next_instruction: str = ""
    used: bool = False             # 是否真的调用了模型
    error: str = ""


class Supervisor:
    """独立监管者：审完成度并给下一步指令。"""

    def __init__(self, llm=None, config: dict = None):
        self.config = dict(SUPERVISOR_CONFIG if config is None else config)
        self.llm = llm
        self.review_count = 0
        self.continue_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and self.llm is not None

    def review(self, goal: str, final: str, evidence: str = "",
               checklist: str = "") -> SupervisorVerdict:
        """审一次；任何异常都 fail-open（判 done）并带上 error 说明。"""
        if not self.enabled:
            return SupervisorVerdict(verdict="done", reason="未启用监管者", used=False)
        self.review_count += 1
        prompt = SUPERVISOR_USER_TEMPLATE.format(
            goal=(goal or "")[:2000],
            final=(final or "")[:3000],
            evidence=(evidence or "（无）")[:2000],
            checklist=(checklist or "（无清单）")[:800],
        )
        try:
            raw = self._call_with_timeout([
                {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ])
        except Exception as e:                  # noqa: BLE001
            # 读 SUPERVISOR_FAIL_OPEN：这个配置项之前**定义了却从没被读过**（guardian.py 的同名开关是生效的），于是用户设 false 想让「坏掉的裁判不放行」也毫无变化 —— 超时/报错仍然静默判完成。
            if not self.config.get("fail_open", True):
                return SupervisorVerdict(
                    verdict="continue", used=True,
                    error=f"监管者不可用（fail-closed）: {str(e)[:150]}",
                    reason="监管者异常，且配置为不放行",
                    next_instruction="监管者（独立复核模型）不可用，请自行确认目标是否真的"
                                     "全部完成，未完成的部分继续做。")
            return SupervisorVerdict(verdict="done", used=True,
                                     error=f"监管者不可用（已放行）: {str(e)[:150]}",
                                     reason="监管者异常，按完成处理")
        verdict, reason, nxt = self._parse(raw)
        if verdict == "continue":
            self.continue_count += 1
        return SupervisorVerdict(verdict=verdict, reason=reason,
                                 next_instruction=nxt, used=True)

    def _call_with_timeout(self, messages) -> str:
        """带超时的调用（与 Guardian 同款：绝不让监管者把主循环挂住）。"""
        timeout = float(self.config.get("timeout", 30))
        model = self.config.get("model") or None
        result: dict = {}

        def _run():
            try:
                result["value"] = self.llm.chat(messages, model=model,
                                                temperature=0.1, max_tokens=800)
            except Exception as e:              # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise TimeoutError(f"监管者超时（{timeout:.0f}s）")
        if "error" in result:
            raise result["error"]
        return result.get("value", "") or ""

    @staticmethod
    def _parse(raw: str) -> tuple:
        """解析监管输出；解析不出来就 fail-open（done）。"""
        text = (raw or "").strip()
        fence = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return "done", f"监管输出无法解析（已放行）: {text[:120]}", ""
        try:
            data = json.loads(m.group())
        except Exception:                       # noqa: BLE001
            return "done", f"监管输出无法解析（已放行）: {text[:120]}", ""
        verdict = str(data.get("verdict", "done")).strip().lower()
        if verdict not in ("done", "continue"):
            verdict = "done"
        reason = str(data.get("reason", ""))[:300]
        nxt = str(data.get("next_instruction", "") or "").strip()[:1200]
        if verdict == "continue" and not nxt:
            # 说 continue 却没给指令 → 无法执行，按完成放行（避免空转）
            return "done", f"{reason}（监管者未给下一步指令，已放行）", ""
        return verdict, reason, nxt


def build_supervisor_llm(main_llm=None):
    """构造监管者用的 LLM：默认**另一个厂商**的端点。
    配置留空 api_key/base_url 时回退主 LLM 端点（至少还能用），并在调用方警告。"""
    model = str(SUPERVISOR_CONFIG.get("model") or "").strip()
    if not model:
        return main_llm
    key = str(SUPERVISOR_CONFIG.get("api_key") or "").strip()
    base = str(SUPERVISOR_CONFIG.get("base_url") or "").strip()
    if not (key and base):
        return main_llm
    try:
        from models.llm import LLM
        return LLM(api_key=key, base_url=base, model=model)
    except Exception:                           # noqa: BLE001
        return main_llm
