"""事件化 Hook 协议：hooks.json 注册外部命令，stdin 收 JSON、exit code / stdout JSON 出决策。

事件名与字段对齐 Claude Code 的 hooks 协议；外部进程异常一律 fail-open（只记警告，不阻断主流程）。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EVENT_PRE_TOOL = "PreToolUse"
EVENT_POST_TOOL = "PostToolUse"
EVENT_STOP = "Stop"
EVENT_SUBAGENT_STOP = "SubagentStop"
EVENT_SESSION_START = "SessionStart"
EVENT_USER_PROMPT = "UserPromptSubmit"

#: 事件别名（大小写/下划线不敏感）
_EVENT_ALIASES = {
    "pretooluse": EVENT_PRE_TOOL, "pre_tool_use": EVENT_PRE_TOOL,
    "posttooluse": EVENT_POST_TOOL, "post_tool_use": EVENT_POST_TOOL,
    "stop": EVENT_STOP,
    "subagentstop": EVENT_SUBAGENT_STOP, "subagent_stop": EVENT_SUBAGENT_STOP,
    "sessionstart": EVENT_SESSION_START, "session_start": EVENT_SESSION_START,
    "userpromptsubmit": EVENT_USER_PROMPT, "user_prompt": EVENT_USER_PROMPT,
    "user_prompt_submit": EVENT_USER_PROMPT,
}


def normalize_event(name: str) -> str:
    """事件名归一化；未知名字按原样返回（便于自定义事件）。"""
    key = str(name or "").strip().lower().replace("-", "_")
    return _EVENT_ALIASES.get(key, str(name or "").strip())


def matches_tool(matcher: str, tool_name: str) -> bool:
    """matcher 匹配工具名：空/`*` 全匹配，`|` 分隔多选，支持 `*` 通配。"""
    pattern = str(matcher or "").strip()
    if not pattern or pattern == "*":
        return True
    name = str(tool_name or "").strip().lower()
    for part in pattern.split("|"):
        part = part.strip().lower()
        if part in ("", "*") or part == name or fnmatch.fnmatch(name, part):
            return True
    return False


@dataclass(frozen=True)
class HookCommand:
    """一条外部命令 hook：事件 + 命令 + 可选 matcher/超时。"""

    event: str
    command: str
    matcher: str = ""
    timeout: float = 10.0


@dataclass(frozen=True)
class HookDecision:
    """一次事件派发的合并结果；全默认值表示没有 hook 干预。"""

    blocked: bool = False
    reason: str = ""
    permission: Optional[str] = None
    messages: Tuple[str, ...] = ()
    updated_input: Optional[Dict] = None
    ran: int = 0

    @property
    def acts(self) -> bool:
        """是否产生了任何干预（阻断/建议/表态）。"""
        return self.blocked or bool(self.messages) or self.permission is not None

    def merge(self, other: "HookDecision") -> "HookDecision":
        """合并决策：阻断取或、消息拼接、先表态者胜。"""
        if other is None:
            return self
        return HookDecision(
            blocked=self.blocked or other.blocked,
            reason=self.reason or other.reason,
            permission=self.permission or other.permission,
            messages=tuple(self.messages) + tuple(other.messages),
            updated_input=self.updated_input or other.updated_input,
            ran=self.ran + other.ran,
        )


def _command_of(item, default_timeout: float) -> Tuple[str, float]:
    """从注册项取出命令与超时；非 command 型（如 prompt 型）返回空命令。"""
    if isinstance(item, str):
        return item.strip(), default_timeout
    if not isinstance(item, dict):
        return "", default_timeout
    kind = str(item.get("type") or "command").strip().lower()
    if kind != "command":
        logger.debug("跳过非 command 型 hook: %s", kind)
        return "", default_timeout
    command = str(item.get("command") or "").strip()
    timeout = default_timeout
    try:
        if item.get("timeout") is not None:
            timeout = max(0.1, float(item["timeout"]))
    except (TypeError, ValueError):
        timeout = default_timeout
    return command, timeout


def parse_hooks(raw, default_timeout: float = 10.0) -> List[HookCommand]:
    """解析 hooks.json：兼容 {"hooks": {...}} 包裹写法与直接事件表写法。"""
    events = raw.get("hooks") if isinstance(raw, dict) and isinstance(raw.get("hooks"), dict) else raw
    if not isinstance(events, dict):
        raise ValueError("hooks 文件根节点必须是对象")
    out: List[HookCommand] = []
    for event_name, entries in events.items():
        event = normalize_event(str(event_name))
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            matcher = ""
            items = [entry]
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                matcher = str(entry.get("matcher") or "").strip()
                items = entry["hooks"]
            for item in items:
                command, timeout = _command_of(item, default_timeout)
                if command:
                    out.append(HookCommand(event=event, command=command,
                                           matcher=matcher, timeout=timeout))
    return out


class EventHookRunner:
    """按事件派发外部命令 hook；加载、执行、解析的异常一律 fail-open。"""

    def __init__(self, events_file: Optional[str] = None, timeout: float = 10.0,
                 enabled: bool = True, cwd: Optional[str] = None):
        self.events_file = events_file
        self.default_timeout = float(timeout or 10.0)
        self.enabled = bool(enabled)
        self.cwd = cwd or os.getcwd()
        self.hooks: List[HookCommand] = []
        self.load_error: Optional[str] = None
        if self.enabled:
            self.load()

    def load(self) -> None:
        """读取 hooks.json；文件缺失静默跳过，解析失败只记警告。"""
        path = self.events_file
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.hooks = parse_hooks(raw, self.default_timeout)
        except Exception as e:
            self.load_error = str(e)
            logger.warning("加载 hooks 事件文件失败（fail-open 跳过）: %s", e)

    def matched(self, event: str, tool_name: str = "") -> List[HookCommand]:
        """返回订阅该事件且 matcher 命中的 hook，保持注册顺序。"""
        ev = normalize_event(event)
        out: List[HookCommand] = []
        for hook in self.hooks:
            if hook.event not in (ev, "all", "*"):
                continue
            if ev in (EVENT_PRE_TOOL, EVENT_POST_TOOL) and not matches_tool(hook.matcher, tool_name):
                continue
            out.append(hook)
        return out

    def run(self, event: str, payload: Optional[Dict] = None) -> HookDecision:
        """派发一次事件：顺序执行匹配的 hook 并合并决策，自身不抛异常。"""
        if not self.enabled:
            return HookDecision()
        ev = normalize_event(event)
        data = dict(payload or {})
        data.setdefault("hook_event_name", ev)
        data.setdefault("cwd", self.cwd)
        tool_name = str(data.get("tool_name") or "")
        decision = HookDecision()
        for hook in self.matched(ev, tool_name):
            try:
                decision = decision.merge(self._run_one(hook, data))
            except Exception as e:
                logger.warning("hook 派发异常（fail-open 跳过）: %s", e)
        return decision

    def _env(self) -> Dict[str, str]:
        """子进程环境：额外给出 MY_AGENT_HOOK_ROOT 便于脚本自定位。"""
        env = dict(os.environ)
        root = os.path.dirname(os.path.abspath(self.events_file)) if self.events_file else self.cwd
        env["MY_AGENT_HOOK_ROOT"] = root
        return env

    def _run_one(self, hook: HookCommand, data: Dict) -> HookDecision:
        """执行单个 hook：stdin 灌 JSON，再按退出码与 stdout 解析决策。"""
        try:
            proc = subprocess.run(
                hook.command, shell=True, input=json.dumps(data, ensure_ascii=False),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=hook.timeout, cwd=self.cwd, env=self._env(),
            )
        except subprocess.TimeoutExpired:
            logger.warning("hook 超时 %.1fs（fail-open 跳过）: %s", hook.timeout, hook.command)
            return HookDecision(ran=1)
        except Exception as e:
            logger.warning("hook 执行失败（fail-open 跳过）: %s (%s)", hook.command, e)
            return HookDecision(ran=1)
        return self._parse_result(proc.returncode, proc.stdout or "", proc.stderr or "", hook)

    @staticmethod
    def _parse_result(code: int, stdout: str, stderr: str, hook: HookCommand) -> HookDecision:
        """翻译子进程输出：exit 2 阻断、exit 1 只提醒、exit 0 读 stdout JSON。"""
        out = stdout.strip()
        err = stderr.strip()
        if code == 2:
            reason = err or out or f"hook 阻断（exit 2）: {hook.command}"
            return HookDecision(blocked=True, reason=reason, ran=1)
        if code == 1:
            msg = err or out
            return HookDecision(messages=(msg,) if msg else (), ran=1)
        if code != 0:
            logger.warning("hook 退出码 %s（fail-open 跳过）: %s", code, hook.command)
            return HookDecision(ran=1)
        if not out:
            return HookDecision(ran=1)
        try:
            data = json.loads(out)
        except ValueError:
            return HookDecision(messages=(out,), ran=1)
        if not isinstance(data, dict):
            return HookDecision(messages=(out,), ran=1)
        return EventHookRunner.decision_from_json(data)

    @staticmethod
    def decision_from_json(data, fallback_text: str = "") -> HookDecision:
        """解析 JSON 决策字典；外部命令 stdout 与 Python 回调共用同一套语义。"""
        if not isinstance(data, dict):
            return HookDecision(messages=(fallback_text,) if fallback_text else (), ran=1)
        messages: Tuple[str, ...] = ()
        sysmsg = data.get("systemMessage")
        if isinstance(sysmsg, str) and sysmsg.strip():
            messages = (sysmsg.strip(),)
        decision = str(data.get("decision") or "").strip().lower()
        if decision in ("block", "deny"):
            reason = str(data.get("reason") or sysmsg or "hook 请求阻断").strip()
            return HookDecision(blocked=True, reason=reason, messages=messages, ran=1)
        hso = data.get("hookSpecificOutput")
        if isinstance(hso, dict):
            updated = hso.get("updatedInput")
            updated = updated if isinstance(updated, dict) else None
            perm = str(hso.get("permissionDecision") or "").strip().lower()
            if perm in ("allow", "ask", "deny"):
                blocked = perm == "deny"
                reason = str(data.get("reason") or sysmsg or "hook 拒绝该工具调用").strip() if blocked else ""
                return HookDecision(blocked=blocked, reason=reason, permission=perm,
                                    messages=messages, updated_input=updated, ran=1)
            if updated is not None:
                # 只改写参数、不表态（Claude Code 里 updatedInput 通常与 permissionDecision
                # 同现，但单独给时也应生效，否则"改了参数却没改"会静默发生）
                return HookDecision(messages=messages, updated_input=updated, ran=1)
        return HookDecision(messages=messages, ran=1)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<EventHookRunner enabled={self.enabled} file={self.events_file!r} "
                f"hooks={len(self.hooks)}>")
