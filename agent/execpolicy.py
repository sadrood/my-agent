"""
execpolicy DSL：结构化命令策略（白名单模式的升级）。

以 JSON 规则数组描述"哪些命令放行 / 拒绝 / 询问"，作为内建启发式策略
之上的可选增量层。定位：

- 纯逻辑、无全局状态：规则由外部注入（load 或构造），decide 无副作用，
  便于单测与嵌入式复用。
- fail-open：策略文件不存在 / JSON 损坏时规则置空并 logging.warning，
  回到内建策略。DSL 是可选项，不因配置损坏而放大权限。
- 安全边界：DSL 评估永远在黑名单与沙箱等级检查之后（由 ApprovalPolicy
  保证调用位置），因此 DSL 不能豁免黑名单，也不能豁免沙箱等级不足；
  allow 只影响"是否需要询问"。

规则格式（JSON 数组）:
    [
      {
        "match": {
          "tool": "terminal",              # 精确相等；省略 = 任意工具
          "command_prefix": "npm ",        # 命令文本前缀；省略 = 任意
          "pattern": "\\binstall\\b"       # 正则 re.search + IGNORECASE；省略 = 任意
        },
        "decision": "allow | deny | ask"
      }
    ]

判定顺序：
1. 先扫描全部规则，命中任一 deny 即返回 "deny"（deny 优先，防止 allow
   规则在前屏蔽 deny）。
2. 无 deny 命中时，按规则顺序 first-match 取 allow / ask。
3. 全部不命中返回 None。
"""
import json
import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

VALID_DECISIONS = ("allow", "deny", "ask")


class ExecPolicy:
    """结构化命令策略（纯逻辑，无全局状态）。"""

    def __init__(self, rules: Optional[List[dict]] = None):
        self.rules = list(rules) if rules else []

    @classmethod
    def load(cls, path: str) -> "ExecPolicy":
        """
        从 JSON 文件加载规则。

        文件不存在 / JSON 损坏 / 不是规则数组 → rules 置空并 logging.warning
        （fail-open：回到内建策略，DSL 是可选增量层，不因配置损坏而放大权限）。
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("execpolicy 文件应为规则数组")
            return cls(data)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.warning("execpolicy 加载失败（%s），已置空规则 fail-open: %s", path, e)
            return cls([])

    def decide(self, tool_name: str, command: str) -> Optional[str]:
        """
        对一次工具调用做策略判定。

        Returns:
            "deny" / "allow" / "ask"，无命中返回 None。
        """
        # 第一遍：deny 优先（防止 allow 规则在前屏蔽 deny）
        for rule in self.rules:
            if rule.get("decision") == "deny" and self._match(rule, tool_name, command):
                return "deny"
        # 第二遍：按规则顺序 first-match 取 allow / ask
        for rule in self.rules:
            if self._match(rule, tool_name, command):
                decision = rule.get("decision")
                if decision in ("allow", "ask"):
                    return decision
        return None

    def _match(self, rule: dict, tool_name: str, command: str) -> bool:
        """单条规则的匹配语义：tool 精确相等、command_prefix 前缀、pattern 正则。"""
        match = rule.get("match") or {}
        if not isinstance(match, dict):
            return False
        # tool 精确相等（省略 = 任意匹配）
        tool = match.get("tool")
        if tool is not None and tool != tool_name:
            return False
        # command_prefix 前缀匹配（对命令文本；省略 = 任意匹配）
        prefix = match.get("command_prefix")
        if prefix is not None and not command.startswith(prefix):
            return False
        # pattern 正则 re.search + IGNORECASE（省略 = 任意匹配）
        pattern = match.get("pattern")
        if pattern is not None:
            try:
                if not re.search(pattern, command, re.IGNORECASE):
                    return False
            except re.error:
                logger.warning("execpolicy 规则含非法正则，跳过: %r", pattern)
                return False
        return True