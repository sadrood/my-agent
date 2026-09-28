"""execpolicy DSL：结构化命令策略（白名单模式的升级）。
保证调用位置），因此 DSL 不能豁免黑名单，也不能豁免沙箱等级不足；"""
import json
import logging
import os
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

VALID_DECISIONS = ("allow", "deny", "ask")


class ExecPolicy:
    """结构化命令策略（纯逻辑，无全局状态）。"""

    def __init__(self, rules: Optional[List[dict]] = None):
        # 只留 dict 规则：`load()` 只校验'是 list'不校验元素类型，而直接构造 `ExecPolicy([...])`（测试与嵌入式调用）也走这里。
        self.rules = [r for r in (rules or []) if isinstance(r, dict)]

    @classmethod
    def load(cls, path: str) -> "ExecPolicy":
        """从 JSON 文件加载规则。"""
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
        """对一次工具调用做策略判定。"""
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
        # 前缀匹配要与**平台的命令解析规则**保持一致（命令最终交给 shell=True 解析）：；  · Windows：cmd.exe 大小写不敏感，`CURL` 就是 `curl.exe` —— 敏感比较等于给
        if prefix is not None:
            if os.name == "nt":
                if not command.lower().startswith(str(prefix).lower()):
                    return False
            elif not command.startswith(prefix):
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