"""
Rollout 事件追踪模块（借鉴同类实现的 rollout-trace 设计）。

同类实现的 rollout 是完整的事件流：每次模型调用、工具调用、工具结果、
审批决策等都被记录，用于：
1. 调试回放 —— 一个 JSONL 文件还原整个执行过程
2. 对话压缩（compaction）—— 上下文超过阈值时，把旧事件总结成摘要，
   保留最近的消息，避免上下文爆炸

本项目实现：
- Rollout: 事件流 + JSONL 落盘（ROLLOUT_CONFIG["dir"]）
- compact_messages(): 当消息列表估计 token 数超过阈值时，
  调用 LLM 把较早的消息压缩为一条摘要（保留最近 N 条完整消息）
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config import ROLLOUT_CONFIG

COMPACT_SYSTEM_PROMPT = """你是一个对话压缩器。请把下面的执行过程压缩成一份精炼的摘要。

要求：
1. 保留：目标、已完成的工具操作及关键结果、出现的错误、当前进展状态
2. 省略：重复内容、冗长的输出细节、无信息量的过程
3. 输出一段连贯的中文摘要（不超过 1500 字），供后续决策使用。"""

COMPACT_USER_TEMPLATE = """目标: {goal}

执行过程（按时间顺序）:
{transcript}

请压缩为摘要。"""


def estimate_tokens(text: str) -> int:
    """粗略估计 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


@dataclass
class RolloutEvent:
    """一条事件记录。"""
    ts: float
    event_type: str
    data: Dict[str, Any]

    def to_line(self) -> str:
        return json.dumps({
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).isoformat(),
            "event": self.event_type,
            "data": self.data,
        }, ensure_ascii=False)


class Rollout:
    """
    事件追踪器。用法:

        rollout = Rollout("task-id")
        rollout.emit("tool_call", {"tool": "terminal", "args": {...}})
        rollout.emit("tool_result", {"success": True, "output": "..."})
    """

    def __init__(
        self,
        run_id: str = "",
        enabled: bool = None,
        config: dict = None,
        summarizer: Optional[Callable[[str], str]] = None,
    ):
        """
        Args:
            run_id: 本次运行的标识（默认自动生成）
            enabled: 是否启用（默认取 ROLLOUT_CONFIG）
            config: 配置覆盖
            summarizer: 压缩摘要回调（通常传 LLM.chat 的封装），为 None 时不做压缩
        """
        self.config = config or ROLLOUT_CONFIG
        self.enabled = enabled if enabled is not None else self.config.get("enabled", True)
        self.run_id = run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 100000}"
        self.events: List[RolloutEvent] = []
        self.summarizer = summarizer
        self._log_file = None
        self._log_path = ""

        if self.enabled:
            self._open_log()

    # ================================================================
    # 事件记录
    # ================================================================

    def emit(self, event_type: str, data: Dict[str, Any] = None):
        """记录一条事件。"""
        if not self.enabled:
            return
        event = RolloutEvent(ts=time.time(), event_type=event_type, data=data or {})
        self.events.append(event)
        if self._log_file:
            try:
                self._log_file.write(event.to_line() + "\n")
                self._log_file.flush()
            except Exception:
                pass

    def close(self):
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _open_log(self):
        try:
            os.makedirs(self.config.get("dir", "./rollouts"), exist_ok=True)
            self._log_path = os.path.join(
                self.config.get("dir", "./rollouts"), f"{self.run_id}.jsonl"
            )
            self._log_file = open(self._log_path, "a", encoding="utf-8")
        except Exception:
            self._log_file = None
            self._log_path = ""

    @property
    def log_path(self) -> str:
        return self._log_path

    def get_transcript(self, max_events: int = 200) -> str:
        """把事件流渲染为可读文本（用于压缩 / 调试）。"""
        lines = []
        for e in self.events[-max_events:]:
            lines.append(f"[{e.event_type}] {json.dumps(e.data, ensure_ascii=False)[:500]}")
        return "\n".join(lines)

    # ================================================================
    # 对话压缩（compaction）
    # ================================================================

    @staticmethod
    def messages_token_estimate(messages: List[dict]) -> int:
        """估计消息列表的总 token 数。"""
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):  # 多模态内容
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += estimate_tokens(part.get("text", ""))
            elif isinstance(content, str):
                total += estimate_tokens(content)
            total += 4  # 每条消息的协议开销
        return total

    def maybe_compact(
        self,
        messages: List[dict],
        goal: str,
        max_tokens: int = None,
        keep_recent: int = None,
    ) -> List[dict]:
        """
        当消息列表超过 token 阈值时，压缩较早的消息。

        Args:
            messages: 当前消息列表（会被复制，不会原地修改）
            goal: 当前目标（写入摘要）
            max_tokens: 触发阈值（默认取配置 compact_tokens）
            keep_recent: 保留最近 N 条完整消息（默认取配置）

        Returns:
            压缩后的消息列表；未触发压缩时返回原列表（副本）。
        """
        # 阈值单一来源：调用方（executor）传入窗口比例制阈值；
        # 未传时仅当配置显式给出 compact_tokens > 0 才触发——不再有 24K 死默认
        threshold = max_tokens or 0
        if threshold <= 0:
            threshold = int(self.config.get("compact_tokens") or 0)
        if threshold <= 0:
            return list(messages)
        keep = keep_recent or int(self.config.get("keep_recent_messages", 8))

        if self.messages_token_estimate(messages) <= threshold:
            return list(messages)
        if self.summarizer is None:
            return list(messages)
        if len(messages) <= keep + 4:
            return list(messages)

        old_part = messages[:-keep]
        transcript_lines = []
        for m in old_part:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if str(content).strip():
                transcript_lines.append(f"{role}: {str(content)[:800]}")
        transcript = "\n".join(transcript_lines)

        try:
            summary = self.summarizer([
                {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": COMPACT_USER_TEMPLATE.format(
                    goal=goal, transcript=transcript
                )},
            ])
            if not summary:
                return list(messages)
        except Exception:
            return list(messages)

        compacted = [{"role": "system", "content": f"## 之前的执行摘要\n{summary.strip()[:3000]}"}]
        # 截断点必须在完整工具闭环之后：从尾部向前扫描，确保保留部分里
        # 不存在"孤立的 tool 消息"（其 tool_calls 被截断线切走）。
        # 若保留区开头是 tool 消息，则把截断线前移，连同它的 assistant
        # tool_calls 一起保留（或整组裁掉），保证协议对仗。
        keep_msgs = list(messages[-keep:])
        while keep_msgs and keep_msgs[0].get("role") == "tool":
            # 保留区以 tool 消息开头 → 向前补它的 assistant(tool_calls)
            start = len(messages) - len(keep_msgs) - 1
            if start < 0:
                break
            prev = messages[start]
            if prev.get("role") == "assistant" and prev.get("tool_calls"):
                keep_msgs.insert(0, prev)
                break
            # 前面不是配对消息：直接裁掉这条孤立 tool 消息
            keep_msgs.pop(0)
        compacted.extend(keep_msgs)
        self.emit("compaction", {
            "before_events": len(old_part),
            "after_messages": len(compacted),
            "summary": summary.strip()[:500],
        })
        return compacted

    def cleanup_old_logs(self):
        """清理旧追踪文件，仅保留最近 N 个。"""
        try:
            d = self.config.get("dir", "./rollouts")
            files = sorted(
                (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")),
                key=os.path.getmtime,
            )
            max_files = int(self.config.get("max_files", 20))
            for f in files[:-max_files]:
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass
