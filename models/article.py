"""文章工坊：多模型互审的写作流水线。

为什么要多个模型：让同一个模型审自己写的稿子，它的知识边界、行文偏好、盲点
与作者完全重合，"互审"最后只会变成自我复述（同厂不同名的模型也基本重合）。
跨厂商互审才有信息增益——所以默认让**另一家**的模型当审阅/校对，
写稿与修订留在主模型（可用 ARTICLE_MODEL_* 逐个阶段改）。

流水线（每一步都落盘，随时可查可回滚）：

    大纲(可关) → 初稿
       ↑            ↓
       │        审阅(换模型，只提问题不改稿)
       │            ↓
       │      事实核查(只查被点名的可疑说法 → 联网检索 → 依据资料判定真伪)
       │            ↓
       └──── 修订(作者逐条回应 + 出完整新稿)   ← 最多 max_revise_rounds 轮
                    ↓
                校对(换模型，只挑错)  → 定稿(只落校对修正) → final.md

设计要点：
- **职责分离**：审阅/校对只提问题，改写只由作者模型做——否则审阅方会顺手重写，
  作者就失去了"回应意见"的机会，改动也不可追溯。
- **有界循环**：审阅方没有"严重/中等"问题就立刻停；某轮修订后正文没变化也停
  （继续问下去只是烧 token）；到上限就带着剩余意见定稿。
- **fall-open 解析**：结构化阶段要求只输出 JSON，但解析失败时降级为"把原文当建议
  文本"继续跑——格式问题绝不能让整条流水线断在半路。
- **事实核查只查被点名的**：全文逐条联网既慢又贵，且大部分句子本来就不需要核实。
- 每一轮的意见、事实核查结论、修订说明都落盘，最终 `changes.md` 是完整的修改台账。
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import ARTICLE_CONFIG, ARTICLE_ENDPOINTS, resolve_under_root
from models.prompts import (
    ARTICLE_CLAIMS_SYSTEM_PROMPT,
    ARTICLE_CLAIMS_USER_PROMPT_TEMPLATE,
    ARTICLE_DRAFT_SYSTEM_PROMPT,
    ARTICLE_DRAFT_USER_PROMPT_TEMPLATE,
    ARTICLE_FINALIZE_SYSTEM_PROMPT,
    ARTICLE_FINALIZE_USER_PROMPT_TEMPLATE,
    ARTICLE_OUTLINE_SYSTEM_PROMPT,
    ARTICLE_OUTLINE_USER_PROMPT_TEMPLATE,
    ARTICLE_PROOFREAD_SYSTEM_PROMPT,
    ARTICLE_PROOFREAD_USER_PROMPT_TEMPLATE,
    ARTICLE_REVIEW_SYSTEM_PROMPT,
    ARTICLE_REVIEW_USER_PROMPT_TEMPLATE,
    ARTICLE_REVISE_SYSTEM_PROMPT,
    ARTICLE_REVISE_USER_PROMPT_TEMPLATE,
    ARTICLE_VERIFY_SYSTEM_PROMPT,
    ARTICLE_VERIFY_USER_PROMPT_TEMPLATE,
)

# 阶段 → (temperature, max_tokens)。审阅/校对要稳定（低温度），写稿要放开。
STAGE_SAMPLING: Dict[str, Tuple[float, int]] = {
    "outline": (0.7, 1600),
    "draft": (0.75, 4096),
    "review": (0.2, 2000),
    "claims": (0.1, 800),
    "verify": (0.1, 800),
    "revise": (0.6, 4096),
    "proofread": (0.1, 2000),
    "finalize": (0.2, 4096),
}

# 审阅方与写作方必须是不同的模型，否则"互审"没有信息增益
_REVIEWER_STAGES = ("review", "proofread")

_ARTICLE_DELIM = re.compile(r"<<<\s*ARTICLE\s*\n(.*?)\n\s*(?:ARTICLE\s*>>>|ARTICLE)\s*$",
                            re.DOTALL | re.IGNORECASE)


class ArticleError(RuntimeError):
    """流水线里某一阶段失败（消息带阶段名与模型，便于定位）。"""


# ================================================================
# 数据结构
# ================================================================

@dataclass
class ArticleIssue:
    """一条审阅/校对意见。"""
    severity: str = "中等"      # 严重 / 中等 / 轻微
    kind: str = "其它"          # 事实 / 逻辑 / 结构 / 文风 / 语言 / 错别字 ...
    quote: str = ""
    problem: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {"severity": self.severity, "kind": self.kind, "quote": self.quote,
                "problem": self.problem, "suggestion": self.suggestion}

    def line(self) -> str:
        quote = f'「{self.quote}」' if self.quote else ""
        fix = f" → {self.suggestion}" if self.suggestion else ""
        return f"[{self.severity}·{self.kind}] {quote}{self.problem}{fix}"


@dataclass
class FactCheck:
    """一条断言的核查结论。"""
    claim: str = ""
    verdict: str = "未查到"        # 证实 / 证伪 / 存疑 / 未查到
    evidence: str = ""
    source: str = ""
    query: str = ""

    def to_dict(self) -> dict:
        return {"claim": self.claim, "verdict": self.verdict, "evidence": self.evidence,
                "source": self.source, "query": self.query}

    def line(self) -> str:
        return f"[{self.verdict}] {self.claim}｜依据: {self.evidence or '无'}" + (
            f"（{self.source}）" if self.source else "")


@dataclass
class StageRecord:
    """一次模型调用的开销记录（进 meta.json，便于复盘哪一步最贵）。"""
    stage: str
    endpoint: str
    model: str
    seconds: float
    out_chars: int

    def to_dict(self) -> dict:
        return {"stage": self.stage, "endpoint": self.endpoint, "model": self.model,
                "seconds": self.seconds, "out_chars": self.out_chars}


@dataclass
class CheckResult:
    """对**已有文稿**做审阅/校对的结果（不改写全文；校对模式额外给修正稿）。"""
    mode: str                                  # review / proofread
    title: str
    text: str                                  # 原文
    source: str = ""                           # 来源文件路径（若来自文件）
    fixed_text: str = ""                       # 校对后的正文（润色落定后才有）
    issues: List[ArticleIssue] = field(default_factory=list)
    fact_checks: List[FactCheck] = field(default_factory=list)
    stages: List[StageRecord] = field(default_factory=list)
    out_dir: str = ""
    warnings: List[str] = field(default_factory=list)

    def issue_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for it in self.issues:
            counts[it.severity] = counts.get(it.severity, 0) + 1
        return counts

    def verdict_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for fc in self.fact_checks:
            counts[fc.verdict] = counts.get(fc.verdict, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "title": self.title, "source": self.source,
            "out_dir": self.out_dir, "text_chars": len(self.text),
            "fixed_chars": len(self.fixed_text),
            "issues": [i.to_dict() for i in self.issues],
            "factchecks": [f.to_dict() for f in self.fact_checks],
            "stages": [s.to_dict() for s in self.stages],
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        label = "审阅" if self.mode == "review" else "校对"
        lines = [f"{label}完成：{self.title}"]
        if self.source:
            lines.append(f"原文：{self.source}（{len(self.text)} 字）")
        if self.out_dir:
            lines.append(f"产物目录：{self.out_dir}")
        models = " · ".join(f"{s.stage}={s.endpoint}:{s.model}" for s in self.stages)
        if models:
            lines.append(f"阶段模型：{models}")
        counts = self.issue_counts()
        lines.append("问题：" + (" · ".join(f"{k} {v}" for k, v in counts.items())
                                if counts else "未发现问题"))
        verdicts = self.verdict_counts()
        if verdicts:
            lines.append("事实核查：" + " · ".join(f"{k} {v}" for k, v in verdicts.items()))
        if self.mode == "proofread" and self.fixed_text:
            lines.append(f"修正稿已生成（{len(self.fixed_text)} 字，见 final.md）")
        if self.warnings:
            lines.append("注意：" + "；".join(self.warnings))
        return "\n".join(lines)


@dataclass
class ArticleResult:
    topic: str
    final_text: str
    out_dir: str = ""
    outline: str = ""
    draft: str = ""
    rounds: int = 0                                   # 实际进行的审阅轮数
    issues_by_round: List[List[ArticleIssue]] = field(default_factory=list)
    proofread_issues: List[ArticleIssue] = field(default_factory=list)
    fact_checks: List[FactCheck] = field(default_factory=list)
    stages: List[StageRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # ---- 统计 ----
    def issue_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for issues in self.issues_by_round:
            for it in issues:
                counts[it.severity] = counts.get(it.severity, 0) + 1
        return counts

    def verdict_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for fc in self.fact_checks:
            counts[fc.verdict] = counts.get(fc.verdict, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "topic": self.topic, "out_dir": self.out_dir, "rounds": self.rounds,
            "final_chars": len(self.final_text),
            "issues": [[i.to_dict() for i in r] for r in self.issues_by_round],
            "proofread": [i.to_dict() for i in self.proofread_issues],
            "factchecks": [f.to_dict() for f in self.fact_checks],
            "stages": [s.to_dict() for s in self.stages],
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        """给人看的一页小结（CLI / 工具输出用）。"""
        lines = [f"文章已定稿：{self.topic}"]
        if self.out_dir:
            lines.append(f"产物目录：{self.out_dir}")
        lines.append(f"篇幅：{len(self.final_text)} 字；审阅 {self.rounds} 轮"
                     f"；模型调用 {len(self.stages)} 次")
        counts = self.issue_counts()
        if counts:
            lines.append("审阅意见：" + " · ".join(f"{k} {v}" for k, v in counts.items()))
        if self.proofread_issues:
            lines.append(f"校对问题：{len(self.proofread_issues)} 条（已落定）")
        verdicts = self.verdict_counts()
        if verdicts:
            lines.append("事实核查：" + " · ".join(f"{k} {v}" for k, v in verdicts.items()))
        models = []
        seen = set()
        for s in self.stages:
            tag = f"{s.stage}={s.endpoint}:{s.model}"
            if s.stage not in seen:
                seen.add(s.stage)
                models.append(tag)
        if models:
            lines.append("阶段模型：" + " · ".join(models))
        if self.warnings:
            lines.append("注意：" + "；".join(self.warnings))
        return "\n".join(lines)


# ================================================================
# 模型端点
# ================================================================

_LLM_CACHE: Dict[tuple, Any] = {}


def parse_spec(spec: str, endpoints: Optional[dict] = None) -> Tuple[str, str, dict]:
    """把 "main" / "<端点名>" / "<端点名>:<模型名>" 解析成 (端点名, 模型名, 端点配置)。"""
    eps = endpoints if endpoints is not None else ARTICLE_ENDPOINTS
    name, _, override = str(spec or "main").partition(":")
    name = name.strip() or "main"
    ep = eps.get(name)
    if ep is None:
        raise ArticleError(
            f"未知模型端点 {name!r}（可用：{', '.join(sorted(eps))}）")
    return name, (override.strip() or str(ep.get("model") or "")), ep


def build_llm(spec: str, endpoints: Optional[dict] = None):
    """按端点规格构建 LLM 实例（同规格复用，避免重复建客户端）。

    端点没配密钥时回退主模型：宁可用同一个模型把文章写完，也不要整条流水线失败
    ——但调用方会把这件事记进 warnings，因为同模型互审价值有限。
    """
    from models.llm import LLM

    name, model, ep = parse_spec(spec, endpoints)
    if not (ep.get("api_key") and ep.get("base_url")):
        if name != "main":
            name, model, ep = parse_spec("main", endpoints)
        if not (ep.get("api_key") and ep.get("base_url")):
            raise ArticleError(
                "没有可用的对话模型端点：请在 .env 配置 LLM_API_KEY / LLM_BASE_URL")
    key = (name, model, str(ep.get("base_url")))
    if key not in _LLM_CACHE:
        _LLM_CACHE[key] = LLM(api_key=ep["api_key"], base_url=ep["base_url"], model=model)
    return _LLM_CACHE[key]


def stage_models(config: Optional[dict] = None) -> Dict[str, str]:
    """返回 {阶段: "端点:模型"}，供 CLI/工具展示"谁写谁审"。"""
    cfg = config if config is not None else ARTICLE_CONFIG
    out: Dict[str, str] = {}
    for stage, spec in (cfg.get("stages") or {}).items():
        try:
            name, model, _ = parse_spec(spec)
            out[stage] = f"{name}:{model}"
        except Exception:                       # noqa: BLE001
            out[stage] = str(spec)
    return out


# ================================================================
# 解析工具（容错优先：格式问题不能让流水线断掉）
# ================================================================

def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏（模型很爱加）。"""
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", t, re.DOTALL)
    return m.group(1).strip() if m else t


def _json_block(text: str, opener: str, closer: str) -> Optional[Any]:
    """从文本里找出第一个能解析的 JSON 数组/对象（容忍前后废话与围栏）。"""
    t = _strip_fence(text)
    start = t.find(opener)
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(t[start:])
            return obj
        except Exception:                       # noqa: BLE001
            start = t.find(opener, start + 1)
    return None


def _pick(d: dict, *names: str, default: str = "") -> str:
    """从模型给的键里挑第一个命中的（各家模型键名不一致，别死认一种）。"""
    for n in names:
        v = d.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def parse_issues(text: str) -> List[ArticleIssue]:
    """解析审阅/校对的 JSON 意见；解析不出来就返回空列表（调用方会降级用原文）。"""
    data = _json_block(text, "[", "]")
    if isinstance(data, dict):                  # 有的模型会包一层 {"issues": [...]}
        for key in ("issues", "problems", "items", "results", "comments"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []
    out: List[ArticleIssue] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        severity = _pick(it, "severity", "level", "等级", "严重度", default="中等")
        kind = _pick(it, "kind", "type", "category", "类型", default="其它")
        problem = _pick(it, "problem", "issue", "description", "问题", "说明")
        suggestion = _pick(it, "suggestion", "fix", "advice", "建议", "改法", "修改")
        quote = _pick(it, "quote", "text", "excerpt", "原文", "片段")
        if not (problem or suggestion):
            continue
        out.append(ArticleIssue(severity=_norm_severity(severity), kind=kind.strip(),
                                quote=quote, problem=problem, suggestion=suggestion))
    return out


def _norm_severity(raw: str) -> str:
    s = (raw or "").strip()
    for key in ("严重", "中等", "轻微"):
        if key in s:
            return key
    low = s.lower()
    if low in ("high", "critical", "major", "blocker"):
        return "严重"
    if low in ("medium", "moderate", "normal"):
        return "中等"
    if low in ("low", "minor", "nit", "trivial"):
        return "轻微"
    return s or "中等"


def parse_claims(text: str, limit: int = 5) -> List[dict]:
    """解析"可检索断言"列表。"""
    data = _json_block(text, "[", "]")
    if isinstance(data, dict):
        for key in ("claims", "items", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        # 降级：模型可能只是逐行写断言，取非空行当断言、原句当查询
        return [{"claim": ln.strip(), "query": ln.strip()[:12]}
                for ln in _strip_fence(text).splitlines() if len(ln.strip()) > 6][:limit]
    out = []
    for it in data:
        if isinstance(it, str):
            out.append({"claim": it.strip(), "query": it.strip()[:12]})
            continue
        if not isinstance(it, dict):
            continue
        claim = _pick(it, "claim", "statement", "fact", "断言")
        if not claim:
            continue
        out.append({"claim": claim, "query": _pick(it, "query", "search", "关键词",
                                                   default=claim[:12])})
        if len(out) >= limit:
            break
    return out[:limit]


def parse_factcheck(text: str, claim: str, query: str = "") -> FactCheck:
    """解析单条核查结论，解析失败时保守记为"未查到"。"""
    data = _json_block(text, "{", "}")
    if not isinstance(data, dict):
        return FactCheck(claim=claim, verdict="未查到",
                         evidence=_clip(_strip_fence(text), 200), query=query)
    verdict = _pick(data, "verdict", "conclusion", "结论", default="未查到")
    for key in ("证实", "证伪", "存疑", "未查到"):
        if key in verdict:
            verdict = key
            break
    else:
        verdict = "未查到"
    return FactCheck(claim=_pick(data, "claim", "断言", default=claim),
                     verdict=verdict,
                     evidence=_pick(data, "evidence", "依据", "证据"),
                     source=_pick(data, "source", "来源", "url", "sources"),
                     query=query)


def split_revision(text: str) -> Tuple[str, str]:
    """把修订输出拆成 (修改说明, 新正文)。

    模型没按规定加分隔符时：整段当正文，说明里注明——**不能因为格式就丢掉新稿**。
    """
    m = _ARTICLE_DELIM.search(_strip_fence(text))
    if not m:
        # 宽松兜底：只有起始标记，取标记之后到结尾
        m2 = re.search(r"<<<\s*ARTICLE\s*\n(.*)", _strip_fence(text), re.DOTALL | re.IGNORECASE)
        if not m2:
            return ("（模型未输出规定的分隔符，已把整段输出当作新稿；请人工确认）",
                    _strip_fence(text))
        return ("（模型未闭合分隔符，已取标记之后的内容作为新稿）", m2.group(1).strip())
    body = m.group(1).strip()
    head = _strip_fence(text)[:m.start()].strip()
    return (head or "（模型未给出修改说明）", body)


def _clip(text: str, limit: int) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


#: 限流/配额类错误的识别串。上游各家写法不一（429 / tpm / rpm / insufficient_quota /
#: "配额已满"），按文本识别才不会漏——漏了就变成"换个端点也没试就直接失败"。
_RATE_LIMIT_HINTS = ("429", "rate limit", "rate_limit", "tpm", "rpm", "quota",
                     "限流", "配额", "too many requests")


def _looks_rate_limited(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in _RATE_LIMIT_HINTS)


#: 中文正文里不该出现的半角标点 → 对应的全角写法
_HALF_PUNCT = {",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "："}
_CJK_RE = "\\u4e00-\\u9fff"
#: 几乎不会被正当叠用的虚词（叠用即错别字的高置信信号）
_DOUBLE_SUSPECT = set("的了在是和与我你他她它不也就都还而但很把被给让从对为以及或")


def _strip_code(text: str) -> str:
    """去掉代码围栏与行内代码：那里的半角标点是合法的，不该被当成中文标点问题。"""
    without_fence = re.sub(r"```.*?```", "", text or "", flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", without_fence)


def mechanical_issues(text: str) -> List[ArticleIssue]:
    """确定性机械校对：半角标点、引号配对、叠字、省略号写法。

    为什么要用规则补模型：实测模型校对会漏掉"全文都用了半角逗号"这种惯例问题
    （它更关注语义与措辞），而这恰恰是最该被逐条指出来的。规则部分不花 token、
    可复现、可测试；语义问题仍交给另一个模型。
    """
    body = _strip_code(text)
    if not body.strip():
        return []
    out: List[ArticleIssue] = []

    # 1) 中文语境里的半角标点（要求前后都是汉字，避免误报英文/数字场景）
    counts: Dict[str, int] = {}
    samples: Dict[str, str] = {}
    pattern = rf"([{_CJK_RE}])([{re.escape(''.join(_HALF_PUNCT))}])(?=[{_CJK_RE}])"
    for m in re.finditer(pattern, body):
        ch = m.group(2)
        counts[ch] = counts.get(ch, 0) + 1
        samples.setdefault(ch, m.group(0))
    for ch, n in counts.items():
        out.append(ArticleIssue(
            severity="轻微", kind="标点", quote=samples[ch],
            problem=f"中文正文里用了半角 {ch}（全文共 {n} 处）",
            suggestion=f"改成全角「{_HALF_PUNCT[ch]}」"))

    # 2) 中文引号不配对
    for open_q, close_q in (("“", "”"), ("‘", "’")):
        a, b = body.count(open_q), body.count(close_q)
        if a != b:
            out.append(ArticleIssue(
                severity="中等", kind="标点", quote=f"{open_q}×{a} / {close_q}×{b}",
                problem="中文引号不配对（成对符号数量不等）",
                suggestion="补齐或删除多余的引号"))

    # 3) 高置信度叠字（的的 / 了了 / 是是 …）
    for m in re.finditer(rf"([{_CJK_RE}])\1", body):
        if m.group(1) not in _DOUBLE_SUSPECT:
            continue
        start = max(0, m.start() - 6)
        out.append(ArticleIssue(
            severity="轻微", kind="错别字", quote=body[start:m.end() + 6],
            problem=f"疑似叠字错误：「{m.group(1)}{m.group(1)}」",
            suggestion=f"确认是否多打了一个「{m.group(1)}」"))
        if len(out) > 12:
            break

    # 4) 省略号写法（中文用「……」，不是三个句点）
    if re.search(rf"[{_CJK_RE}](\.\.\.|。。。)", body):
        out.append(ArticleIssue(
            severity="轻微", kind="标点", quote="…",
            problem="中文省略号写成了三个句点/半角点",
            suggestion="改用「……」"))

    seen, uniq = set(), []
    for it in out:
        key = (it.kind, it.quote, it.problem)
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return uniq[:10]


def merge_issues(*groups: List[ArticleIssue]) -> List[ArticleIssue]:
    """合并多来源意见并去重（机械规则在前：它们是确定的，模型意见在后）。"""
    seen, out = set(), []
    for group in groups:
        for it in (group or []):
            key = (it.kind, it.quote, it.problem)
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
    return out


#: 值得"重试/换端点"的暂时性故障。配额耗尽的端点除了回 429，**还会回空正文**
#: （上游实测：第一次 429，紧接着一次 HTTP 200 但 content 为空）——只认 429
#: 会漏掉这半数的限流表现，导致明明另一家能用却直接失败。
_TRANSIENT_HINTS = _RATE_LIMIT_HINTS + (
    "返回空内容", "timeout", "timed out", "502", "503", "504",
    "connection", "temporarily", "overloaded",
)


def _is_transient_error(message: str) -> bool:
    m = (message or "").lower()
    return any(h in m for h in _TRANSIENT_HINTS)


def zhihu_lookup(query: str, limit: int = 3) -> List[dict]:
    """知乎全网搜索（开放平台 global_search，5000 次/日）作为核查检索通道。

    为什么优先用它而不是抓搜索引擎结果页：返回的是结构化的
    Title/Url/ContentText，直接可用；抓 Bing/Google 的 HTML 既不稳又常被拦
    （实测抓取通道经常一个来源都取不到，事实核查只能退化成"未查到"）。
    未配置 ZHIHU_ACCESS_SECRET 时返回空列表，调用方自动落到下一个通道。
    """
    from models.zhihu import ZhihuClient

    client = ZhihuClient()
    if not getattr(client, "configured", False):
        return []
    data = client.web_search(query, count=max(1, min(int(limit), 20)))
    items = data.get("Items") if isinstance(data, dict) else None
    out: List[dict] = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": str(it.get("Title") or "").strip(),
            "url": str(it.get("Url") or "").strip(),
            "snippet": str(it.get("ContentText") or "").strip()[:1200],
        })
    return out


def slugify(topic: str, limit: int = 24) -> str:
    """主题 → 目录名（保留中文，去掉路径与非法字符）。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", (topic or "").strip())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return (s[:limit] or "article")


# ================================================================
# 流水线
# ================================================================

class ArticlePipeline:
    """多模型互审写作流水线。

    用法：
        p = ArticlePipeline()
        result = p.write("AI Agent 的记忆机制", requirements="给技术读者，3000 字")
        print(result.summary())

    Args:
        llms: {阶段: LLM} 覆盖默认端点解析（测试注入假模型用）
        config: 覆盖 ARTICLE_CONFIG
        save_dir: 覆盖产物目录
        emit: 事件回调 emit(event_type, data)（→ dashboard/rollout）
        lookup: 事实核查用的检索函数 lookup(query, limit) -> [{title,url,snippet}]
        tool_manager: 未注入 lookup 时用它 + DeepResearcher 现建一个
    """

    def __init__(self, llms: Optional[Dict[str, Any]] = None,
                 config: Optional[dict] = None,
                 save_dir: Optional[str] = None,
                 emit: Optional[Callable[[str, dict], None]] = None,
                 lookup: Optional[Callable[[str, int], List[dict]]] = None,
                 tool_manager=None,
                 endpoints: Optional[dict] = None,
                 verbose: bool = True):
        self.cfg = dict(ARTICLE_CONFIG if config is None else config)
        self.endpoints = dict(endpoints if endpoints is not None else ARTICLE_ENDPOINTS)
        self.llms: Dict[str, Any] = dict(llms or {})
        self.save_dir = save_dir or self.cfg.get("save_dir") or "./output/articles"
        self.emit = emit
        self.lookup = lookup
        self.tool_manager = tool_manager
        self.verbose = verbose

        self.out_dir = ""
        self.stages: List[StageRecord] = []
        self.warnings: List[str] = []
        self._warned: set = set()
        #: 各阶段最近一次成功输出（失败时用来留住"当前最好的一版"）
        self._last_text: Dict[str, str] = {}

    # ---------------- 模型 ----------------

    def _spec(self, stage: str) -> str:
        return str((self.cfg.get("stages") or {}).get(stage, "main"))

    def _stage_llm(self, stage: str, spec: Optional[str] = None) -> Tuple[Any, str, str]:
        """取该阶段要用的 LLM 实例与 (端点名, 模型名) 标签。

        注入的假模型优先（测试），键可以是阶段名（本阶段默认端点），
        也可以是 "阶段@端点"（用于验证限流换端点后的行为）。
        """
        spec = spec if spec is not None else self._spec(stage)
        name, model, _ = parse_spec(spec, self.endpoints)
        injected = self.llms.get(f"{stage}@{name}")
        if injected is None and spec == self._spec(stage):
            injected = self.llms.get(stage)
        if injected is not None:
            return injected, name, model
        return build_llm(spec, self.endpoints), name, model

    def _fallback_spec(self, stage: str) -> str:
        """限流兜底端点：默认自动换成"另一家"（写/审本来就是两家，换过去最省事）。"""
        mode = str(self.cfg.get("fallback_endpoint", "auto") or "").strip()
        if not mode or mode.lower() in ("none", "off", "false"):
            return ""
        try:
            own = parse_spec(self._spec(stage), self.endpoints)[0]
        except Exception:                       # noqa: BLE001
            return ""
        if mode.lower() != "auto":
            return "" if mode == own else mode
        for alt in self.endpoints:
            if alt != own:
                try:
                    if not (self.endpoints[alt].get("api_key")
                            and self.endpoints[alt].get("base_url")):
                        continue
                except Exception:               # noqa: BLE001
                    continue
                return alt
        return ""

    def _note_fallback(self, stage: str, endpoint: str) -> None:
        """审阅/校对与初稿用了同一端点 → 记一条警告（同源互审收益有限）。"""
        if stage not in _REVIEWER_STAGES:
            return
        try:
            draft_ep = parse_spec(self._spec("draft"), self.endpoints)[0]
        except Exception:                       # noqa: BLE001
            return
        if endpoint == draft_ep and "same-model" not in self._warned:
            self._warned.add("same-model")
            self.warnings.append(
                f"审阅/校对与写作使用的是同一个端点（{endpoint}）——互审会退化为"
                "自我复述；建议在 .env 把 ARTICLE_MODEL_REVIEW / ARTICLE_MODEL_PROOFREAD "
                "指向另一家模型")

    def _call_once(self, stage: str, spec: str, system: str, user: str) -> str:
        llm, endpoint, model = self._stage_llm(stage, spec)
        self._note_fallback(stage, endpoint)
        temperature, max_tokens = STAGE_SAMPLING.get(stage, (0.3, 2000))
        t0 = time.time()
        try:
            out = llm.chat([{"role": "system", "content": system},
                            {"role": "user", "content": user}],
                           temperature=temperature, max_tokens=max_tokens)
        except Exception as e:                  # noqa: BLE001
            raise ArticleError(
                f"阶段「{stage}」调用失败（{endpoint}:{model}）：{str(e)[:200]}") from e
        text = (out or "").strip()
        if not text:
            raise ArticleError(f"阶段「{stage}」返回空内容（{endpoint}:{model}）")
        return (endpoint, model, text, round(time.time() - t0, 1))

    def _call(self, stage: str, system: str, user: str) -> str:
        """调用一个阶段，带限流退避与跨端点兜底。

        上游配额是真实存在的硬约束：长文流水线一次跑 8+ 次大请求，很容易撞
        TPM/RPM（实测默认供应商就在 revise 阶段 429 过）。所以这里分三层兜：
        同端点退避重试 → 换另一家端点继续 → 都不行才失败，并把已完成的部分留住。
        """
        own = self._spec(stage)
        specs = [own]
        fallback = self._fallback_spec(stage)
        if fallback and fallback != own:
            specs.append(fallback)
        retries = max(0, int(self.cfg.get("stage_retries") if
                             self.cfg.get("stage_retries") is not None else 1))
        # 注意不能写 `cfg.get("retry_wait", 20) or 20`：配 0（测试里就是不等待）
        # 会被 or 当成假值换成 20 秒，白白拖慢每次重试。
        raw_wait = self.cfg.get("retry_wait")
        wait_base = float(20 if raw_wait is None else raw_wait)

        last_error: Optional[ArticleError] = None
        for idx, spec in enumerate(specs):
            attempts = retries + 1 if idx == 0 else 1
            for attempt in range(attempts):
                try:
                    endpoint, model, text, seconds = self._call_once(
                        stage, spec, system, user)
                except ArticleError as e:
                    last_error = e
                    if not _is_transient_error(str(e)):
                        raise                    # 非暂时性错误换端点也没用，直接失败
                    if attempt + 1 < attempts:
                        wait = wait_base * (attempt + 1)
                        self._emit("article_wait", {"stage": stage, "seconds": wait,
                                                    "reason": "上游限流/暂时故障"})
                        if self.verbose:
                            print(f"  · {stage} 被限流，等待 {wait:.0f}s 后重试…")
                        time.sleep(wait)
                    continue
                if idx > 0:
                    note = f"阶段「{stage}」被上游限流，已临时换用 {endpoint} 完成"
                    if note not in self.warnings:
                        self.warnings.append(note)
                    self._emit("article_fallback", {"stage": stage, "endpoint": endpoint})
                rec = StageRecord(stage=stage, endpoint=endpoint, model=model,
                                  seconds=seconds, out_chars=len(text))
                self.stages.append(rec)
                self._emit("article_stage", {"stage": stage, "endpoint": endpoint,
                                             "model": model, "seconds": rec.seconds,
                                             "out_chars": rec.out_chars})
                if self.verbose:
                    print(f"  · {stage:9s} {endpoint}:{model} "
                          f"{rec.seconds:5.1f}s → {rec.out_chars} 字")
                self._last_text[stage] = text
                return text
            if idx + 1 < len(specs) and self.verbose:
                print(f"  · {stage} 在 {parse_spec(spec, self.endpoints)[0]} 上重试无效，"
                      f"换端点继续…")
        raise last_error or ArticleError(f"阶段「{stage}」失败")

    def _emit(self, event: str, data: dict) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event, data)
        except Exception:                       # noqa: BLE001
            pass

    # ---------------- 落盘 ----------------

    def _write(self, name: str, text: str) -> str:
        path = os.path.join(self.out_dir, name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except Exception as e:                  # noqa: BLE001
            self.warnings.append(f"写入 {name} 失败：{str(e)[:120]}")
        return path

    def _write_json(self, name: str, obj) -> None:
        try:
            self._write(name, json.dumps(obj, ensure_ascii=False, indent=2))
        except Exception as e:                  # noqa: BLE001
            self.warnings.append(f"写入 {name} 失败：{str(e)[:120]}")

    def _make_out_dir(self, topic: str, suffix: str = "") -> str:
        base = resolve_under_root(self.save_dir)
        tag = slugify(topic) + (f"-{suffix}" if suffix else "")
        path = os.path.join(base, f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(path, exist_ok=True)
        return path

    # ---------------- 已有文稿：审阅 / 校对 ----------------

    def check_text(self, text: str, mode: str = "proofread", title: str = "",
                   source: str = "") -> CheckResult:
        """对**已有文稿**做审阅或校对。

        mode="review"     ：只提意见（事实/逻辑/结构），有事实类问题时顺带核查
        mode="proofread"  ：只挑错（错别字/标点/术语/语法/格式），并把修正落成 final.md

        与 write() 的区别：不写大纲、不重写全文——用户要的是"看看我这篇有什么问题"，
        不是"照这个主题另写一篇"。改动只以修正稿形式给出，是否覆盖原文件由调用方决定。
        """
        body = (text or "").strip()
        if not body:
            raise ArticleError("没有可检查的正文（text / file 都为空）")
        mode = (mode or "proofread").strip().lower()
        if mode not in ("review", "proofread"):
            raise ArticleError(f"未知检查模式 {mode!r}（review / proofread）")
        title = (title or "").strip() or "未命名文稿"

        self.out_dir = self._make_out_dir(title, suffix=mode)
        self._emit("article_start", {"topic": title, "out_dir": self.out_dir,
                                     "mode": mode, "models": stage_models(self.cfg)})
        if self.verbose:
            print(f"[文章工坊·{'审阅' if mode == 'review' else '校对'}] {title}"
                  f"（{len(body)} 字）")
            print(f"[文章工坊] 产物目录: {self.out_dir}")
        try:
            if mode == "review":
                raw = self._call("review", ARTICLE_REVIEW_SYSTEM_PROMPT,
                                 ARTICLE_REVIEW_USER_PROMPT_TEMPLATE.format(
                                     topic=title,
                                     requirements="（这是**已有文稿**：只指出问题、给修改建议，"
                                                  "不要代写或重写全文）",
                                     article=body))
                issues = parse_issues(raw)
                self._write("review.md", raw)
                self._write_json("review.json", [i.to_dict() for i in issues])
                checks: List[FactCheck] = []
                if self.cfg.get("factcheck", True) and issues:
                    checks = self._factcheck(title, body, issues)
                    if checks:
                        self._write("factcheck.md", "## 事实核查结论\n" + "\n".join(
                            f"- {fc.line()}" for fc in checks))
                fixed = ""
            else:
                raw = self._call("proofread", ARTICLE_PROOFREAD_SYSTEM_PROMPT,
                                 ARTICLE_PROOFREAD_USER_PROMPT_TEMPLATE.format(
                                     topic=title, article=body))
                issues = merge_issues(mechanical_issues(body), parse_issues(raw))
                self._write("proofread.md", raw)
                self._write_json("proofread.json", [i.to_dict() for i in issues])
                checks = []
                fixed = ""
                if issues:
                    fixed = self._call("finalize", ARTICLE_FINALIZE_SYSTEM_PROMPT,
                                       ARTICLE_FINALIZE_USER_PROMPT_TEMPLATE.format(
                                           topic=title, article=body,
                                           fixes=json.dumps(
                                               [i.to_dict() for i in issues],
                                               ensure_ascii=False, indent=2)))
                    self._write("final.md", fixed)
        except ArticleError as e:
            self._write_json("meta.json", {
                "title": title, "mode": mode, "source": source, "error": str(e),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stages": [s.to_dict() for s in self.stages], "warnings": self.warnings,
            })
            raise ArticleError(f"{e}；已完成的部分已保存在 {self.out_dir}") from e

        result = CheckResult(mode=mode, title=title, text=body, source=source,
                             fixed_text=fixed, issues=issues, fact_checks=checks,
                             stages=self.stages, out_dir=self.out_dir,
                             warnings=self.warnings)
        self._write_json("meta.json", {
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "models": stage_models(self.cfg), **result.to_dict(),
        })
        self._emit("article_done", {"mode": mode, "title": title,
                                    "out_dir": self.out_dir,
                                    "issues": result.issue_counts(),
                                    "verdicts": result.verdict_counts()})
        return result

    # ---------------- 事实核查 ----------------

    def _lookup_sources(self) -> List[str]:
        raw = str(self.cfg.get("lookup_sources", "zhihu,browser") or "")
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    def _lookup_one(self, source: str, query: str, limit: int) -> List[dict]:
        if source == "zhihu":
            return list(zhihu_lookup(query, limit) or [])
        if source == "browser":
            if self.tool_manager is None:
                return []
            from tools.research import DeepResearcher
            researcher = DeepResearcher(tool_manager=self.tool_manager, llm=None)
            return list(researcher.lookup_snippets(query, limit=limit) or [])
        self.warnings.append(f"未知的检索通道 {source!r}（可用 zhihu / browser）")
        return []

    def _lookup_hits(self, query: str, limit: int) -> List[dict]:
        """按配置顺序取检索资料，凑够 limit 条就停。

        多通道串联是必要的：知乎开放平台搜索稳定但需要配 secret，浏览器抓搜索
        结果页不依赖密钥但经常取不到东西——任何一条通道失败/为空都落到下一条，
        全都拿不到才记"未查到"。
        """
        if self.lookup is not None:             # 调用方注入（测试/自定义通道）
            try:
                return list(self.lookup(query, limit) or [])
            except Exception as e:              # noqa: BLE001
                self.warnings.append(f"检索「{query}」失败：{str(e)[:100]}")
                return []
        hits: List[dict] = []
        for source in (self._lookup_sources() or ["browser"]):
            if len(hits) >= limit:
                break
            try:
                hits.extend(self._lookup_one(source, query, limit - len(hits)))
            except Exception as e:              # noqa: BLE001
                self.warnings.append(
                    f"检索通道 {source} 失败（{query}）：{str(e)[:100]}")
        return [h for h in hits if isinstance(h, dict)][:limit]

    @staticmethod
    def _render_material(hits: List[dict]) -> str:
        if not hits:
            return "（未检索到资料）"
        parts = []
        for i, h in enumerate(hits, 1):
            title = str(h.get("title") or "").strip()
            url = str(h.get("url") or "").strip()
            body = _clip(str(h.get("snippet") or h.get("content") or ""), 800)
            parts.append(f"[资料{i}] {title}\nURL: {url}\n{body}")
        return "\n\n".join(parts)

    def _factcheck(self, topic: str, article: str, issues: List[ArticleIssue]) -> List[FactCheck]:
        """只核查审阅方点名的"事实"类问题。"""
        fact_issues = [i for i in issues if "事实" in i.kind or "事实" in i.problem]
        if not fact_issues:
            return []
        limit = int(self.cfg.get("factcheck_max_claims", 5) or 5)
        listed = "\n".join(f"- {i.quote}｜{i.problem}" for i in fact_issues if i.problem or i.quote)
        raw = self._call("claims", ARTICLE_CLAIMS_SYSTEM_PROMPT.replace(
            "{max_claims}", str(limit)),
            ARTICLE_CLAIMS_USER_PROMPT_TEMPLATE.format(
                topic=topic, issues=listed or "（见原文标记）",
                excerpt=_clip(article, 4000)))
        claims = parse_claims(raw, limit=limit)
        out: List[FactCheck] = []
        for c in claims:
            hits = self._lookup_hits(c.get("query") or c["claim"][:12],
                                     int(self.cfg.get("lookup_limit", 3) or 3))
            if not hits:
                # 没取到资料就不要让模型凭记忆下结论——直接记"未查到"，省一次调用
                out.append(FactCheck(claim=c["claim"], verdict="未查到",
                                     evidence="本轮未检索到可用资料",
                                     query=c.get("query", "")))
                continue
            raw_v = self._call("verify", ARTICLE_VERIFY_SYSTEM_PROMPT,
                               ARTICLE_VERIFY_USER_PROMPT_TEMPLATE.format(
                                   claim=c["claim"], material=self._render_material(hits)))
            out.append(parse_factcheck(raw_v, c["claim"], query=c.get("query", "")))
        return out

    # ---------------- 主流程 ----------------

    def write(self, topic: str, requirements: str = "", target_length: str = "",
              outline: str = "", max_rounds: Optional[int] = None) -> ArticleResult:
        """跑完整条流水线，返回 ArticleResult（产物已落盘）。

        任何阶段失败时**不丢已完成的成果**：把当前最好的一版写成 partial.md、
        元数据写成 meta.json，再抛错并告知目录——长文流水线跑一次要几分钟和
        好几次大模型调用，因为最后一步限流就全部作废是不可接受的。
        """
        try:
            return self._run(topic, requirements=requirements,
                             target_length=target_length, outline=outline,
                             max_rounds=max_rounds)
        except ArticleError as e:
            partial = (self._last_text.get("revise") or self._last_text.get("finalize")
                       or self._last_text.get("draft") or "")
            if self.out_dir:
                if partial:
                    self._write("partial.md", partial)
                self._write_json("meta.json", {
                    "topic": (topic or "").strip(), "error": str(e), "partial": True,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "models": stage_models(self.cfg),
                    "stages": [s.to_dict() for s in self.stages],
                    "warnings": self.warnings,
                })
                self._emit("article_failed", {"topic": (topic or "").strip(),
                                              "out_dir": self.out_dir, "error": str(e)})
            hint = "（partial.md 是当前最好的一版）" if partial else ""
            raise ArticleError(f"{e}；已完成的部分已保存在 {self.out_dir}{hint}") from e

    def _run(self, topic: str, requirements: str = "", target_length: str = "",
             outline: str = "", max_rounds: Optional[int] = None) -> ArticleResult:
        """流水线主体（失败处理见 write）。"""
        topic = (topic or "").strip()
        if not topic:
            raise ArticleError("主题不能为空")
        requirements = (requirements or "").strip() or "（无特别要求）"
        target = (target_length or self.cfg.get("target_length") or "").strip() or "由你按主题判断（1500-2500 字）"
        rounds_max = int(max_rounds if max_rounds is not None
                         else self.cfg.get("max_revise_rounds", 2))
        revise_sev = tuple(self.cfg.get("revise_severities") or ("严重", "中等"))
        do_factcheck = bool(self.cfg.get("factcheck", True))

        self.out_dir = self._make_out_dir(topic)
        self._emit("article_start", {"topic": topic, "out_dir": self.out_dir,
                                     "models": stage_models(self.cfg)})
        if self.verbose:
            print(f"[文章工坊] {topic}")
            print(f"[文章工坊] 产物目录: {self.out_dir}")
            for stage, tag in stage_models(self.cfg).items():
                print(f"  · {stage:9s} → {tag}")

        # 1) 大纲
        if not outline and self.cfg.get("outline", True):
            outline = self._call("outline", ARTICLE_OUTLINE_SYSTEM_PROMPT,
                                 ARTICLE_OUTLINE_USER_PROMPT_TEMPLATE.format(
                                     topic=topic, requirements=requirements,
                                     target_length=target))
            self._write("outline.md", outline)

        # 2) 初稿
        article = self._call("draft", ARTICLE_DRAFT_SYSTEM_PROMPT,
                             ARTICLE_DRAFT_USER_PROMPT_TEMPLATE.format(
                                 topic=topic, requirements=requirements,
                                 target_length=target,
                                 outline=outline or "（无大纲，直接写）"))
        self._write("draft.md", article)

        # 3) 审阅 → 事实核查 → 修订（有界循环）
        changes_log: List[str] = []
        issues_by_round: List[List[ArticleIssue]] = []
        fact_checks: List[FactCheck] = []
        done_rounds = 0
        for rnd in range(1, max(0, rounds_max) + 1):
            review_raw = self._call("review", ARTICLE_REVIEW_SYSTEM_PROMPT,
                                    ARTICLE_REVIEW_USER_PROMPT_TEMPLATE.format(
                                        topic=topic, requirements=requirements,
                                        article=article))
            issues = parse_issues(review_raw)
            issues_by_round.append(issues)
            done_rounds = rnd
            self._write(f"review-r{rnd}.md", review_raw)
            self._write_json(f"review-r{rnd}.json", [i.to_dict() for i in issues])

            blocking = [i for i in issues if i.severity in revise_sev]
            if not issues:
                if self.verbose:
                    print(f"  · 第 {rnd} 轮审阅未提出问题（或输出无法解析）")
                # 解析失败但原文很长 → 当成自由文本意见，仍然推给作者改一轮
                if len(review_raw) < 40:
                    break
                blocking = [ArticleIssue(severity="中等", kind="综合",
                                         problem=_clip(review_raw, 600),
                                         suggestion="按上述意见修改")]
            if not blocking:
                if self.verbose:
                    print(f"  · 第 {rnd} 轮无严重/中等问题 → 不再回炉")
                break

            round_checks: List[FactCheck] = []
            if do_factcheck:
                round_checks = self._factcheck(topic, article, blocking)
                fact_checks.extend(round_checks)
                if round_checks:
                    self._write(f"factcheck-r{rnd}.md",
                                "## 事实核查结论\n" + "\n".join(
                                    f"- {fc.line()}" for fc in round_checks))

            revised_raw = self._call(
                "revise", ARTICLE_REVISE_SYSTEM_PROMPT,
                ARTICLE_REVISE_USER_PROMPT_TEMPLATE.format(
                    topic=topic, requirements=requirements, article=article,
                    issues="\n".join(f"- {i.line()}" for i in blocking),
                    factchecks=("\n".join(f"- {fc.line()}" for fc in round_checks)
                                or "（本轮无核查结论）")))
            note, new_article = split_revision(revised_raw)
            self._write(f"revise-r{rnd}.md", f"## 修改说明\n{note}\n\n---\n\n{new_article}")
            changes_log.append(f"### 第 {rnd} 轮\n\n**审阅意见**\n\n"
                               + "\n".join(f"- {i.line()}" for i in blocking)
                               + "\n\n**事实核查**\n\n"
                               + ("\n".join(f"- {fc.line()}" for fc in round_checks) or "- （无）")
                               + f"\n\n**修改说明**\n\n{note}\n")
            if not new_article.strip():
                self.warnings.append(f"第 {rnd} 轮修订返回空正文，已沿用上一版")
                break
            if new_article.strip() == article.strip():
                if self.verbose:
                    print(f"  · 第 {rnd} 轮修订后正文无变化 → 停止回炉")
                article = new_article
                break
            article = new_article

        # 4) 校对 → 定稿
        proof_issues: List[ArticleIssue] = []
        proof_raw = self._call("proofread", ARTICLE_PROOFREAD_SYSTEM_PROMPT,
                               ARTICLE_PROOFREAD_USER_PROMPT_TEMPLATE.format(
                                   topic=topic, article=article))
        # 机械规则 + 模型意见：规则抓半角标点/引号/叠字这类"全文性"问题，模型抓语义
        proof_issues = merge_issues(mechanical_issues(article), parse_issues(proof_raw))
        self._write("proofread.md", proof_raw)
        self._write_json("proofread.json", [i.to_dict() for i in proof_issues])

        if proof_issues:
            final = self._call("finalize", ARTICLE_FINALIZE_SYSTEM_PROMPT,
                               ARTICLE_FINALIZE_USER_PROMPT_TEMPLATE.format(
                                   topic=topic, article=article,
                                   fixes=json.dumps([i.to_dict() for i in proof_issues],
                                                    ensure_ascii=False, indent=2)))
        else:
            final = article                       # 没有校对问题就不再多调一次模型

        self._write("final.md", final)
        changes = (f"# 修改台账：{topic}\n\n"
                   f"- 审阅轮数：{done_rounds}\n"
                   f"- 事实核查：{len(fact_checks)} 条\n"
                   f"- 校对问题：{len(proof_issues)} 条\n\n"
                   + ("\n".join(changes_log) if changes_log else "（未发生回炉修改）")
                   + "\n\n## 校对意见\n\n"
                   + ("\n".join(f"- {i.line()}" for i in proof_issues) or "- （无）"))
        self._write("changes.md", changes)

        result = ArticleResult(topic=topic, final_text=final, out_dir=self.out_dir,
                               outline=outline, draft=article, rounds=done_rounds,
                               issues_by_round=issues_by_round,
                               proofread_issues=proof_issues, fact_checks=fact_checks,
                               stages=self.stages, warnings=self.warnings)
        self._write_json("meta.json", {
            "topic": topic, "requirements": requirements, "target_length": target,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "max_revise_rounds": rounds_max,
            "revise_severities": list(revise_sev),
            "factcheck": do_factcheck,
            "models": stage_models(self.cfg),
            **result.to_dict(),
        })
        self._emit("article_done", {"topic": topic, "out_dir": self.out_dir,
                                    "rounds": done_rounds,
                                    "final_chars": len(final),
                                    "issues": result.issue_counts(),
                                    "verdicts": result.verdict_counts()})
        return result
