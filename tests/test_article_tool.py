# -*- coding: utf-8 -*-
"""ArticleTool 测试：命令协议、产物摘要、错误处理、事件接线（离线，桩掉流水线）。"""
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


class TestErrorHandling:
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
