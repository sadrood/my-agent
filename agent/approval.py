"""审批与命令安全模块（借鉴同类实现的 approval_policy / exec_policy 设计）。
on-failure     自动执行；仅当命令失败后重跑前需要批准（默认）"""
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from config import APPROVAL_CONFIG, resolve_under_root
from agent.execpolicy import ExecPolicy
from tools.base import SANDBOX_LEVELS, RISK_LEVELS, ApprovalRequest

# ============================================================
# 安全关键文件（自我修改防线）
# ============================================================

#: 改写这些文件等于改审批/沙箱/守卫本身：必须人工确认，无人值守下直接拒绝
SECURITY_CRITICAL_PATHS = (
    "agent/approval.py",
    "agent/sandbox.py",
    "agent/guardian.py",
    "agent/execpolicy.py",
    "agent/consent.py",
    "config.py",
    ".env",
)

#: 拿不到真实路径（如 python 代码文本）时的退化判断依据
SECURITY_CRITICAL_NAME_HINTS = tuple(
    os.path.basename(p) for p in SECURITY_CRITICAL_PATHS)


def is_security_critical_path(path) -> bool:
    """路径是否指向安全关键文件（realpath + normcase 归一化后与清单比对）。"""
    raw = str(path or "").strip()
    if not raw:
        return False
    try:
        target = os.path.normcase(os.path.realpath(resolve_under_root(raw)))
        for item in SECURITY_CRITICAL_PATHS:
            if target == os.path.normcase(os.path.realpath(resolve_under_root(item))):
                return True
    except OSError:
        return False
    return False


def security_critical_name_hits(text: str) -> list:
    """文本里出现的安全关键文件名（`.env` 按词边界匹配，避免误伤 os.environ）。"""
    haystack = str(text or "")
    hits = []
    for name in SECURITY_CRITICAL_NAME_HINTS:
        if name == ".env":
            if re.search(r"(?<![\w.])\.env(?![\w])", haystack):
                hits.append(name)
        elif name in haystack:
            hits.append(name)
    return hits

# ============================================================
# 命令安全分类（终端工具）
# ============================================================

# 硬性黑名单：任何沙箱等级下都拒绝（除非 danger-full-access 且策略 never）
BLOCKED_COMMAND_PATTERNS = [
    r":\(\)\s*\{.*:\|:&\s*\};?",          # fork bomb
    r"\bmkfs\b",                          # 格式化文件系统
    r"\bdd\s+if=.*\bof=/dev/",            # 写块设备
    r"\bdd\s+if=/dev/zero\b",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\bformat\s+[a-z]:",                 # Windows format
    r"\bdel\s+/f\s+/s\b",                 # Windows 强制删除
    r"\brd\s+/s\s+/q\b",                  # Windows 递归删除
    r"\bdiskpart\b", r"\bfdisk\b", r"\bparted\b",
    r"\bsc\s+delete\b", r"\breg\s+delete\b",
    r"\brm\s+-rf\s+/(?:\s|$)",            # rm -rf /
    r"\brm\s+-rf\s+~(?:\s|$)",            # rm -rf ~
    r"\brm\s+-rf\s+\.(?:\s|$)",           # rm -rf .
    r"\brm\s+-rf\s+\$HOME\b",
    r"\bgit\s+push\s+.*--force\b",        # 强制推送
    r"\bgit\s+push\s+-f\b",
    r"\bchmod\s+-R\s+777\s+/(?:\s|$)",
    r"\bdel\s+/q\s+[a-z]:\\\\",           # del /q C:\
]

# 高风险：需要批准（workspace-write 下）
HIGH_RISK_COMMAND_PATTERNS = [
    r"\brm\s+-rf\b", r"\brm\s+-fr\b", r"\brm\s+-r\b",
    r"\bdel\b", r"\brmdir\s+/s\b", r"\brd\b",
    r"\bmove\b", r"\bren(?:ame)?\b",
    r"\bpip\s+uninstall\b", r"\bnpm\s+uninstall\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-fd\b",
    r"\bgit\s+checkout\s+--\b", r"\bgit\s+rebase\b",
    r"\breg\s+add\b", r"\bsc\s+create\b", r"\bschtasks\b",
    r"\bnet\s+user\b", r"\bnet\s+localgroup\b", r"\btaskkill\b",
    r"\bkill(?:all)?\b", r"\bpkill\b",
    r"\bchmod\b", r"\bchown\b", r"\bchgrp\b", r"\bsudo\b",
    r"\bwget\b.*\|\s*(?:ba)?sh\b", r"\bcurl\b.*\|\s*(?:ba)?sh\b",
    r">\s*/dev/", r"\btee\b.*\/(?:etc|usr|bin)\b",
    r"\bgh\s+repo\s+delete\b", r"\baws\s+.*\bdelete\b", r"\baz\s+.*\bdelete\b",
]

# 只读/低风险：任何沙箱等级都放行（read-only 下也允许）
READONLY_COMMAND_PATTERNS = [
    r"^(?:dir|ls|ll|tree|type|cat|more|less|head|tail|where|which|echo|print|pwd|cd)\b",
    r"\b(git|gh)\s+(status|log|diff|show|branch|remote\s+-v|tag|stash\s+list)\b",
    r"\bpip\s+(list|show|freeze)\b",
    r"\bnpm\s+(list|ls)\b",
    r"\bpython\s+(?:--version|-V)\b",
    r"\bpython\s+-m\s+pip\s+(list|show)\b",
    r"\bnode\s+(?:--version|-v)\b",
    r"\bjava\s+-version\b",
    r"\bgcc\s+--version\b",
    r"\bwhere\s+python\b", r"\bwhich\s+python\b",
    r"\bcurl\s+-I\b", r"\bping\b", r"\btracert\b", r"\bipconfig\b", r"\bifconfig\b",
    r"\bsysteminfo\b", r"\btasklist\b", r"\bnetstat\b",
    r"\bpython\s+-c\s+[\"']?import\s+sys;?\s*print\b",
]


#: 链接多个子命令的操作符（引号内的不算）
_SHELL_CHAIN_OPS = (";", "|", "&", "\n", "\r")


def _has_shell_operators(cmd: str) -> bool:
    """命令里是否含**引号之外**的链接 / 重定向 / 命令替换操作符。
    （`&& curl ...`、`> 文件`、`` `cmd` ``、`$(cmd)`），就不能整条按只读放行。"""
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2                     # 引号内的转义序列整对跳过
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in _SHELL_CHAIN_OPS or ch in (">", "<", "`"):
            return True
        elif ch == "$" and i + 1 < n and cmd[i + 1] in ("(", "{"):
            return True
        i += 1
    return False


def _split_shell_segments(cmd: str) -> List[str]:
    """按引号外的链接操作符把复合命令切成子命令段。"""
    segments: List[str] = []
    buf: List[str] = []
    quote = ""
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in _SHELL_CHAIN_OPS:
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _is_readonly_segment(segment: str) -> bool:
    """单个子命令段是否只读：必须不含重定向 / 命令替换，且命中只读规则。"""
    if not segment or _has_shell_operators(segment):
        return False
    return any(re.search(p, segment, re.IGNORECASE) for p in READONLY_COMMAND_PATTERNS)


def arguments_from_string(tool_input) -> dict:
    """把 legacy 字符串入参转成审批用的结构化参数。"""
    if isinstance(tool_input, dict):
        return tool_input
    text = str(tool_input or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {"command": text}


def check_tool_execution(approval, tool_manager, tool_name: str, arguments: dict) -> str:
    """执行前的**最低**安全门槛（审批策略）。返回拒绝原因，"" = 放行。"""
    if approval is None:
        return ""
    builder = getattr(tool_manager, "build_approval_request", None)
    if builder is None:
        return ""
    try:
        request = builder(tool_name, arguments or {})
    except Exception:
        return ""
    if request is None:
        return ""
    decision = approval.decide(request)
    return "" if decision.allowed else decision.reason


class CommandSafety:
    """终端命令风险分类器（基于关键词启发式，借鉴同类实现的命令安全思路）。"""

    @classmethod
    def classify(cls, command: str) -> str:
        """分类命令风险等级。"""
        cmd = command.strip()
        if not cmd:
            return "low"

        for pattern in BLOCKED_COMMAND_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return "blocked"

        for pattern in HIGH_RISK_COMMAND_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return "high"

        for pattern in READONLY_COMMAND_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                # 只读档必须覆盖**整条**命令，所以不能在这里直接 return。
                segments = _split_shell_segments(cmd)
                if segments and all(_is_readonly_segment(s) for s in segments):
                    return "low"
                break

        return "medium"  # 无法识别 / 复合命令 → 中风险

    @classmethod
    def is_readonly(cls, command: str) -> bool:
        return cls.classify(command) == "low"


# ============================================================
# 命令白名单（深度防御：default-deny 替代 default-allow）
# ============================================================

# 白名单基线 = 已知安全的只读命令集合（与只读分级同源，随其演进）
COMMAND_WHITELIST_BASE_PATTERNS = READONLY_COMMAND_PATTERNS


def _load_default_exec_policy():
    """按 APPROVAL_CONFIG 构造缺省 execpolicy（可选增量层）。"""
    if not APPROVAL_CONFIG.get("exec_policy_enabled", False):
        return None
    path = APPROVAL_CONFIG.get("exec_policy_file", "./execpolicy.json")
    if not os.path.isfile(path):
        return None
    return ExecPolicy.load(path)


# ============================================================
# 审批决策
# ============================================================

@dataclass
class ApprovalDecision:
    """审批决策结果。"""

    allowed: bool
    reason: str = ""
    required_approval: bool = False   # 是否真的询问了用户


class ApprovalPolicy:
    """审批策略引擎。
    不能豁免沙箱等级不足，allow 只影响"是否需要询问"）"""

    @property
    def mode(self) -> str:
        """当前策略：可调用对象时每次读取都重新解析（支持运行中实时切换）。"""
        m = self._mode
        return m() if callable(m) else m

    def __init__(
        self,
        mode: str = "on-failure",
        sandbox_mode: str = "workspace-write",
        interactive: bool = True,
        approver: Optional[Callable[[ApprovalRequest], bool]] = None,
        command_whitelist: Optional[bool] = None,
        command_whitelist_extra: Optional[List[str]] = None,
        exec_policy: Optional["ExecPolicy"] = None,
    ):
        """Args:
        （enabled 且文件存在才创建，否则保持旧路径零行为变化）。"""
        if not callable(mode) and mode not in ("untrusted", "on-failure", "on-request", "never"):
            raise ValueError(f"未知 approval_policy: {mode}")
        if sandbox_mode not in SANDBOX_LEVELS:
            raise ValueError(f"未知 sandbox_mode: {sandbox_mode}")

        self._mode = mode
        self.sandbox_mode = sandbox_mode
        self.interactive = interactive
        self.approver = approver
        # 白名单缺省走配置中心（显式传参可覆盖，便于测试与嵌入式调用）
        if command_whitelist is None:
            command_whitelist = APPROVAL_CONFIG.get("command_whitelist", False)
        if command_whitelist_extra is None:
            command_whitelist_extra = APPROVAL_CONFIG.get("command_whitelist_extra", [])
        self.command_whitelist = bool(command_whitelist)
        self.command_whitelist_extra = list(command_whitelist_extra)
        # execpolicy DSL：None = 按 APPROVAL_CONFIG 构造（缺省旧路径零行为变化）
        if exec_policy is None:
            exec_policy = _load_default_exec_policy()
        self.exec_policy = exec_policy
        self.decision_log: list = []   # [(request, decision)]

    def _command_whitelisted(self, command: str) -> bool:
        """终端命令是否命中白名单（基线只读集合 + 用户追加的正则）。
        要放行复合命令请显式配 execpolicy DSL 的 allow 规则，别指望这里。"""
        cmd = command.strip()
        if not cmd:
            return False
        if _has_shell_operators(cmd):
            return False    # 含链接/重定向/命令替换 → 不享受白名单捷径
        for pattern in COMMAND_WHITELIST_BASE_PATTERNS + self.command_whitelist_extra:
            try:
                if re.search(pattern, cmd, re.IGNORECASE):
                    return True
            except re.error:
                continue   # 用户配置的非法正则不炸审批门
        return False

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """对一次工具调用做出审批决策。"""
        sandbox_ok = SANDBOX_LEVELS[self.sandbox_mode] >= SANDBOX_LEVELS[request.min_sandbox_mode]
        risk = request.risk_level

        # 1. 黑名单：一律拒绝（danger-full-access + never 除外）
        if risk == "blocked":
            if self.sandbox_mode == "danger-full-access" and self.mode == "never":
                decision = ApprovalDecision(True, "danger-full-access + never 策略放行")
            else:
                decision = ApprovalDecision(False, "该操作命中硬性黑名单，任何情况下均被拒绝。")
            self._record(request, decision)
            return decision

        # 2. 沙箱等级不足：拒绝
        if not sandbox_ok:
            decision = ApprovalDecision(
                False,
                f"当前沙箱等级为 {self.sandbox_mode}，该工具要求 {request.min_sandbox_mode}。",
            )
            self._record(request, decision)
            return decision

        # 2.5 execpolicy DSL（结构化命令策略，白名单模式的升级）。永远在黑名单（第 1 步）与沙箱等级（第 2 步）检查之后评估： deny → 拒绝，allow → 直接放行（跳过后续询问环节），ask → 询问。
        if self.exec_policy is not None:
            verdict = self.exec_policy.decide(request.tool_name, request.command)
            if verdict == "deny":
                decision = ApprovalDecision(False, "execpolicy 规则：命中 deny，拒绝执行。")
                self._record(request, decision)
                return decision
            if verdict == "allow":
                decision = ApprovalDecision(True, "execpolicy 规则：命中 allow，直接放行。")
                self._record(request, decision)
                return decision
            if verdict == "ask":
                decision = self._ask(request, "execpolicy 规则：命中 ask，需要人工确认")
                self._record(request, decision)
                return decision

        # 3. 命令白名单（深度防御）：开启后终端命令只有命中白名单才继续走原策略；未命中的升级为需人工批准（never/无人值守下直接拒绝）。黑名单命令在第 1 步已处理，此处只拦"未列入白名单"的命令。
        if (self.command_whitelist and request.tool_name == "terminal"
                and request.command.strip()):
            if not self._command_whitelisted(request.command):
                if self.mode == "never":
                    decision = ApprovalDecision(
                        False,
                        "命令白名单模式：该命令未命中白名单且策略为 never（无人值守），拒绝执行。",
                    )
                    self._record(request, decision)
                    return decision
                decision = self._ask(
                    request, "命令白名单模式：该命令不在白名单中，需要人工确认")
                self._record(request, decision)
                return decision

        # 3. 工具主动要求批准（on-request 语义）
        if getattr(request, "approval", "auto") == "on-request":
            if self.mode == "never":
                decision = ApprovalDecision(False, "工具要求批准，但策略为 never。")
            else:
                decision = self._ask(request, "该工具主动要求人工确认")
            self._record(request, decision)
            return decision

        # 4. 按策略处理
        if self.mode == "never":
            decision = ApprovalDecision(True, "策略 never：放行（黑名单除外）。")
        elif self.mode == "untrusted":
            # 不信任模型：低风险自动放行，其余全部询问
            if risk == "low":
                decision = ApprovalDecision(True, "低风险操作，untrusted 策略下自动放行。")
            else:
                decision = self._ask(request, f"untrusted 策略：{risk} 风险操作需要确认")
        elif self.mode == "on-request":
            # 仅在工具主动请求时询问（已在第 3 步处理）→ 其余放行
            decision = ApprovalDecision(True, "on-request 策略：放行。")
        else:  # on-failure
            # 自动执行；高风险操作在非 danger-full-access 下仍需确认
            if risk in ("high",) and self.sandbox_mode != "danger-full-access":
                decision = self._ask(request, f"高风险命令（{risk}），执行前需要确认")
            else:
                decision = ApprovalDecision(True, "on-failure 策略：自动执行。")

        self._record(request, decision)
        return decision

    def _ask(self, request: ApprovalRequest, reason: str) -> ApprovalDecision:
        """询问用户（或调用自定义 approver）。"""
        if self.approver is not None:
            try:
                allowed = bool(self.approver(request))
                return ApprovalDecision(
                    allowed,
                    "自定义 approver 返回" + ("批准。" if allowed else "拒绝。"),
                    required_approval=True,
                )
            except Exception as e:
                return ApprovalDecision(False, f"approver 调用失败: {e}", required_approval=False)

        if not self.interactive:
            # 无人值守：默认拒绝（保守）
            return ApprovalDecision(False, "非交互模式下默认拒绝（未提供 approver）。", required_approval=False)

        # 内置交互（主流风格：一行风险说明 + 命令 + y/N）
        print()
        print(f"⚠ 需要批准 · {request.tool_name}")
        print(f"  原因: {reason}")
        print(f"  $ {request.command[:400]}")
        sys.stdout.flush()   # 确保提示立即可见（流式输出后无换行的情况）
        try:
            answer = input("  允许执行? [y/N]: ").strip().lower()
            allowed = answer in ("y", "yes", "是", "允许")
        except (EOFError, KeyboardInterrupt):
            allowed = False
        return ApprovalDecision(allowed, "用户选择" + ("批准。" if allowed else "拒绝。"), required_approval=True)

    def _record(self, request: ApprovalRequest, decision: ApprovalDecision):
        # 只保留最近 N 条：decision_log 在长驻进程（桌面端）里只增不减，而 agent 每次 run 都要全量扫一遍统计被拒次数 → 越用越慢、内存越涨。统计口径（report / 被拒计数）只看近期即可。
        self.decision_log.append({"tool": request.tool_name, "command": request.command[:200], "risk": request.risk_level, "decision": "allow" if decision.allowed else "deny", "reason": decision.reason})
        max_len = int(APPROVAL_CONFIG.get("decision_log_max", 200))
        if max_len > 0 and len(self.decision_log) > max_len:
            del self.decision_log[:-max_len]

    def report(self) -> str:
        """输出本次运行的审批统计。"""
        if not self.decision_log:
            return "无审批记录。"
        allowed = sum(1 for d in self.decision_log if d["decision"] == "allow")
        lines = [f"审批记录（{len(self.decision_log)} 次，放行 {allowed} 次）:"]
        for d in self.decision_log[-20:]:
            lines.append(f"  [{d['decision']:5}] {d['tool']}: {d['command'][:100]}")
        return "\n".join(lines)
