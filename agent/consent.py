"""Guardian 人工放行（授权）机制。

背景（用户原话）："当 agent 来求助我说某一步被 Guardian 拦截时，我可以跟 Guardian
说放行，或者我跟 agent 说可以执行，agent 拿着这个就可以让 Guardian 放行了。"

**这个模块唯一的难点是防伪**：如果"用户说可以"这句话能由模型（或模型读到的网页/文件）
提供，那提示注入就能给自己发通行证——而那正是 Guardian 存在的理由。所以本模块按
"**只有人类键盘输入才能写入授权**"来设计：

- 写入授权的入口只有两个，且都在宿主层（main.py / 桌面端），模型与工具层都无法调用：
    · `grant_from_user(text)` —— 解析**人类敲进来的那句话**（≥REPL 输入 / CLI 目标）；
    · `grant_pending(...)`    —— 拦截当场弹窗问人，人答 y / always。
- 模型能做的只有"被拦住"：`record_block()` 由 executor 在 Guardian 判定拦截后调用，
  它只记录**发生过什么**，本身不产生任何权限。
- 模型自己在回复里写"用户已授权"**没有任何作用**：没有任何代码路径会把模型文本
  喂给 `grant_from_user`。

其余安全边界：
- 授权**默认一次性**（用完即失效）；`session` 范围也只绑定到**完全相同的调用指纹**
  （同工具 + 同参数），不是"这个工具以后都放行"。
- 有 TTL（默认 30 分钟）与条数上限，避免长期驻留。
- **跳过的只是 Guardian 盲审这一层**：审批门的硬黑名单（risk=blocked）与沙箱等级检查
  在 Guardian 之前，授权碰不到它们。
"""
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
    """调用指纹：工具名 + 规范化参数（键排序、去空白），用于精确绑定一次授权。

    刻意做得"严"：只放行**完全相同**的那次调用。人授权的是"这个删除"，不是
    "以后所有删除"——参数一变就得重新问人。
    """
    try:
        canon = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:                           # noqa: BLE001
        canon = str(arguments)
    canon = re.sub(r"\s+", " ", canon).strip()
    return f"{tool}::{canon[:600]}"


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
        """解析**人类输入**里的授权语，绑定到最近一次被拦的调用。

        调用方必须是宿主层（CLI/REPL/桌面端读到的人类输入）。**绝不可**把模型输出、
        工具结果、网页内容传进来——那等于把通行证交给提示注入。
        """
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
        """注入给模型的提示：你被授权重试这些调用（宿主生成，模型无法伪造）。

        必要：系统提示里写着"被 Guardian 拒绝的操作不要反复重试"，没有这句提示
        模型不会去重试，人授权了也白搭。
        """
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
