"""Guardian 人工放行（授权）机制。"""
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 人类表达"放行"的措辞（命中任一即可，但需先过否定词检查）
_ALLOW_PHRASES = (
    "放行", "允许", "授权", "批准", "同意执行", "可以执行", "可以继续", "继续执行",
    "没问题", "可以做", "去做吧", "执行吧", "干吧", "ok to run", "allow it", "go ahead",
)
#: 否定词：出现即视为"不授权"（"我不允许你删" 绝不能被当成放行）
_DENY_PHRASES = (
    "不允许", "不能", "不要", "别", "禁止", "拒绝", "不行", "不可以", "不准", "no",
    "don't", "do not",
)
#: 命中这些词的 pending 块优先绑定（用于人话里点名了具体操作时）
_STOPWORDS = {"的", "了", "吧", "就", "是", "我", "你", "它", "那个", "这个", "执行", "操作"}


def call_signature(tool: str, arguments: Any) -> str:
    """调用指纹：工具名 + 规范化参数（键排序、去空白），用于精确绑定一次授权。"""
    try:
        canon = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:                           # noqa: BLE001
        canon = str(arguments)
    canon = re.sub(r"\s+", " ", canon).strip()
    # 指纹必须绑定**全量**参数：旧实现取 `canon[:600]`，而 terminal 的 JSON 前缀 `{"command": "` 就占 14 字符 —— 约 587 字符之后的内容完全不参与绑定。
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    return f"{tool}::{canon[:200]}::{digest}"


def _keywords(text: str) -> List[str]:
    """从人话里抽出可用于匹配 pending 命令的词（中文按 2 字滑窗 + 英文单词）。"""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_\-.]{1,}|[\u4e00-\u9fff]{2,}", text or "")
    out = []
    for w in words:
        if w in _STOPWORDS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", w):
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.append(w)
    return [w for w in dict.fromkeys(out) if len(w) >= 2]


@dataclass
class PendingBlock:
    """被 Guardian 拦下的一次调用（宿主记录，模型无法伪造）。"""
    tool: str
    arguments: dict
    reason: str
    command: str = ""
    signature: str = ""
    at: float = field(default_factory=time.time)

    def describe(self) -> str:
        body = self.command or json.dumps(self.arguments, ensure_ascii=False, default=str)
        return f"{self.tool}: {str(body)[:200]}"


@dataclass
class Grant:
    """一次人工授权。"""
    signature: str
    scope: str            # once / session
    tool: str = ""
    note: str = ""        # 授权来源说明（谁、就什么授权）
    created: float = field(default_factory=time.time)
    used: int = 0

    def matches(self, tool: str, arguments: Any) -> bool:
        return self.signature == call_signature(tool, arguments)


class ConsentStore:
    """被拦记录 + 人工授权（会话级，内存态；进程退出即失效）。"""

    def __init__(self, ttl: float = 1800.0, max_pending: int = 5,
                 max_grants: int = 20, clock=time.time, enabled: bool = True):
        self.ttl = float(ttl)
        self.max_pending = int(max_pending)
        self.max_grants = int(max_grants)
        self.enabled = bool(enabled)
        self._clock = clock
        self._blocks: List[PendingBlock] = []
        self._grants: List[Grant] = []

    # ---------------- 记录被拦（executor 调用；不产生任何权限） ----------------

    def record_block(self, tool: str, arguments: Any, reason: str,
                     command: str = "") -> PendingBlock:
        block = PendingBlock(tool=tool, arguments=dict(arguments or {}),
                             reason=reason, command=command,
                             signature=call_signature(tool, arguments),
                             at=self._clock())      # 用 store 的时钟，TTL 才可测可控
        self._blocks.append(block)
        if len(self._blocks) > self.max_pending:
            del self._blocks[:-self.max_pending]
        return block

    def pending(self) -> List[PendingBlock]:
        self._expire()
        return list(self._blocks)

    # ---------------- 授权（只有人类输入路径会调用） ----------------

    def grant_from_user(self, text: str) -> Optional[Grant]:
        """解析**人类输入**里的授权语，绑定到最近一次被拦的调用。"""
        if not self.enabled or not text:
            return None
        low = text.lower()
        if any(p in low for p in _DENY_PHRASES):
            return None
        if not any(p in low for p in _ALLOW_PHRASES):
            return None
        blocks = self.pending()
        if not blocks:
            return None                      # 没有待放行的拦截，授权语无从绑定
        target = self._best_match(text, blocks)
        scope = "session" if any(w in low for w in ("以后", "都", "一直", "永久", "always")) else "once"
        note = f"用户口头授权（{'本次' if scope == 'once' else '本会话内同一条调用'}）：{text.strip()[:120]}"
        return self._add_grant(target, scope, note)

    def grant_pending(self, index: int = -1, scope: str = "once",
                      note: str = "拦截时人工确认") -> Optional[Grant]:
        """就把某次被拦的调用授权（拦截当场弹窗、人答 y/always 时调用）。"""
        blocks = self.pending()
        if not blocks:
            return None
        try:
            target = blocks[index]
        except IndexError:
            return None
        return self._add_grant(target, scope, note)

    def _add_grant(self, block: PendingBlock, scope: str, note: str) -> Grant:
        if scope not in ("once", "session"):
            scope = "once"
        grant = Grant(signature=block.signature, scope=scope, tool=block.tool, note=note,
                      created=self._clock())
        self._grants.append(grant)
        if len(self._grants) > self.max_grants:
            del self._grants[:-self.max_grants]
        # 已授权的拦截从待办里移除，避免同一条被反复"授权"
        self._blocks = [b for b in self._blocks if b.signature != block.signature]
        return grant

    def _best_match(self, text: str, blocks: List[PendingBlock]) -> PendingBlock:
        """人话里点名了具体操作就绑定那一条，否则绑定最近一条。"""
        kws = _keywords(text)
        if kws:
            scored = []
            for b in blocks:
                hay = (b.describe() + " " + b.reason).lower()
                scored.append((sum(1 for k in kws if k.lower() in hay), b))
            top = max(s for s, _ in scored)
            if top > 0:
                winners = [b for s, b in scored if s == top]
                if len(winners) == 1:
                    return winners[0]
        return blocks[-1]

    # ---------------- 判定 ----------------

    def allows(self, tool: str, arguments: Any) -> bool:
        """这次调用是否已被人工授权（命中即消费掉一次性授权）。"""
        self._expire()
        for i, g in enumerate(self._grants):
            if g.matches(tool, arguments):
                g.used += 1
                if g.scope == "once":
                    del self._grants[i]
                return True
        return False

    def grant_for(self, tool: str, arguments: Any) -> Optional[Grant]:
        """查看（不消费）某次调用是否已被授权。"""
        self._expire()
        for g in self._grants:
            if g.matches(tool, arguments):
                return g
        return None

    def _expire(self) -> None:
        now = self._clock()
        self._blocks = [b for b in self._blocks if now - b.at <= self.ttl]
        self._grants = [g for g in self._grants if now - g.created <= self.ttl]

    # ---------------- 给模型/人看的文本 ----------------

    def hint_for_agent(self) -> str:
        """注入给模型的提示：你被授权重试这些调用（宿主生成，模型无法伪造）。"""
        grants = [g for g in self._grants if g.scope == "session" or g.used == 0]
        if not grants:
            return ""
        lines = [f"- {g.tool}（{g.signature.split('::', 1)[-1][:160]}）" for g in grants[:3]]
        return (
            "【宿主提示】用户已明确授权你重试以下被 Guardian 拦截的调用，"
            "请**直接重试**（本条授权只对完全相同的那次调用有效，参数一变即失效）：\n"
            + "\n".join(lines)
        )

    def summary(self) -> str:
        self._expire()
        if not self._blocks and not self._grants:
            return "无被拦记录，无人工授权。"
        lines = []
        for b in self._blocks:
            lines.append(f"  [待放行] {b.describe()}")
        for g in self._grants:
            scope = "本次" if g.scope == "once" else "本会话"
            lines.append(f"  [已授权·{scope}] {g.tool}: {g.note}")
        return "\n".join(lines)
