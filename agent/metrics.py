"""
运行时统计模块（借鉴同类实现的统计行）。

每次运行结束后输出一行统计：
  4 轮 · 6 步 · LLM 42.3s · 工具 0.8s · 首 token 2.1s · 21 tok/s
  · 缓存命中 63% · 输入 1.2K · 输出 312

数据来源：
- 轮数/步数/工具耗时 → Executor 主循环
- LLM 耗时/首 token/速率/token 用量 → LLM 层（每次调用后记录）
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional


def humanize_seconds(seconds: float) -> str:
    """秒数 → 人类可读（42.3s / 1m23s / 1h05m）。"""
    if seconds is None or seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    if minutes < 60:
        return f"{minutes}m{secs:.0f}s"
    hours = minutes // 60
    return f"{hours}h{minutes % 60:02d}m"


def humanize_tokens(n: int) -> str:
    """token 数 → 人类可读（312 / 1.2K / 64.5M）。"""
    if n is None or n <= 0:
        return "0"
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


@dataclass
class RunMetrics:
    """一次运行的全部统计。"""

    turns: int = 0                      # 模型调用轮数
    steps: int = 0                      # 工具调用步数（含 think）
    llm_seconds: float = 0.0            # LLM 调用总耗时
    tool_seconds: float = 0.0           # 工具执行总耗时
    first_token_seconds: List[float] = field(default_factory=list)  # 每次调用的首 token 延迟
    input_tokens: int = 0               # 输入 token 总量
    output_tokens: int = 0              # 输出 token 总量
    cached_tokens: int = 0              # 缓存命中的输入 token
    last_context_tokens: int = 0        # 最近一次请求的输入规模（≈上下文窗口水位）
    modified_files: dict = field(default_factory=dict)  # {文件路径: 修改次数}

    # ---- 累加接口 ----

    def add_llm_call(
        self,
        elapsed: float,
        first_token: Optional[float] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
    ):
        """记录一次 LLM 调用。"""
        self.llm_seconds += elapsed or 0.0
        if first_token is not None:
            self.first_token_seconds.append(max(first_token, 0.0))
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.cached_tokens += cached_tokens or 0
        # 每次调用传入的 input_tokens 即该次请求的完整 prompt 长度：
        # 保留最后一次作为「上下文窗口水位」的近似（多轮重复发送不计入窗口水位）
        if input_tokens:
            self.last_context_tokens = int(input_tokens)

    def add_file_change(self, path: str):
        """记录一次文件修改。"""
        if path:
            self.modified_files[path] = self.modified_files.get(path, 0) + 1

    # ---- 派生指标 ----

    @property
    def first_token_avg(self) -> float:
        if not self.first_token_seconds:
            return 0.0
        return sum(self.first_token_seconds) / len(self.first_token_seconds)

    @property
    def tokens_per_sec(self) -> float:
        if self.first_token_seconds and self.llm_seconds > 0:
            # 速率 = 输出 token / 流式输出时长（LLM 总耗时）
            return self.output_tokens / self.llm_seconds if self.llm_seconds > 0 else 0.0
        return 0.0

    @property
    def cache_hit_rate(self) -> float:
        if self.input_tokens <= 0:
            return 0.0
        return min(self.cached_tokens / self.input_tokens, 1.0)

    def has_data(self) -> bool:
        return self.turns > 0 or self.steps > 0 or self.llm_seconds > 0

    # ---- 渲染 ----

    def render_line(self) -> str:
        """
        渲染统计行。

        Returns:
            "4 轮 · 6 步 · LLM 42.3s · 工具 0.8s · 首 token 2.1s · 21 tok/s
             · 缓存命中 63% · 输入 1.2K · 输出 312"
        """
        parts = [
            f"{self.turns} 轮",
            f"{self.steps} 步",
            f"LLM {humanize_seconds(self.llm_seconds)}",
            f"工具 {humanize_seconds(self.tool_seconds)}",
        ]
        if self.first_token_seconds:
            avg = self.first_token_avg
            parts.append(f"首 token 平均 {avg:.2f}s" if avg < 1 else f"首 token 平均 {avg:.1f}s")
        if self.output_tokens > 0:
            rate = self.tokens_per_sec
            parts.append(f"{rate:.1f} tok/s" if rate < 10 else f"{rate:.0f} tok/s")
        if self.input_tokens > 0:
            parts.append(f"缓存命中 {self.cache_hit_rate:.0%}")
        parts.append(f"输入 {humanize_tokens(self.input_tokens)}")
        parts.append(f"输出 {humanize_tokens(self.output_tokens)}")
        if self.modified_files:
            files = " · ".join(
                f"{p} ×{n}" if n > 1 else p
                for p, n in sorted(self.modified_files.items())
            )
            parts.append(f"修改 {files}")
        return " · ".join(parts)

    def render_status_bar(self, policy: str = "", sandbox_mode: str = "") -> str:
        """
        渲染常驻状态栏（每轮模型调用前刷新一次）。

        Args:
            policy: 审批策略名（空则不显示）
            sandbox_mode: 沙箱等级名（空则不显示）

        Returns:
            "轮 3 · ↑12.3K ↓1.8K tok · 缓存 63% · 沙箱 workspace-write · 策略 on-failure"
        """
        parts = [f"轮 {self.turns}"]
        if self.input_tokens or self.output_tokens:
            parts.append(
                f"↑{humanize_tokens(self.input_tokens)} "
                f"↓{humanize_tokens(self.output_tokens)} tok"
            )
        if self.input_tokens > 0:
            parts.append(f"缓存 {self.cache_hit_rate:.0%}")
        if sandbox_mode:
            parts.append(f"沙箱 {sandbox_mode}")
        if policy:
            parts.append(f"策略 {policy}")
        return " · ".join(parts)
