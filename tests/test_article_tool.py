# -*- coding: utf-8 -*-
"""ArticleTool 测试：命令协议、产物摘要、错误处理、事件接线（离线，桩掉流水线）。"""
from pathlib import Path

import pytest

import models.article as article_mod
from tools.article import ArticleTool


class _StubResult:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.final_text = "# 定稿\n\n正文内容。\n"
        self.rounds = 2

    def summary(self):
        return f"文章已定稿：测试主题\n产物目录：{self.out_dir}"


class _StubPipeline:
    """记录调用参数的假流水线。"""

    last = {}

    def __init__(self, tool_manager=None, emit=None, verbose=True, **kw):
        _StubPipeline.last = {"tool_manager": tool_manager, "emit": emit,
                              "verbose": verbose}

    def write(self, topic, requirements="", target_length="", max_rounds=None):
        _StubPipeline.last.update({"topic": topic, "requirements": requirements,
                                   "target_length": target_length,
                                   "max_rounds": max_rounds})
        return _StubResult("/tmp/articles/demo")


@pytest.fixture()
def tool(monkeypatch):
    monkeypatch.setattr(article_mod, "ArticlePipeline", _StubPipeline)
    return ArticleTool(tool_manager="TM")


class TestMetadata:
    def test_tool_metadata(self):
        t = ArticleTool()
        assert t.name == "article"
        assert t.risk_level == "medium", "会写文件 + 多次外部模型调用"
        assert t.min_sandbox_mode == "workspace-write"
        assert t.parallel_safe is False, "内部多阶段串行，并行只会互相抢配额"
        assert "write" in t.schema["properties"]["operation"]["enum"]

    def test_description_mentions_cross_model_review(self):
        assert "另一个" in ArticleTool().description or "互审" in ArticleTool().description


class TestCommands:
    def test_help(self):
        out = ArticleTool().execute_json({"operation": "help"})
        assert out.success and "article write" in out.output

    def test_empty_text_input_shows_help(self):
        out = ArticleTool().execute("")
        assert out.success and "文章工坊" in out.output

    def test_models_lists_stages(self, monkeypatch):
        monkeypatch.setattr(article_mod, "stage_models",
                            lambda cfg=None: {"draft": "main:m", "review": "agnes:a",
                                              "proofread": "agnes:a"})
        out = ArticleTool().execute_json({"operation": "models"})
        assert out.success
        assert "draft" in out.output and "agnes:a" in out.output
        assert "⚠" not in out.output, "跨厂商时不该报警"

    def test_models_warns_when_same_endpoint(self, monkeypatch):
        monkeypatch.setattr(article_mod, "stage_models",
                            lambda cfg=None: {"draft": "main:m", "review": "main:m",
                                              "proofread": "main:m"})
        out = ArticleTool().execute_json({"operation": "models"})
        assert "⚠" in out.output and "另一个模型" in out.output or "另一家" in out.output

    def test_write_happy_path(self, tool):
        out = tool.execute_json({"operation": "write", "topic": "测试主题",
                                 "requirements": "给技术读者"})
        assert out.success
        assert "文章已定稿" in out.output and "定稿预览" in out.output
        assert _StubPipeline.last["topic"] == "测试主题"
        assert _StubPipeline.last["requirements"] == "给技术读者"
        assert _StubPipeline.last["verbose"] is False, "工具内不要刷屏（CLI 才 verbose）"
        assert _StubPipeline.last["tool_manager"] == "TM", "事实核查要复用同一个工具管理器"

    def test_text_protocol_splits_requirements(self, tool):
        out = tool.execute("write 选题名字 | 面向大众，800字")
        assert out.success
        assert _StubPipeline.last["topic"] == "选题名字"
        assert _StubPipeline.last["requirements"] == "面向大众，800字"

    def test_text_protocol_without_write_keyword(self, tool):
        out = tool.execute("直接给主题")
        assert out.success and _StubPipeline.last["topic"] == "直接给主题"

    def test_write_requires_topic(self, tool):
        out = tool.execute_json({"operation": "write", "topic": "  "})
        assert not out.success and "topic" in out.error

    def test_unknown_operation(self, tool):
        out = tool.execute_json({"operation": "publish"})
        assert not out.success and "publish" in out.error

    def test_write_without_topic_in_text_protocol(self, tool):
        out = tool.execute("write")
        assert not out.success and "用法" in out.error


class _StubCheck:
    def __init__(self, mode, title, fixed_text=""):
        self.mode = mode
        self.title = title
        self.fixed_text = fixed_text
        self.out_dir = "/tmp/articles/demo"
        self.issues = [article_mod.ArticleIssue(severity="中等", kind="标点",
                                                quote="很重要.", problem="用了英文句点",
                                                suggestion="改成中文句号")]

    def summary(self):
        return f"{'审阅' if self.mode == 'review' else '校对'}完成：{self.title}"


class _StubCheckPipeline:
    last = {}

    def __init__(self, tool_manager=None, emit=None, verbose=True, **kw):
        _StubCheckPipeline.last = {"tool_manager": tool_manager, "verbose": verbose}

    def check_text(self, text, mode="proofread", title="", source=""):
        _StubCheckPipeline.last.update({"text": text, "mode": mode,
                                        "title": title, "source": source})
        fixed = "修正后的正文\n" if mode == "proofread" else ""
        return _StubCheck(mode, title, fixed)


class TestCheckCommands:
    """用户说"校对/审阅"时模型会调这里：必须支持**已有文稿**（file 或 text）。"""

    @pytest.fixture()
    def tool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(article_mod, "ArticlePipeline", _StubCheckPipeline)
        return ArticleTool(tool_manager="TM")

    def test_description_mentions_trigger_words(self):
        """工具描述里必须列出"校对/审阅/挑错/润色"这些说法，模型才知道该调它。"""
        d = ArticleTool().description
        for word in ("校对", "审阅", "挑错", "润色", "错别字"):
            assert word in d, f"描述里缺触发词 {word}"
        assert "review" in ArticleTool().schema["properties"]["operation"]["enum"]
        assert "proofread" in ArticleTool().schema["properties"]["operation"]["enum"]

    def test_proofread_from_file(self, tool, tmp_path):
        f = tmp_path / "文稿.md"
        f.write_text("# 标题\n\n正文内容.", encoding="utf-8")
        out = tool.execute_json({"operation": "proofread", "file": str(f)})
        assert out.success
        assert "校对完成" in out.output and "问题清单" in out.output
        assert _StubCheckPipeline.last["text"].startswith("# 标题")
        assert _StubCheckPipeline.last["title"] == "文稿", "标题默认取文件名"
        assert _StubCheckPipeline.last["source"] == str(f)

    def test_review_from_inline_text(self, tool):
        out = tool.execute_json({"operation": "review", "text": "一段待审的文字。",
                                 "title": "随笔"})
        assert out.success and _StubCheckPipeline.last["mode"] == "review"
        assert _StubCheckPipeline.last["title"] == "随笔"

    def test_missing_file_is_reported(self, tool):
        out = tool.execute_json({"operation": "proofread", "file": "不存在的文件.md"})
        assert not out.success and "找不到文件" in out.error

    def test_missing_input_is_reported(self, tool):
        out = tool.execute_json({"operation": "review"})
        assert not out.success and "file" in out.error and "text" in out.error

    def test_empty_file_is_reported(self, tool, tmp_path):
        f = tmp_path / "空.md"
        f.write_text("   \n", encoding="utf-8")
        out = tool.execute_json({"operation": "proofread", "file": str(f)})
        assert not out.success and "空" in out.error

    def test_apply_writes_back_with_backup(self, tool, tmp_path):
        f = tmp_path / "文稿.md"
        original = "# 标题\n\n正文内容.\n"
        f.write_text(original, encoding="utf-8")
        out = tool.execute_json({"operation": "proofread", "file": str(f), "apply": True})
        assert out.success and "已写回原文件" in out.output
        assert f.read_text(encoding="utf-8").startswith("修正后的正文")
        assert (tmp_path / "文稿.md.bak").read_text(encoding="utf-8") == original, \
            "改写用户文件前必须留备份"

    def test_default_does_not_touch_original(self, tool, tmp_path):
        f = tmp_path / "文稿.md"
        original = "# 标题\n\n正文内容.\n"
        f.write_text(original, encoding="utf-8")
        out = tool.execute_json({"operation": "proofread", "file": str(f)})
        assert out.success and "原文件未改动" in out.output
        assert f.read_text(encoding="utf-8") == original

    def test_text_protocol_variants(self, tool, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("正文。", encoding="utf-8")
        for cmd in (f"proofread {f}", f"校对 {f}", f"review {f}", f"审阅 {f}"):
            out = tool.execute(cmd)
            assert out.success, cmd
        assert tool.execute("proofread").success is False


class TestErrorHandling:
    @pytest.fixture()
    def tool(self, monkeypatch):
        monkeypatch.setattr(article_mod, "ArticlePipeline", _StubPipeline)
        return ArticleTool(tool_manager="TM")

    def test_article_error_is_reported(self, tool, monkeypatch):
        class _Boom(_StubPipeline):
            def write(self, *a, **kw):
                raise article_mod.ArticleError("阶段「draft」调用失败（main:m）：上游 500")

        monkeypatch.setattr(article_mod, "ArticlePipeline", _Boom)
        out = tool.execute_json({"operation": "write", "topic": "T"})
        assert not out.success and "draft" in out.error

    def test_unexpected_exception_is_reported_not_raised(self, tool, monkeypatch):
        class _Boom(_StubPipeline):
            def write(self, *a, **kw):
                raise ValueError("意料之外")

        monkeypatch.setattr(article_mod, "ArticlePipeline", _Boom)
        out = tool.execute_json({"operation": "write", "topic": "T"})
        assert not out.success and "ValueError" in out.error


class TestRouting:
    """用户只说"校对"时，模型能不能自己想到用这个工具。

    真正的路由信号是**工具描述里的触发词**（随产品提交，任何机器都生效）；
    本机还额外装了 skills/article-check 技能包做二次强化（skills/ 不入库，
    所以在没有该技能包的机器上这条自动跳过）。
    """

    def test_description_tells_the_model_when_to_use_it(self):
        d = ArticleTool().description
        assert "校对" in d and "审阅" in d and "review" in d and "proofread" in d

    def test_local_skill_routes_proofread_wording(self):
        import pytest

        from agent.skills import SkillManager

        skills_dir = Path(__file__).resolve().parents[1] / "skills"
        if not (skills_dir / "article-check" / "SKILL.md").is_file():
            pytest.skip("本机未安装 article-check 技能包（skills/ 被 .gitignore 忽略）")
        mgr = SkillManager(project_dir=str(skills_dir),
                           user_dir=str(skills_dir / "_none"))
        for wording in ("帮我校对一下这篇文章", "看看有没有错别字", "帮我审阅这份报告",
                        "把这段文字润色一下", "proofread this draft"):
            hits = [s.name for s in mgr.match(wording)]
            assert "article-check" in hits, f"这句话没命中技能：{wording}"


class TestEmitWiring:
    def test_emit_callback_reaches_pipeline(self, tool):
        seen = []
        tool.set_emit(lambda e, d: seen.append((e, d)))
        tool.execute_json({"operation": "write", "topic": "T"})
        emit = _StubPipeline.last["emit"]
        assert callable(emit)
        emit("article_stage", {"stage": "draft"})       # 工具收到的回调被转给流水线
        assert seen == [("article_stage", {"stage": "draft"})]

    def test_broken_emit_callback_is_swallowed(self, tool):
        def boom(e, d):
            raise RuntimeError("前端挂了")

        tool.set_emit(boom)
        tool.execute_json({"operation": "write", "topic": "T"})
        _StubPipeline.last["emit"]("article_stage", {})   # 不抛异常
