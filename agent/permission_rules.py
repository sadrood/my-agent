"""声明式权限规则：把授权粒度从"工具名"下沉到"参数模式"。

规则写法 `Tool(pattern)`，如 `terminal(git *)`、`edit(src/**)`；省略括号则匹配整个工具。
决策顺序 deny > ask > allow，同级先命中者生效；无命中时用 default（缺省 ask）。
规则集为空时调用方行为与接入前完全一致。

`Bash(...)` 与 `terminal(...)` 等价（工具在本仓库叫 terminal，Bash 是对齐 Claude Code 的别名）。

接入点：`agent.approval.ApprovalPolicy`（decide 的第 2.6 步，排在 execpolicy 之后）。
规则文件路径来自 `config.APPROVAL_CONFIG["permission_rules_file"]`（锚定项目根）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

EFFECTS = ("allow", "ask", "deny")
_EFFECT_RANK = {"deny": 0, "ask": 1, "allow": 2}

#: 工具名别名（左）归一到右：本仓库的终端工具叫 terminal，Claude Code 叫 Bash
TOOL_ALIASES = {"bash": "terminal", "shell": "terminal", "sh": "terminal",
                "write": "file", "patch": "edit"}

#: 工具名 -> 参与匹配的参数字段（按序取第一个存在的）
SUBJECT_KEYS: Dict[str, Tuple[str, ...]] = {
    "terminal": ("command", "cmd"),
    "python": ("code",),
    "file": ("path", "destination"),
    "edit": ("file_path",),
    "read": ("path",),
    "grep": ("path",),
}

_RULE_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*(?:\(\s*(.*?)\s*\))?\s*$", re.S)

#: 复合命令切分符：`;` `&&` `||` `|` 换行（引号内的不切，见 _subcommands）
_COMPOUND_SPLIT = re.compile(r"\|\||&&|[;|\n]")


def canonical_tool(name: str) -> str:
    """工具名归一：别名与大小写都收掉（`Bash` → `terminal`）。"""
    low = str(name or "").strip().lower()
    return TOOL_ALIASES.get(low, low)



@dataclass(frozen=True)
class Rule:
    """一条规则：工具名 + 可选参数模式；effect 由所在列表决定。"""

    tool: str
    pattern: Optional[str] = None
    effect: str = "ask"
    raw: str = ""

    @property
    def scope(self) -> str:
        return "tool" if self.pattern is None else "args"


@dataclass(frozen=True)
class Decision:
    """决策结果；rule 为命中的规则（None 表示落到 default）。"""

    effect: str
    rule: Optional[Rule] = None
    reason: str = ""


def parse_rule(text: str, effect: str = "ask") -> Optional[Rule]:
    """解析 `Bash(git *)` 这类写法；非法返回 None。"""
    if effect not in EFFECTS:
        raise ValueError(f"未知 effect: {effect!r}")
    if not isinstance(text, str):
        return None
    m = _RULE_RE.match(text)
    if not m:
        return None
    pattern = m.group(2)
    if pattern == "":
        pattern = None
    return Rule(tool=m.group(1), pattern=pattern, effect=effect, raw=text.strip())


def parse_rules(items: Iterable[str], effect: str) -> List[Rule]:
    """批量解析，跳过非法项（配置容错：一条写错不拖垮整体）。"""
    out: List[Rule] = []
    for it in items or ():
        r = parse_rule(it, effect)
        if r is not None:
            out.append(r)
    return out


def _segments(pattern: str) -> List[str]:
    return [s for s in re.split(r"[\\/]+", pattern.strip()) if s not in ("", ".")]


def _path_match(pattern: str, subject: str) -> bool:
    """`**` 跨目录、`*` 只在单层内生效的路径匹配。"""
    pat, sub = _segments(pattern), _segments(subject)
    if not pat:
        return False

    def walk(i: int, j: int) -> bool:
        if i == len(pat):
            return j == len(sub)
        if pat[i] == "**":
            return any(walk(i + 1, k) for k in range(j, len(sub) + 1))
        if j == len(sub):
            return False
        if not fnmatch.fnmatchcase(sub[j], pat[i]):
            return False
        return walk(i + 1, j + 1)

    return walk(0, 0)


def _command_match(pattern: str, subject: str) -> bool:
    """命令模式：`git *` 命中 `git status`，但不命中 `gitk`（前缀须按词边界）；不含通配则整串相等。"""
    pat = pattern.strip()
    if not pat:
        return False
    text = subject.strip()
    if pat.endswith("*"):
        prefix = pat[:-1].rstrip()
        if not prefix:
            return bool(text)
        low = text.lower()
        return low == prefix.lower() or low.startswith(prefix.lower() + " ")
    return fnmatch.fnmatchcase(text, pat)


def _subcommands(command: str) -> List[str]:
    """把复合命令切成子命令（`;` `&&` `||` `|` 与换行）。

    不做完整 shell 解析（引号/转义会带来更多误判），但足以拦住"以允许前缀开头、
    后面接别的命令"这类绕过 —— 见 `_match_command` 的语义。
    """
    parts = [s.strip() for s in _COMPOUND_SPLIT.split(str(command or ""))]
    return [p for p in parts if p] or [str(command or "").strip()]


def subject_of(tool: str, args) -> str:
    """从工具参数里取出参与匹配的主语（命令串 / 路径）。"""
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    if not isinstance(args, dict):
        return str(args)
    for k in SUBJECT_KEYS.get(canonical_tool(tool), ()):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v
    for v in args.values():
        if isinstance(v, str) and v.strip():
            return v
    return ""


class PermissionRules:
    """规则集：allow / ask / deny 三档 + default 兜底。"""

    def __init__(self, allow=None, ask=None, deny=None, default: str = "ask"):
        self.allow = parse_rules(allow, "allow")
        self.ask = parse_rules(ask, "ask")
        self.deny = parse_rules(deny, "deny")
        self.default = default if default in EFFECTS else "ask"

    def __len__(self) -> int:
        return len(self.allow) + len(self.ask) + len(self.deny)

    @property
    def empty(self) -> bool:
        return len(self) == 0

    def _match_command(self, rule: Rule, subject: str) -> bool:
        """命令类模式 + 复合命令语义。

        allow 要求**每一段**子命令都命中（`allow terminal(git *)` 不该把
        `git status; rm -rf /` 整串放行）；deny / ask 命中任一段即算
        （`deny terminal(rm *)` 要认出 `cd / && rm -rf /` 里的危险段）。
        """
        parts = _subcommands(subject)
        hits = [_command_match(rule.pattern or "", p) for p in parts]
        if not hits:
            return False
        return all(hits) if rule.effect == "allow" else any(hits)

    def _match(self, rule: Rule, tool: str, subject: str) -> bool:
        if canonical_tool(rule.tool) != canonical_tool(tool):
            return False
        if rule.pattern is None:
            return True
        if canonical_tool(rule.tool) == "terminal":
            return self._match_command(rule, subject)
        return _path_match(rule.pattern, subject)

    def decide(self, tool: str, args=None) -> Decision:
        """返回 Decision；无命中且规则集为空时是 default（默认 ask，即行为不变）。"""
        subject = subject_of(tool, args)
        hits: List[Rule] = []
        for bucket in (self.deny, self.ask, self.allow):
            for rule in bucket:
                if self._match(rule, tool, subject):
                    hits.append(rule)
        if not hits:
            return Decision(effect=self.default, rule=None, reason="no-rule")
        best = min(hits, key=lambda r: _EFFECT_RANK[r.effect])
        return Decision(effect=best.effect, rule=best, reason=f"rule:{best.raw}")

    @classmethod
    def from_dict(cls, data: dict) -> "PermissionRules":
        data = data or {}
        return cls(
            allow=data.get("allow"),
            ask=data.get("ask"),
            deny=data.get("deny"),
            default=data.get("default", "ask"),
        )

    @classmethod
    def from_file(cls, path) -> "PermissionRules":
        """从 JSON 读规则；文件不存在或损坏时返回空规则集（不改变既有行为）。"""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, ValueError):
            return cls()

    @classmethod
    def from_env(cls, var: str = None) -> "PermissionRules":
        """载入规则文件。

        路径优先取 `config.APPROVAL_CONFIG["permission_rules_file"]`（已锚定项目根，
        避免 cwd 漂移导致规则静默变空集），取不到再退回同名环境变量原文。
        """
        path = ""
        try:
            from config import APPROVAL_CONFIG
            path = str(APPROVAL_CONFIG.get("permission_rules_file") or "").strip()
        except Exception:
            path = ""
        if not path:
            path = (os.environ.get(var or "MY_AGENT_PERMISSION_RULES") or "").strip()
        return cls.from_file(path) if path else cls()
