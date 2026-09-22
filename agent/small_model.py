"""小快模型（杂活专用）—— 学 Claude Code：后台跑腿的活不占用主模型。

为什么需要：主模型负责"想与做"（推理、工具编排、写正文），但有一类活是纯跑腿：
生成会话标题、写交接摘要、压对话历史……它们
  · 不需要主模型的智商；
  · 交给主模型既慢又贵（一次几百 token 的提示 + 一次完整推理）；
  · 还会挤占主模型的上下文预算（标题这种活本来不该进主对话）。
Claude Code 的做法就是后台挂一个小快模型干这些活，这里照做。

设计底线：
- **fail-open**：小模型超时/报错/输出没法用 → 返回空串，调用方退回原来的规则实现
  （例如会话标题退回"第一条用户消息截断"）。小模型坏了绝不能挡住主流程。
- **不污染主上下文**：杂活的提示与回复只在小模型这边走，不进主对话 messages。
- **可关**：SMALL_MODEL_ENABLED=false 就完全停用（全退回规则实现）。
- 只用它做**无副作用**的杂活：它永远不调用工具、不执行任何东西。
"""
import re
import threading
from typing import List, Optional

from config import SMALL_MODEL_CONFIG

TITLE_SYSTEM_PROMPT = (
    "你负责给一段对话起标题。要求：\n"
    "- 用**中文**，不超过 12 个字，像目录条目，不要句子\n"
    "- 抓住具体在做的事（对象/动作），不要写「关于…的讨论」「用户询问」这类空话\n"
    "- 不要标点、引号、书名号、emoji，不要编号\n"
    "只输出标题本身。"
)


class SmallModel:
    """杂活专用的小快模型（只做无副作用的文本活）。"""

    def __init__(self, llm=None, config: dict = None):
        self.config = dict(SMALL_MODEL_CONFIG if config is None else config)
        self.llm = llm
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True)) and self.llm is not None

    def chore_enabled(self, chore: str) -> bool:
        chores = str(self.config.get("chores", "") or "")
        return chore in [c.strip() for c in chores.split(",") if c.strip()]

    # ------------------------------------------------------------

    def chat(self, messages: List[dict], max_tokens: int = None,
             temperature: float = 0.3) -> str:
        """调一次小模型；任何异常/超时都返回空串（fail-open）。"""
        if not self.enabled:
            return ""
        self.calls += 1
        timeout = float(self.config.get("timeout", 10))
        result: dict = {}

        def _run():
            try:
                result["value"] = self.llm.chat(
                    messages,
                    model=self.config.get("model") or None,
                    temperature=temperature,
                    max_tokens=int(max_tokens or self.config.get("max_tokens", 200)),
                )
            except Exception as e:              # noqa: BLE001
                result["error"] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            self.failures += 1
            self.last_error = f"小模型超时（{timeout:.0f}s）"
            return ""
        if "error" in result:
            self.failures += 1
            self.last_error = str(result["error"])[:200]
            return ""
        return (result.get("value") or "").strip()

    # ------------------------------------------------------------
    # 杂活 1：会话标题
    # ------------------------------------------------------------

    def title_for(self, messages: List[dict]) -> str:
        """给一段对话起个短标题；不可用/失败时返回空串（调用方用规则兜底）。"""
        if not self.enabled or not self.chore_enabled("titles"):
            return ""
        head = self._title_material(messages)
        if not head:
            return ""
        raw = self.chat([
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {"role": "user", "content": head},
        ], max_tokens=40, temperature=0.2)
        return self.sanitize_title(raw)

    def sanitize_title(self, raw: str) -> str:
        """把模型输出收拾成能当文件/列表用的标题。

        模型很爱加书名号、引号、句号、前缀（"标题："）或换行——都清掉；
        超长按字符截断（宁可短，不要糊在列表里）。
        """
        text = (raw or "").strip()
        if not text:
            return ""
        text = text.splitlines()[0].strip()                 # 只取第一行
        text = re.sub(r"^(标题|题目|topic|title)\s*[:：]\s*", "", text, flags=re.I)
        text = text.strip("《》\"'“”‘’`。.，,、!！?？:：;；-—…· \t")
        text = re.sub(r"\s+", " ", text).strip()
        limit = int(self.config.get("title_max_chars", 18) or 18)
        if len(text) > limit:
            text = text[:limit].rstrip()
        # 收拾完变成空（例如模型只输出了标点）→ 当失败，让调用方兜底
        return text if len(text) >= 2 else ""

    @staticmethod
    def _title_material(messages: List[dict]) -> str:
        """挑给标题模型看的素材：第一条用户消息 + 第一次回复的开头。

        只喂开头，不喂全量对话——标题要抓的是"这件事是什么"，
        而且小模型的上下文预算本来就该省着用。
        """
        user = next((str(m.get("content") or "").strip()
                     for m in (messages or []) if m.get("role") == "user"
                     and str(m.get("content") or "").strip()), "")
        assistant = next((str(m.get("content") or "").strip()
                          for m in (messages or []) if m.get("role") == "assistant"
                          and str(m.get("content") or "").strip()), "")
        if not user:
            return ""
        parts = [f"用户：{user[:400]}"]
        if assistant:
            parts.append(f"助手：{assistant[:400]}")
        return "\n".join(parts)


def build_small_llm(main_llm=None):
    """构造小快模型用的 LLM（端点/模型可独立配；缺配置时退回主 LLM）。"""
    model = str(SMALL_MODEL_CONFIG.get("model") or "").strip()
    key = str(SMALL_MODEL_CONFIG.get("api_key") or "").strip()
    base = str(SMALL_MODEL_CONFIG.get("base_url") or "").strip()
    if not model:
        return main_llm
    try:
        from models.llm import LLM
        if key and base:
            return LLM(api_key=key, base_url=base, model=model)
        if main_llm is not None and hasattr(main_llm, "client"):
            # 同一个端点换个轻量模型即可
            return LLM(api_key=getattr(main_llm, "api_key", None),
                       base_url=str(getattr(main_llm.client, "base_url", "") or "") or None,
                       model=model)
        return LLM(model=model)
    except Exception:                           # noqa: BLE001
        return main_llm
