"""上下文压缩：token 估算、阈值、指针折叠与 LLM 摘要。

从 Executor 抽出的内聚块；Executor 保留同名薄委托，既有调用点与测试不受影响。
"""
import re

from config import COMPACT_CONFIG
from agent.context_compaction import leading_system_messages, repair_tool_boundary

POINTER_SECTION = "## 之前的执行摘要（零驻留指针）\n"
SUMMARY_SECTION = "## 之前的执行摘要\n"


def one_line(text: str, limit: int = 200) -> str:
    """把一段文本压成单行（指针里只留能认出"这是什么"的那点信息）。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


class Compactor:
    """把长对话压成摘要或确定性指针清单。

    cfg / 各步骤回调可由宿主注入：Executor 用它把模块级配置与实例上的
    同名方法（含测试替换）原样接进来。
    """

    def __init__(self, llm=None, emit=None):
        self.llm = llm
        self.emit = emit or (lambda *args, **kwargs: None)
        self._ctx_ns = ""        # 指针命名空间（懒建，按运行区分）
        self._ctx_seq = 0        # 句柄序号

    # ------------------------------------------------------------

    @staticmethod
    def estimate_tokens(messages: list) -> int:
        """估算消息列表 token 数（与 rollout 共用同一实现，避免两套口径漂移）。"""
        from agent.rollout import Rollout
        return Rollout.messages_token_estimate(messages)

    def compact_threshold(self, cfg: dict = None) -> int:
        """压缩触发阈值：显式 token_threshold >0 优先（含测试注入）；
        否则委托 LLM（按网关窗口 × 比例）；窗口不可知兜底 240K。"""
        cfg = COMPACT_CONFIG if cfg is None else cfg
        try:
            explicit = int(cfg.get("token_threshold") or 0)
            if explicit > 0:
                return explicit
            if self.llm is not None and hasattr(self.llm, "compact_threshold_tokens"):
                return self.llm.compact_threshold_tokens()
        except Exception:                        # noqa: BLE001
            pass
        return 240_000

    @staticmethod
    def compact_mode() -> str:
        try:
            from config import CONTEXT_STORE_CONFIG
            mode = str(CONTEXT_STORE_CONFIG.get("compact_mode") or "pointer").strip().lower()
        except Exception:                        # noqa: BLE001
            mode = "pointer"
        return mode if mode in ("pointer", "summary") else "pointer"

    def maybe_compact(self, messages: list, cfg: dict = None, *,
                      estimate=None, threshold=None, mode=None,
                      fold=None, summarize=None) -> list:
        """token 超阈值时把旧历史换成摘要/指针；失败或未超阈值则原样返回。

        回调默认走本类方法；宿主注入后可沿用宿主实例上的替换（测试常用）。
        """
        cfg = COMPACT_CONFIG if cfg is None else cfg
        estimate = estimate or self.estimate_tokens
        threshold = threshold or (lambda: self.compact_threshold(cfg))
        mode = mode or self.compact_mode
        fold = fold or (lambda old: self.fold_old_messages(old, cfg))
        summarize = summarize or self.summarize_old

        keep = int(cfg.get("keep_last", 20))
        # 开头的 system 消息（含项目指令注入）必须原样保留，不能被压进摘要
        head_msgs = leading_system_messages(messages)
        body_start = len(head_msgs)
        if len(messages) - body_start <= keep + 2 or estimate(messages) < threshold():
            return messages
        # 保留区取尾 keep 条并修掉孤立的 tool 消息（切在工具闭环中间会让下一轮请求 400）
        recent = repair_tool_boundary(messages, keep)
        old = messages[body_start:len(messages) - len(recent)] if recent else []
        if not recent:
            # 全被判成孤立 tool（极端情况）：宁可这轮不压缩，也不能只剩摘要
            self.emit("compaction", {"ok": False,
                                     "error": "保留区全为孤立 tool 消息，已跳过压缩"})
            return messages
        if not old:
            # 全 system / 没有可压的正文：同样原样返回
            self.emit("compaction", {"ok": False,
                                     "error": "没有可压缩的正文（仅剩系统提示与保留区），已跳过压缩"})
            return messages
        if mode() == "pointer":
            folded, saved = fold(old)
            if folded:
                compacted = head_msgs + [
                    {"role": "system", "content": POINTER_SECTION + folded}
                ] + recent
                self.emit("compaction", {"ok": True, "mode": "pointer", "folded": len(old),
                                         "kept": len(recent), "saved_chars": saved})
                return compacted
        try:
            summary = summarize(old)
        except Exception as e:                   # noqa: BLE001
            self.emit("compaction", {"ok": False, "error": str(e)[:200]})
            return messages
        compacted = head_msgs + [
            {"role": "system", "content": SUMMARY_SECTION + summary}
        ] + recent
        self.emit("compaction", {"ok": True, "summarized": len(old), "kept": len(recent)})
        return compacted

    # ------------------------------------------------------------
    # 零驻留：大负载只留确定性指针，原文落盘可按句柄逐字取回
    # ------------------------------------------------------------

    def ctx_namespace(self) -> str:
        """本次运行的指针命名空间（句柄前缀，便于按运行列出/清理）。"""
        if not self._ctx_ns:
            from agent import context_store as store
            self._ctx_ns = store.new_namespace()
        return self._ctx_ns

    def next_handle(self) -> str:
        from agent import context_store as store
        self._ctx_seq += 1
        return store.handle_for(self.ctx_namespace(), self._ctx_seq)

    def fold_result(self, text: str) -> str:
        """超过阈值的工具结果：落盘 + 只把指针放进消息（原文可按句柄取回）。"""
        try:
            from agent import context_store as store
            folded, _done = store.fold(self.next_handle(), text)
            return folded
        except Exception:                        # noqa: BLE001
            return text

    def fold_old_messages(self, old_messages: list, cfg: dict = None) -> tuple:
        """把旧历史折成确定性指针清单（不调模型；原文落盘，句柄可 recall）。"""
        from agent import context_store as store

        cfg = COMPACT_CONFIG if cfg is None else cfg
        lines, saved = [], 0
        for m in old_messages:
            role = str(m.get("role") or "")
            content = m.get("content")
            if role == "tool":
                text = str(content or "")
                folded, done = store.fold(self.next_handle(), text)
                if done:
                    saved += max(0, len(text) - len(folded))
                    lines.append("[工具结果] " + folded.split("\n")[0])
                elif text.strip():
                    lines.append("[工具结果] " + one_line(text, 200))
            elif role == "assistant":
                calls = m.get("tool_calls") or []
                if calls:
                    names = []
                    for call in calls[:6]:
                        fn = (call or {}).get("function") or {}
                        names.append(f"{(fn.get('name') or '?')}({one_line(fn.get('arguments') or '', 80)})")
                    lines.append("[助手→工具] " + "；".join(names))
                text = str(content or "")
                if text.strip():
                    lines.append("[助手] " + one_line(text, 240))
            elif role == "user":
                lines.append("[用户] " + one_line(str(content or ""), 300))
            elif role == "system":
                lines.append("[提示] " + one_line(str(content or ""), 200))
        joined = "\n".join(lines)
        cap = int(cfg.get("pointer_max_chars", 8000))
        return joined[:cap], saved

    def summarize_old(self, old_messages: list) -> str:
        """把旧历史交给 LLM 压缩成摘要。"""
        text = "\n".join(
            f"[{m.get('role', 'user')}] {str(m.get('content', ''))[:500]}"
            for m in old_messages
        )
        prompt = (
            "你是对话历史压缩器。把下面这段 AI 助手与用户的对话历史压缩成一段简洁摘要，"
            "务必保留：已确认的事实、已做的决定、未完成的任务、关键约束、用户偏好、"
            "相关文件路径与已执行的操作结果。输出 200-500 字。\n\n"
            f"{text}"
        )
        resp = self.llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=800, temperature=0.2,
        )
        return (resp or "").strip() or "(历史已压缩)"
