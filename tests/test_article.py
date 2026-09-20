# -*- coding: utf-8 -*-
"""文章工坊（多模型互审写作流水线）测试：全程离线（脚本化假模型）。

钉住的语义：
- 职责分离：审阅/校对只提问题，改写只由作者模型做（意见必须进 revise 的提示）；
- 跨厂商：默认 main 写、agnes 审；两者相同时必须给出明确警告（同源互审价值有限）；
- 有界回炉：无严重/中等意见即停、正文没变化即停、到上限即停；
- 事实核查只查被点名的可疑说法；取不到资料时**不让模型凭记忆下结论**（记"未查到"）；
- 格式容错：模型不按 JSON/分隔符输出时降级继续，绝不让流水线断在半路。
"""
import json
import os

import pytest

from config import ARTICLE_CONFIG
from models.article import (
    ArticleError,
    ArticlePipeline,
    FactCheck,
    parse_claims,
    parse_factcheck,
    parse_issues,
    slugify,
    split_revision,
    stage_models,
)

# ---------------------------------------------------------------- 假模型

class FakeStageLLM:
    """脚本化假模型：按顺序返回；异常对象直接抛；记录每次调用的提示。"""

    def __init__(self, script, label="fake"):
        self.script = list(script)
        self.label = label
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.script:
            return "（脚本耗尽）"
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # 便于断言"提示里带了什么"
    def text(self) -> str:
        return "\n".join(m["content"] for c in self.calls for m in c["messages"])


DRAFT = "# 初稿\n\nAI Agent 的记忆机制决定它能不能长期干活。[待核实: 2024年市场规模为100亿元]\n"
REVISED = "# 修订稿\n\nAI Agent 的记忆机制决定它能否长期干活。\n"
FINAL = "# 定稿\n\nAI Agent 的记忆机制决定它能否长期干活。\n"

REVIEW_ONE_FACT = json.dumps([{
    "severity": "严重", "kind": "事实", "quote": "2024年市场规模为100亿元",
    "problem": "该数据来源不明且与公开数据矛盾", "suggestion": "核实后改为准确表述或删除",
}], ensure_ascii=False)

REVIEW_EMPTY = "[]"
PROOFREAD_ONE = json.dumps([{
    "severity": "轻微", "kind": "标点", "quote": "能否长期干活。",
    "problem": "无需修改，示例", "fix": "保持",
}], ensure_ascii=False)


def _cfg(tmp_path, **over):
    """基于真实配置做测试覆盖（只改落盘目录与个别旋钮）。"""
    cfg = dict(ARTICLE_CONFIG)
    cfg["save_dir"] = str(tmp_path / "articles")
    cfg["stages"] = dict(ARTICLE_CONFIG["stages"])
    cfg["stages"].update(over.pop("stages", {}))
    cfg.update(over)
    return cfg


def _pipeline(tmp_path, scripts, lookup=None, **over):
    llms = {stage: FakeStageLLM(script, label=stage) for stage, script in scripts.items()}
    pipe = ArticlePipeline(llms=llms, config=_cfg(tmp_path, **over),
                           lookup=lookup, verbose=False)
    return pipe, llms


def _happy_scripts(**extra):
    scripts = {
        "outline": ["# 大纲\n1. 记忆机制是什么\n2. 为什么重要"],
        "draft": [DRAFT],
        "review": [REVIEW_ONE_FACT, REVIEW_EMPTY],
        "claims": [json.dumps([{"claim": "2024年市场规模为100亿元",
                                "query": "AI Agent 市场规模"}], ensure_ascii=False)],
        "verify": [json.dumps({"claim": "2024年市场规模为100亿元", "verdict": "证伪",
                               "evidence": "公开资料显示为 50 亿元", "source": "https://example.com/a"},
                              ensure_ascii=False)],
        "revise": ["## 修改说明\n- [严重·事实] 已删除不实数据\n\n<<<ARTICLE\n" + REVISED + "\nARTICLE"],
        "proofread": [PROOFREAD_ONE],
        "finalize": [FINAL],
    }
    scripts.update(extra)
    return scripts


def _hits(query, limit):
    return [{"title": "市场规模报告", "url": "https://example.com/a",
             "snippet": "2024 年 AI Agent 市场规模约 50 亿元。"}]


# ---------------------------------------------------------------- 解析器

class TestParsers:
    def test_issues_from_plain_json(self):
        issues = parse_issues(REVIEW_ONE_FACT)
        assert len(issues) == 1
        assert issues[0].severity == "严重" and issues[0].kind == "事实"
        assert "100亿元" in issues[0].quote

    def test_issues_from_fenced_json(self):
        raw = "这是审阅结果：\n```json\n" + REVIEW_ONE_FACT + "\n```\n以上。"
        assert len(parse_issues(raw)) == 1, "模型爱加围栏和废话，必须能剥出来"

    def test_issues_wrapped_in_object(self):
        raw = json.dumps({"issues": json.loads(REVIEW_ONE_FACT)}, ensure_ascii=False)
        assert len(parse_issues(raw)) == 1

    def test_english_severity_is_normalized(self):
        raw = json.dumps([{"severity": "critical", "kind": "fact",
                           "problem": "p", "suggestion": "s"}])
        assert parse_issues(raw)[0].severity == "严重"

    def test_non_json_returns_empty(self):
        assert parse_issues("这篇文章整体不错，没什么问题。") == []

    def test_claims_fallback_to_lines(self):
        claims = parse_claims("2024年市场规模为100亿元\n用户增长了三倍", limit=5)
        assert len(claims) == 2 and claims[0]["claim"].startswith("2024年")

    def test_claims_from_json(self):
        claims = parse_claims(json.dumps([{"claim": "A 成立于 2015 年", "query": "A 成立"}],
                                         ensure_ascii=False))
        assert claims == [{"claim": "A 成立于 2015 年", "query": "A 成立"}]

    def test_factcheck_parse_and_default(self):
        ok = parse_factcheck('{"claim": "x", "verdict": "证实", "evidence": "e", "source": "s"}', "x")
        assert (ok.verdict, ok.evidence) == ("证实", "e")
        bad = parse_factcheck("我觉得应该是对的", "x")
        assert bad.verdict == "未查到", "解析不出来时保守记未查到，不能当成证实"

    def test_split_revision_with_delimiters(self):
        note, body = split_revision("## 修改说明\n- 改了 A\n\n<<<ARTICLE\n正文内容\nARTICLE")
        assert "改了 A" in note and body == "正文内容"

    def test_split_revision_closing_variant(self):
        note, body = split_revision("说明\n<<<ARTICLE\n正文内容\nARTICLE>>>")
        assert body == "正文内容"

    def test_split_revision_without_delimiter_keeps_text(self):
        note, body = split_revision("直接给了一版新正文，没有分隔符")
        assert "分隔符" in note and body == "直接给了一版新正文，没有分隔符"

    def test_slugify(self):
        assert slugify("AI/Agent: 记忆 机制?") == "AI-Agent-记忆-机制"
        assert slugify("") == "article"

    def test_stage_models_labels(self):
        labels = stage_models(_cfg(__import__("pathlib").Path(".")))
        assert labels["draft"].startswith("main:")
        assert labels["review"].startswith("agnes:"), "默认必须跨厂商：另一家来审"


# ---------------------------------------------------------------- 主流程

class TestPipeline:
    def test_full_run_artifacts_and_rounds(self, tmp_path):
        pipe, llms = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)
        result = pipe.write("AI Agent 的记忆机制", requirements="给技术读者，1500 字")

        assert result.rounds == 2, "第一轮有严重问题→修订；第二轮无严重/中等问题→停"
        assert result.final_text.strip() == FINAL.strip()
        assert [fc.verdict for fc in result.fact_checks] == ["证伪"]

        for name in ("outline.md", "draft.md", "review-r1.md", "review-r1.json",
                     "factcheck-r1.md", "revise-r1.md", "review-r2.md",
                     "proofread.md", "proofread.json", "final.md",
                     "changes.md", "meta.json"):
            assert os.path.exists(os.path.join(result.out_dir, name)), f"缺产物 {name}"

        with open(os.path.join(result.out_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["models"]["review"].startswith("agnes")
        assert meta["topic"] == "AI Agent 的记忆机制"

    def test_review_issues_are_fed_to_the_author(self, tmp_path):
        """职责分离：审阅提的问题必须原样进作者（revise）的提示，否则改稿无从谈起。"""
        pipe, llms = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)
        pipe.write("主题")
        assert "该数据来源不明且与公开数据矛盾" in llms["revise"].text()

    def test_stage_models_are_recorded_per_stage(self, tmp_path):
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)
        result = pipe.write("主题")
        by_stage = {s.stage: s for s in result.stages}
        assert by_stage["draft"].endpoint == "main"
        assert by_stage["review"].endpoint == "agnes"
        assert by_stage["proofread"].endpoint == "agnes"
        assert by_stage["verify"].endpoint == "main"

    def test_no_issues_stops_after_first_review(self, tmp_path):
        scripts = _happy_scripts(review=[REVIEW_EMPTY])
        pipe, llms = _pipeline(tmp_path, scripts, lookup=_hits)
        result = pipe.write("主题")
        assert result.rounds == 1
        assert "revise" not in {s.stage for s in result.stages}, "没有意见就不该回炉"
        assert result.final_text.strip() == FINAL.strip()

    def test_identical_revision_stops_the_loop(self, tmp_path):
        """修订后正文没变化 → 立刻停（继续问下去只是烧 token）。"""
        same = "## 修改说明\n- 无需修改\n\n<<<ARTICLE\n" + DRAFT + "\nARTICLE"
        scripts = _happy_scripts(review=[REVIEW_ONE_FACT, REVIEW_ONE_FACT], revise=[same])
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits)
        result = pipe.write("主题")
        assert result.rounds == 1, "第 1 轮就发现正文没变，不应再跑第 2 轮"

    def test_loop_is_bounded_by_max_rounds(self, tmp_path):
        always = "## 修改说明\n- 又改了一版\n\n<<<ARTICLE\n" + REVISED + "\nARTICLE"
        scripts = _happy_scripts(review=[REVIEW_ONE_FACT] * 5, revise=[always] * 5)
        pipe, llms = _pipeline(tmp_path, scripts, lookup=_hits, max_revise_rounds=2)
        result = pipe.write("主题")
        assert result.rounds == 2, "上限就是上限，不能无限互审"
        assert len(llms["revise"].calls) == 2

    def test_same_stage_text_twice_does_not_duplicate_rounds(self, tmp_path):
        """第二轮修订内容与第一轮不同 → 允许继续，但受上限约束。"""
        v1 = "## 修改说明\n- v1\n\n<<<ARTICLE\n第一版修订\nARTICLE"
        v2 = "## 修改说明\n- v2\n\n<<<ARTICLE\n第二版修订\nARTICLE"
        scripts = _happy_scripts(review=[REVIEW_ONE_FACT, REVIEW_ONE_FACT], revise=[v1, v2])
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits, max_revise_rounds=2)
        result = pipe.write("主题")
        assert result.rounds == 2
        assert "第二版修订" in result.final_text or "第二版修订" in result.draft

    def test_factcheck_only_for_flagged_facts(self, tmp_path):
        logic_only = json.dumps([{"severity": "中等", "kind": "逻辑",
                                  "quote": "q", "problem": "论证跳跃", "suggestion": "补论据"}],
                                ensure_ascii=False)
        calls = []

        def lookup(q, limit):
            calls.append(q)
            return _hits(q, limit)

        scripts = _happy_scripts(review=[logic_only, REVIEW_EMPTY])
        scripts.pop("claims")
        scripts.pop("verify")
        pipe, llms = _pipeline(tmp_path, scripts, lookup=lookup)
        result = pipe.write("主题")
        assert calls == [], "没点名事实问题就不该联网"
        assert "claims" not in {s.stage for s in result.stages}
        assert result.fact_checks == []
        assert "revise" in {s.stage for s in result.stages}, "逻辑问题仍要回炉"

    def test_factcheck_records_unverified_when_no_material(self, tmp_path):
        """取不到资料时不许模型凭记忆下结论：直接记未查到，也不再调 verify。"""
        scripts = _happy_scripts()
        scripts.pop("verify")
        pipe, llms = _pipeline(tmp_path, scripts, lookup=lambda q, n: [])
        result = pipe.write("主题")
        assert [fc.verdict for fc in result.fact_checks] == ["未查到"]
        assert "verify" not in {s.stage for s in result.stages}

    def test_lookup_failure_does_not_break_pipeline(self, tmp_path):
        def boom(q, n):
            raise RuntimeError("浏览器挂了")

        scripts = _happy_scripts()
        scripts.pop("verify")
        pipe, _ = _pipeline(tmp_path, scripts, lookup=boom)
        result = pipe.write("主题")
        assert result.final_text.strip() == FINAL.strip()
        assert any("检索" in w for w in result.warnings)

    def test_lookup_chain_falls_through_to_next_source(self, tmp_path, monkeypatch):
        """多通道串联：知乎拿不到就落到浏览器，拿到的资料要真的进核查提示。"""
        import models.article as am

        calls = []

        def fake_lookup_one(self, source, query, limit):
            calls.append(source)
            if source == "zhihu":
                return []
            return [{"title": "备选来源", "url": "https://b.example", "snippet": "资料正文"}]

        monkeypatch.setattr(am.ArticlePipeline, "_lookup_one", fake_lookup_one)
        pipe, llms = _pipeline(tmp_path, _happy_scripts())
        result = pipe.write("主题")
        assert calls == ["zhihu", "browser"], "知乎为空应落到浏览器"
        assert "备选来源" in llms["verify"].text()
        assert [fc.verdict for fc in result.fact_checks] == ["证伪"]

    def test_lookup_source_stops_when_enough(self, tmp_path, monkeypatch):
        """第一条通道就凑够条数时，不该再去调第二条（省时间也省配额）。"""
        import models.article as am

        calls = []

        def fake_lookup_one(self, source, query, limit):
            calls.append(source)
            return [{"title": "T", "url": "U", "snippet": "S"}]

        monkeypatch.setattr(am.ArticlePipeline, "_lookup_one", fake_lookup_one)
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup_limit=1)
        pipe.write("主题")
        assert calls == ["zhihu"], "够 1 条就停"

    def test_lookup_sources_config_parsing(self, tmp_path):
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup_sources=" browser , zhihu ,")
        assert pipe._lookup_sources() == ["browser", "zhihu"]
        pipe2, _ = _pipeline(tmp_path, _happy_scripts(), lookup_sources="")
        assert pipe2._lookup_sources() == [], "空配置时调用方会退回默认通道"

    def test_no_proofread_issues_skips_finalize(self, tmp_path):
        scripts = _happy_scripts(proofread=["[]"])
        scripts.pop("finalize")
        pipe, llms = _pipeline(tmp_path, scripts, lookup=_hits)
        result = pipe.write("主题")
        assert result.proofread_issues == []
        assert "finalize" not in {s.stage for s in result.stages}, "没错就不用再多调一次模型"
        assert result.final_text.strip() == REVISED.strip()

    def test_unparseable_review_still_triggers_one_revision(self, tmp_path):
        """审阅方没按 JSON 输出：把整段当自由意见推给作者改一轮，不要直接放弃。"""
        prose = ("这篇文章的问题在于：第一段的事实没有任何来源；"
                 "第二段的论证缺少中间环节，读者会跟不上；结尾下的结论过强。")
        scripts = _happy_scripts(review=[prose, REVIEW_EMPTY])
        pipe, llms = _pipeline(tmp_path, scripts, lookup=_hits)
        result = pipe.write("主题")
        assert result.rounds == 2
        assert "缺少中间环节" in llms["revise"].text()

    def test_outline_can_be_skipped(self, tmp_path):
        scripts = _happy_scripts()
        scripts.pop("outline")
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits, outline=False)
        result = pipe.write("主题")
        assert "outline" not in {s.stage for s in result.stages}

    def test_given_outline_is_used_as_is(self, tmp_path):
        scripts = _happy_scripts()
        scripts.pop("outline")
        pipe, llms = _pipeline(tmp_path, scripts, lookup=_hits)
        result = pipe.write("主题", outline="# 我的大纲\n- 只讲一件事")
        assert "outline" not in {s.stage for s in result.stages}
        assert "只讲一件事" in llms["draft"].text()

    def test_empty_topic_rejected(self, tmp_path):
        pipe, _ = _pipeline(tmp_path, _happy_scripts())
        with pytest.raises(ArticleError):
            pipe.write("   ")

    def test_stage_failure_names_the_stage(self, tmp_path):
        scripts = _happy_scripts(draft=[RuntimeError("上游 500")])
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits)
        with pytest.raises(ArticleError) as ei:
            pipe.write("主题")
        assert "draft" in str(ei.value) and "500" in str(ei.value)

    def test_same_endpoint_warns_about_weak_cross_review(self, tmp_path):
        """写和审是同一个模型时必须明说——这正是"互审退化成自我复述"的根源。"""
        scripts = _happy_scripts()
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits,
                            stages={"review": "main", "proofread": "main"})
        result = pipe.write("主题")
        assert any("同一个端点" in w for w in result.warnings)

    def test_changes_log_lists_issues_and_verdicts(self, tmp_path):
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)
        result = pipe.write("主题")
        with open(os.path.join(result.out_dir, "changes.md"), encoding="utf-8") as f:
            log = f.read()
        assert "该数据来源不明" in log, "修改台账要能追溯到每条意见"
        assert "证伪" in log

    def test_factcheck_can_be_disabled(self, tmp_path):
        scripts = _happy_scripts()
        scripts.pop("claims")
        scripts.pop("verify")
        pipe, _ = _pipeline(tmp_path, scripts, lookup=_hits, factcheck=False)
        result = pipe.write("主题")
        assert result.fact_checks == []
        assert result.final_text.strip() == FINAL.strip()


class TestRateLimitResilience:
    """上游限流是真实硬约束（实测商汤在 revise 阶段 429）：退避重试 → 换端点 → 留成果。"""

    _RL = "阶段「revise」调用失败（main:m）：Error code: 429 - insufficient_quota"

    def _pipe(self, tmp_path, scripts, **over):
        cfg = _cfg(tmp_path, **over)
        cfg["retry_wait"] = over.get("retry_wait", 0)      # 测试不真的等
        cfg["stage_retries"] = over.get("stage_retries", 1)
        cfg["fallback_endpoint"] = over.get("fallback_endpoint", "auto")
        llms = {k: FakeStageLLM(v, label=k) for k, v in scripts.items()}
        return ArticlePipeline(llms=llms, config=cfg, lookup=_hits, verbose=False), llms

    def test_same_endpoint_retry_then_success(self, tmp_path):
        """同端点重试一次就成功：不该换端点，也不该报错。"""
        scripts = _happy_scripts(revise=[RuntimeError(self._RL),
                                         "## 修改说明\n- 重试成功\n\n<<<ARTICLE\n" + REVISED + "\nARTICLE"])
        pipe, llms = self._pipe(tmp_path, scripts)
        result = pipe.write("主题")
        assert result.final_text.strip() == FINAL.strip()
        assert len(llms["revise"].calls) == 2, "应重试一次"
        assert not any("换用" in w for w in result.warnings)

    def test_falls_back_to_the_other_endpoint(self, tmp_path):
        """同端点重试仍失败 → 换另一家端点把这一步跑完，并明确记一条警告。"""
        scripts = _happy_scripts(revise=[RuntimeError(self._RL), RuntimeError(self._RL)])
        scripts["revise@agnes"] = ["## 修改说明\n- 由 agnes 接手\n\n<<<ARTICLE\n" + REVISED + "\nARTICLE"]
        pipe, _ = self._pipe(tmp_path, scripts)
        result = pipe.write("主题")
        assert result.final_text.strip() == FINAL.strip()
        revise_stages = [s for s in result.stages if s.stage == "revise"]
        assert [s.endpoint for s in revise_stages] == ["agnes"], "换端点后要如实记录是谁干的"
        assert any("换用 agnes" in w for w in result.warnings)

    def test_non_rate_limit_error_does_not_fall_back(self, tmp_path):
        """非限流错误（如 400/500）换端点也没用：立刻失败，保住钱和时间。"""
        scripts = _happy_scripts(revise=[RuntimeError("Error code: 400 - bad request")])
        scripts["revise@agnes"] = ["不该被调用"]
        pipe, _ = self._pipe(tmp_path, scripts)
        with pytest.raises(ArticleError) as ei:
            pipe.write("主题")
        assert "400" in str(ei.value)
        assert "agnes" not in str(ei.value)

    def test_failure_keeps_partial_artifacts(self, tmp_path):
        """最后一步失败也要留住成果：partial.md + meta.json(error)，报错里给目录。"""
        scripts = _happy_scripts(revise=[RuntimeError("Error code: 400 - bad request")])
        pipe, _ = self._pipe(tmp_path, scripts)
        with pytest.raises(ArticleError) as ei:
            pipe.write("主题")
        assert "partial.md" in str(ei.value)
        out_dir = pipe.out_dir
        assert os.path.exists(os.path.join(out_dir, "partial.md"))
        assert os.path.exists(os.path.join(out_dir, "draft.md"))
        with open(os.path.join(out_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["partial"] is True and "400" in meta["error"]

    def test_fallback_can_be_disabled(self, tmp_path):
        scripts = _happy_scripts(revise=[RuntimeError(self._RL), RuntimeError(self._RL)])
        scripts["revise@agnes"] = ["不该被调用"]
        pipe, _ = self._pipe(tmp_path, scripts, fallback_endpoint="off")
        with pytest.raises(ArticleError):
            pipe.write("主题")

    def test_rate_limit_detection_covers_vendor_wording(self):
        from models.article import _looks_rate_limited

        for msg in ("Error code: 429 - rate_limit_error", "inference exceeds tpm/rpm limit",
                    "insufficient_quota", "上游配额已满", "Too Many Requests"):
            assert _looks_rate_limited(msg), msg
        assert not _looks_rate_limited("Error code: 400 - invalid api key")
        assert not _looks_rate_limited("阶段「draft」返回空内容")

    def test_empty_response_also_falls_back(self, tmp_path):
        """配额耗尽的端点除了 429 还会回**空正文**（实测商汤就是这样）：
        这种"看着像成功、其实什么都没有"的失败也必须走换端点，否则白等一场。"""
        scripts = _happy_scripts(revise=["", ""])
        scripts["revise@agnes"] = ["## 修改说明\n- agnes 接手\n\n<<<ARTICLE\n" + REVISED + "\nARTICLE"]
        pipe, _ = self._pipe(tmp_path, scripts)
        result = pipe.write("主题")
        assert result.final_text.strip() == FINAL.strip()
        assert any("换用 agnes" in w for w in result.warnings)

    def test_transient_detection(self):
        from models.article import _is_transient_error

        assert _is_transient_error("阶段「outline」返回空内容（main:m）")
        assert _is_transient_error("Error code: 503 - service unavailable")
        assert not _is_transient_error("Error code: 401 - invalid api key")
        assert not _is_transient_error("Error code: 400 - bad request")


class TestZhihuLookup:
    """知乎开放平台全网搜索作为核查通道：结构化 Items → (title,url,snippet)。"""

    def test_parses_items(self, monkeypatch):
        import models.article as am
        import models.zhihu as zh

        class _FakeClient:
            configured = True

            def web_search(self, query, count=10, **kw):
                assert count >= 1
                return {"Items": [
                    {"Title": "标题一", "Url": "https://z.example/1", "ContentText": "正文一"},
                    {"Title": "标题二", "Url": "https://z.example/2", "ContentText": "正文二"},
                ]}

        monkeypatch.setattr(zh, "ZhihuClient", _FakeClient)
        hits = am.zhihu_lookup("查询词", limit=1)
        assert hits == [{"title": "标题一", "url": "https://z.example/1", "snippet": "正文一"}]

    def test_unconfigured_returns_empty(self, monkeypatch):
        import models.article as am
        import models.zhihu as zh

        class _Off:
            configured = False

        monkeypatch.setattr(zh, "ZhihuClient", _Off)
        assert am.zhihu_lookup("查询词") == [], "没配 secret 就该空手而归，交给下一个通道"


# ---------------------------------------------------------------- 事件/摘要

class TestReporting:
    def test_events_and_summary(self, tmp_path):
        events = []
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)
        pipe.emit = lambda e, d: events.append((e, d))
        result = pipe.write("主题")
        kinds = [e for e, _ in events]
        assert kinds[0] == "article_start" and kinds[-1] == "article_done"
        assert kinds.count("article_stage") == len(result.stages)
        text = result.summary()
        assert "文章已定稿" in text and "事实核查" in text and "阶段模型" in text

    def test_emit_failure_is_swallowed(self, tmp_path):
        pipe, _ = _pipeline(tmp_path, _happy_scripts(), lookup=_hits)

        def boom(e, d):
            raise RuntimeError("dashboard 挂了")

        pipe.emit = boom
        result = pipe.write("主题")
        assert result.final_text.strip() == FINAL.strip(), "事件回调坏掉不能影响写作"

    def test_factcheck_line_contains_verdict(self):
        assert "证伪" in FactCheck(claim="x", verdict="证伪", evidence="e").line()
