# -*- coding: utf-8 -*-
"""动态轮数预算：由"有没有进展"决定续多少轮，而不是固定上限。"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple


@dataclass
class TurnOutcome:
    """一轮循环的产出摘要——只聚合 executor 里已有的信号，不新增埋点成本。"""

    tool_calls: int = 0
    succeeded: int = 0
    failed: int = 0
    files_changed: int = 0
    new_calls: int = 0              # 本轮首次出现的**成功**调用数（同签名此前跑过就不算）
    repeated: bool = False          # 本轮调用（名称+参数）与上一轮完全一致
    empty_response: bool = False    # 模型既没给答案也没调工具（上游偶发只回 reasoning）

    @property
    def productive(self) -> bool:
        """有没有产出（用于展示）。续期判定用 meaningful，不认"成功但没新信息"。"""
        if self.repeated:
            return False
        return self.files_changed > 0 or self.succeeded > 0

    @property
    def meaningful(self) -> bool:
        """真进展：改了文件，或出现了此前没跑过的成功调用。

        "成功"本身太宽：交替重复两个读/查操作每轮都成功，预算就会一路续到硬上限。
        同一签名只算一次——重复调用拿不到新信息。
        """
        if self.repeated:
            return False
        return self.files_changed > 0 or self.new_calls > 0

    @property
    def spinning(self) -> bool:
        """是否在原地打转（计入停滞）：原样重复调用，或空回复。
        也不计停滞，最终由预算上限兜住（理由是"任务比预期复杂"，不是"打转"）。"""
        return self.repeated or self.empty_response

    def describe(self) -> str:
        bits = []
        if self.files_changed:
            bits.append("改文件 %d" % self.files_changed)
        if self.succeeded:
            bits.append("成功 %d" % self.succeeded)
        if self.failed:
            bits.append("失败 %d" % self.failed)
        if self.meaningful:
            bits.append("有进展")
        elif self.spinning:
            bits.append("打转")
        elif self.succeeded:
            bits.append("无新信息")
        return "、".join(bits) or "无操作"


def tool_signature(tool_calls: Sequence[Any]) -> Tuple[str, ...]:
    """把一轮的调用压成可比较的签名（名称 + 参数），用于识别"原样重复"。"""
    sig = []
    for tc in tool_calls or ():
        name = getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else "")
        args = getattr(tc, "arguments", None)
        if args is None and isinstance(tc, dict):
            args = tc.get("arguments")
        try:
            rendered = repr(sorted((args or {}).items()))
        except Exception:       # noqa: BLE001 — 参数不是 dict 时退化为整体 repr
            rendered = repr(args)
        sig.append("%s(%s)" % (name, rendered))
    return tuple(sorted(sig))


@dataclass
class LoopBudget:
    """动态轮数预算（纯逻辑，不依赖 executor）。"""

    base: int = 30                  # 起步轮数
    extend: int = 10                # 每有一轮"有进展"续多少轮
    stall_limit: int = 5            # 自上次进展以来打转多少轮就判定空转
    hard_cap: int = 120             # 绝对安全网；0 = 不设上限
    stagnation_limit: int = 0       # 触发"连续无进展"预警的轮数；0 = 关闭
    limit: int = field(init=False)
    used: int = 0
    productive_turns: int = 0
    spinning_turns: int = 0         # 自上次进展以来"原地打转"的轮数（有进展即清零）
    stagnation_turns: int = 0       # 连续**无任何进展**轮数（不管是否换新做法；有进展即清零）
    extensions: int = 0
    stop_reason: str = ""           # "exhausted" / "stalled" / "interrupted" / ""（未停）
    interrupt_error: str = ""       # 中断收尾时的最后一条上游错误（仅用于说明文案）

    def __post_init__(self):
        self.base = max(1, int(self.base))
        self.extend = max(0, int(self.extend))
        self.stall_limit = max(1, int(self.stall_limit))
        self.hard_cap = max(0, int(self.hard_cap))
        self.stagnation_limit = max(0, int(self.stagnation_limit))
        self.limit = self._capped(self.base)

    def _capped(self, value: int) -> int:
        """硬上限就是硬上限：即使它小于起步轮数也以它为准。
        而 --max-ops 的语义是"就跑这么多轮"，必须被尊重。）"""
        if self.hard_cap <= 0:
            return value
        return min(value, self.hard_cap)

    @classmethod
    def fixed(cls, turns: int) -> "LoopBudget":
        """固定轮数预算（显式 --max-ops 的旧语义：不续期、不因停滞提前停）。
        调用方（CLI 参数 / 测试）依赖它可预测；动态续期只应作为默认行为。"""
        turns = max(1, int(turns))
        return cls(base=turns, extend=0, stall_limit=turns + 1, hard_cap=turns)

    @classmethod
    def from_config(cls, hard_cap_fallback: int = 120,
                    config: Optional[Dict[str, Any]] = None) -> "LoopBudget":
        """从 TOOL_CONFIG 构造。"""
        if config is None:
            from config import TOOL_CONFIG
            config = TOOL_CONFIG
        hard_cap = config.get("loop_hard_cap")
        if hard_cap is None:
            hard_cap = hard_cap_fallback
        return cls(
            base=int(config.get("loop_base_turns", 30)),
            extend=int(config.get("loop_extend_per_progress", 10)),
            stall_limit=int(config.get("loop_stall_limit", 5)),
            hard_cap=int(hard_cap),
            stagnation_limit=int(config.get("loop_stagnation_warn", 0)),
        )

    # ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return True

    def allow_next(self) -> bool:
        """能否再跑一轮；不能则设置 stop_reason。"""
        if self.spinning_turns >= self.stall_limit:
            self.stop_reason = "stalled"
            return False
        if self.hard_cap <= 0:
            return True
        if self.used >= self.limit:
            self.stop_reason = "exhausted"
            return False
        return True

    def observe(self, outcome: TurnOutcome) -> None:
        """结算一轮：有真进展就续期并清零打转/停滞计数；否则累计。"""
        self.used += 1
        if outcome.meaningful:
            self.productive_turns += 1
            self.spinning_turns = 0
            self.stagnation_turns = 0
            grew = self._capped(self.limit + self.extend)
            if grew > self.limit:
                self.limit = grew
                self.extensions += 1
        else:
            # 无新进展（重读同一处 / 打转 / 换了做法但失败）→ 累计停滞；
            # 只有原样重复与空回复才算"打转"，失败的新尝试仍给机会，只是不续期。
            self.stagnation_turns += 1
            if outcome.spinning:
                self.spinning_turns += 1

    # ------------------------------------------------------------

    def stagnation_alarm(self) -> bool:
        """连续无进展轮数是否**刚好达到**预警阈值（每阈值只响一次）。"""
        return (
            self.stagnation_limit > 0
            and self.stagnation_turns == self.stagnation_limit
        )

    # ------------------------------------------------------------

    def mark_interrupted(self, error: str = "") -> None:
        """LLM 连续失败（限流/超时）导致的收尾。"""
        self.stop_reason = "interrupted"
        self.interrupt_error = (error or "").strip()[:200]

    def near_limit(self, warn_fraction: float = 0.8) -> bool:
        """是否已接近当前预算（用于预警）。
        无限模式（hard_cap=0）下没有"接近上限"这回事，恒返回 False。"""
        if self.hard_cap <= 0 or self.limit <= 0:
            return False
        return self.used >= int(self.limit * warn_fraction)

    def summary(self) -> Dict[str, Any]:
        return {
            "used": self.used, "limit": self.limit, "base": self.base,
            "extensions": self.extensions, "productive_turns": self.productive_turns,
            "spinning_turns": self.spinning_turns, "stagnation_turns": self.stagnation_turns,
            "hard_cap": self.hard_cap,
            "stop_reason": self.stop_reason,
            "interrupt_error": self.interrupt_error,
        }

    def stop_message(self) -> str:
        """给用户/模型看的中止说明（理由比"轮数耗尽"准确，且能被交接机制识别）。"""
        if self.stop_reason == "interrupted":
            detail = f"：{self.interrupt_error}" if self.interrupt_error else ""
            return (
                "上游模型连续失败，重试耗尽后停止%s。**轮数预算并未耗尽**"
                "（起步 %d 轮 / 已用 %d 轮 / 安全网上限 %s）——这不是任务太大，"
                "是上游暂时不可用；稍后重发即可续跑，已完成的部分不会重做。"
                % (detail, self.base, self.used,
                   self.hard_cap if self.hard_cap > 0 else "无")
            )
        if self.stop_reason == "stalled":
            return (
                "自上次取得进展以来，已有 %d 轮在原地打转（原样重复调用或空回复），"
                "已停止以免空转。**这不代表任务无解**——若判断还能推进，请给出"
                "**新的**做法（换命令、换工具、缩小问题范围），再让我继续；"
                "续跑时会沿用已有成果。"
                % self.spinning_turns
            )
        return (
            "已达到任务最大操作轮数（%d 轮，其中按进展动态续期 %d 次，安全网上限 %s）。"
            "任务比预期复杂，已完成的操作都已记录：可用 --max-ops 提高上限，"
            "或把目标拆成几步后分别执行。"
            % (self.limit, self.extensions,
               self.hard_cap if self.hard_cap > 0 else "无")
        )


@dataclass
class StallWatch:
    """打转提醒：预算负责"停"，这里负责先告诉模型一句。

    模型看不到自己的重复——不提醒就只会把轮数耗完、然后被停掉。
    `observe()` 返回空串表示这轮不用提醒；阈值处各触发一次，持续打转就按周期重复提醒。
    """

    repeat_after: int = 2           # 连续几轮原样重复后提醒
    fail_after: int = 3             # 同一调用连续失败几轮后提醒
    repeat_streak: int = 0
    fail_streak: int = 0
    last_fail_sig: Tuple[str, ...] = ()

    def observe(self, repeated: bool, fail_sig: Sequence[str] = (),
                error: str = "") -> str:
        self.repeat_streak = self.repeat_streak + 1 if repeated else 0

        sig = tuple(fail_sig or ())
        if sig and sig == self.last_fail_sig:
            self.fail_streak += 1
        else:
            self.fail_streak = 1 if sig else 0
        self.last_fail_sig = sig

        if (sig and self.fail_after > 0 and self.fail_streak >= self.fail_after
                and self.fail_streak % self.fail_after == 0):
            detail = f"（最后一次错误：{str(error)[:300]}）" if error else ""
            return (
                f"同一个工具调用已连续失败 {self.fail_streak} 次{detail}。"
                "原样重试不会成功：先按错误信息修正参数，或换一种做法。"
            )
        if (self.repeat_after > 0 and self.repeat_streak
                and self.repeat_streak % self.repeat_after == 0):
            return (
                f"你已经连续 {self.repeat_streak} 轮发出了完全相同的工具调用，结果也一样。"
                "先说明当前卡在哪里，再换一种做法（换工具 / 换参数 / 先读错误信息），不要原样重试。"
            )
        return ""
