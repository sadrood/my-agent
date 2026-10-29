"""Rollout 事件追踪模块（借鉴同类实现的 rollout-trace 设计）。"""
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config import ROLLOUT_CONFIG, resolve_under_root
from agent.retention import list_by_mtime


def estimate_tokens(text: str) -> int:
    """粗略估计 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4


def clip_text(text: Any, limit: int = None) -> str:
    """截断要落盘的**模型输出**（最终回答、每轮文本）。"""
    s = "" if text is None else str(text)
    n = int(limit if limit is not None else ROLLOUT_CONFIG.get("text_limit", 4000))
    if n <= 0 or len(s) <= n:
        return s
    return s[:n] + "\n…（落盘截断：原文 %d 字，调大 ROLLOUT_TEXT_LIMIT 可见全文）" % len(s)


def clip_result(text: Any, limit: int = None) -> str:
    """截断要落盘的**工具返回**（上限由 ROLLOUT_RESULT_LIMIT 控制）。"""
    n = limit if limit is not None else ROLLOUT_CONFIG.get("result_limit", 2000)
    return clip_text(text, n)


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
    """事件追踪器。用法:"""

    def __init__(
        self,
        run_id: str = "",
        enabled: bool = None,
        config: dict = None,
        summarizer: Optional[Callable[[str], str]] = None,
    ):
        """Args:"""
        self.config = config or ROLLOUT_CONFIG
        self.enabled = enabled if enabled is not None else self.config.get("enabled", True)
        self.run_id = run_id or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 100000}"
        self.events: List[RolloutEvent] = []
        self.summarizer = summarizer
        # 内存里最多留这么多条事件；完整记录已写 JSONL，内存只留尾部（此前无上限）
        self._max_memory_events = max(0, int(self.config.get("max_memory_events", 2000)))
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
        if self._max_memory_events and len(self.events) > self._max_memory_events:
            del self.events[:len(self.events) - self._max_memory_events]
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
            os.makedirs(resolve_under_root(self.config.get("dir", "./rollouts")),
                    exist_ok=True)
            self._log_path = os.path.join(
                resolve_under_root(self.config.get("dir", "./rollouts")),
            f"{self.run_id}.jsonl"
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
        """估计消息列表的总 token 数：含每条消息的协议开销与 tool_calls 负载。
        主循环压缩与 rollout 压缩共用这一个口径，避免两套估算对同一阈值给出不同结果。"""
        total = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):  # 多模态内容
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += estimate_tokens(part.get("text", ""))
            elif content:
                total += estimate_tokens(str(content))
            tc = m.get("tool_calls")
            if tc:
                try:
                    total += estimate_tokens(json.dumps(tc, ensure_ascii=False))
                except Exception:               # noqa: BLE001 — 非可序列化时退化为整体字符串
                    total += estimate_tokens(str(tc))
            total += 4  # 每条消息的协议开销
        return total

    def maybe_compact(
        self,
        messages: List[dict],
        goal: str,
        max_tokens: int = None,
        keep_recent: int = None,
    ) -> List[dict]:
        """兼容入口：压缩实现已统一到 agent.compactor.Compactor（这里只做参数适配）。

        rollout 侧从不做指针折叠，所以固定 summary 模式；keep 与阈值仍按本类配置解析。
        """
        from agent.compactor import Compactor

        threshold = int(max_tokens or 0) or int(self.config.get("compact_tokens") or 0)
        if threshold <= 0:
            return list(messages)
        keep = keep_recent or int(self.config.get("keep_recent_messages", 8))
        comp = Compactor(summarizer=self.summarizer, emit=self.emit)
        out = comp.maybe_compact(
            messages,
            {"keep_last": keep, "token_threshold": threshold},
            mode=lambda: "summary",
        )
        # 未压缩时 Compactor 原样返回同一个列表；这条入口历来返回副本，别让调用方改动串味
        return out if out is not messages else list(messages)

    def cleanup_old_logs(self):
        """清理旧追踪文件，仅保留最近 N 个。"""
        try:
            d = resolve_under_root(self.config.get("dir", "./rollouts"))
            max_files = int(self.config.get("max_files", 20))
            entries = list_by_mtime(d, suffix=".jsonl")   # 最旧在前
            for _, f in entries[:-max_files]:
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass
