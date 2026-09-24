# -*- coding: utf-8 -*-
"""小快模型（杂活专用）测试 —— 跑腿的活不占用主模型。

关键不是"能不能起标题"，而是**坏了不能挡住主流程**：
小模型超时/报错/输出没法用时，一律退回原来的规则实现（第一条用户消息截断）。
"""
import json
import os
import shutil
import tempfile

import pytest

import config
from agent.small_model import SmallModel, build_small_llm
from agent.session import SessionStore



@pytest.fixture(autouse=True)
def _small_model_enabled(monkeypatch):
    """本文件专门验证小快模型 —— 覆盖 conftest 的默认关闭。

    `tests/conftest.py` 默认关掉它，否则 `build_small_llm` 会按
    SMALL_MODEL_CONFIG["model"] 建出**真实端点**的客户端，测试就会打外部 API
    （它被会话标题用到）。
    """
    from config import SMALL_MODEL_CONFIG
    monkeypatch.setitem(SMALL_MODEL_CONFIG, "enabled", True)

class FakeLLM:
    def __init__(self, out="海边五章连载", error=None):
        self.out = out
        self.error = error
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        if self.error:
            raise self.error
        return self.out


def _msgs(user="写一部五章连载短篇（每章 150 字以上，主题：程序员搬到海边小城）",
          assistant="第 1 章写好了。"):
    out = [{"role": "user", "content": user}]
    if assistant:
        out.append({"role": "assistant", "content": assistant})
    return out


class TestSmallModelBasics:
    def test_disabled_without_llm(self):
        s = SmallModel(llm=None)
        assert s.enabled is False
        assert s.title_for(_msgs()) == ""

    def test_disabled_by_config(self):
        s = SmallModel(llm=FakeLLM(), config={"enabled": False, "chores": "titles"})
        assert s.enabled is False and s.title_for(_msgs()) == ""

    def test_chore_can_be_off(self):
        s = SmallModel(llm=FakeLLM(), config={"enabled": True, "chores": ""})
        assert s.title_for(_msgs()) == "", "杂活没开就不该调用"

    def test_generates_title(self):
        llm = FakeLLM("海边小城连载")
        s = SmallModel(llm=llm, config={"enabled": True, "chores": "titles"})
        assert s.title_for(_msgs()) == "海边小城连载"
        assert llm.calls and llm.calls[0]["model"] is None      # 模型由 LLM 实例决定
        prompt = llm.calls[0]["messages"][-1]["content"]
        assert "写一部五章连载短篇" in prompt, "素材应包含用户的原始目标"

    def test_failure_is_fail_open(self):
        s = SmallModel(llm=FakeLLM(error=RuntimeError("小模型 500")),
                       config={"enabled": True, "chores": "titles"})
        assert s.title_for(_msgs()) == ""
        assert s.failures == 1 and "500" in s.last_error

    def test_timeout_is_fail_open(self, monkeypatch):
        import time as _t

        class Hanging:
            def chat(self, *a, **kw):
                _t.sleep(1.0)
                return "不该等到我"

        s = SmallModel(llm=Hanging(),
                       config={"enabled": True, "chores": "titles", "timeout": 0.05})
        assert s.chat([{"role": "user", "content": "x"}]) == ""
        assert "超时" in s.last_error

    def test_empty_material_no_call(self):
        llm = FakeLLM()
        s = SmallModel(llm=llm, config={"enabled": True, "chores": "titles"})
        assert s.title_for([]) == "" and s.title_for([{"role": "assistant", "content": "x"}]) == ""
        assert llm.calls == []


class TestTitleSanitize:
    @pytest.mark.parametrize("raw,expect", [
        ("《海边小城五章连载》", "海边小城五章连载"),
        ("标题：小模型杂活改造", "小模型杂活改造"),
        ("Topic: chores model", "chores model"),
        ("  海边连载  ", "海边连载"),
        ("海边连载\n（说明：这是标题）", "海边连载"),
        ("“海边连载”", "海边连载"),
    ])
    def test_cleans_model_output(self, raw, expect):
        s = SmallModel(llm=FakeLLM(), config={"enabled": True, "chores": "titles"})
        assert s.sanitize_title(raw) == expect

    def test_rejects_unusable_output(self):
        s = SmallModel(llm=FakeLLM(), config={"enabled": True, "chores": "titles"})
        for raw in ("", "   ", "。。。", "……", "-"):
            assert s.sanitize_title(raw) == "", raw

    def test_truncates_long_title(self):
        s = SmallModel(llm=FakeLLM(),
                       config={"enabled": True, "chores": "titles", "title_max_chars": 8})
        assert s.sanitize_title("这是一个非常长的标题需要截断") == "这是一个非常长的"


class TestTitleWiring:
    def _store(self):
        """显式把 store 指到临时目录（别依赖默认目录——那是真实数据）。"""
        tmp = tempfile.mkdtemp(prefix="sess_title_")
        return SessionStore(config={"dir": tmp}), tmp

    def test_title_fn_used_for_new_conversation(self):
        store, tmp = self._store()
        store.save_conversation("conv-t", messages=_msgs(), title_fn=lambda m: "小模型起的标题")
        assert store.load_conversation("conv-t")["title"] == "小模型起的标题"
        shutil.rmtree(tmp)

    def test_falls_back_when_small_model_returns_nothing(self):
        store, tmp = self._store()
        store.save_conversation("conv-f", messages=_msgs(), title_fn=lambda m: "")
        title = store.load_conversation("conv-f")["title"]
        assert title.startswith("写一部五章连载短篇"), "小模型没给出标题就退回规则标题"
        shutil.rmtree(tmp)

    def test_falls_back_when_title_fn_raises(self):
        store, tmp = self._store()

        def boom(_m):
            raise RuntimeError("小模型炸了")

        store.save_conversation("conv-e", messages=_msgs(), title_fn=boom)
        assert store.load_conversation("conv-e")["title"].startswith("写一部五章连载短篇")
        shutil.rmtree(tmp)

    def test_existing_title_is_kept_and_fn_not_called_again(self):
        store, tmp = self._store()
        calls = []

        def fn(m):
            calls.append(1)
            return "第一版标题"

        store.save_conversation("conv-k", messages=_msgs(), title_fn=fn)
        store.save_conversation("conv-k", messages=_msgs() + [{"role": "user", "content": "继续"}],
                                title_fn=fn)
        assert store.load_conversation("conv-k")["title"] == "第一版标题"
        assert len(calls) == 1, "标题一经生成就固定，不该每轮重算"
        shutil.rmtree(tmp)

    def test_without_title_fn_behaviour_unchanged(self):
        store, tmp = self._store()
        store.save_conversation("conv-n", messages=_msgs())
        assert store.load_conversation("conv-n")["title"].startswith("写一部五章连载短篇")
        shutil.rmtree(tmp)


class TestLLMSelection:
    def test_uses_dedicated_small_model(self, monkeypatch):
        import models.llm as llm_mod
        seen = {}

        class FakeLLMCls:
            def __init__(self, api_key=None, base_url=None, model=None):
                seen.update({"key": api_key, "base": base_url, "model": model})

        monkeypatch.setattr(llm_mod, "LLM", FakeLLMCls)
        monkeypatch.setitem(config.SMALL_MODEL_CONFIG, "model", "small-model-x")
        monkeypatch.setitem(config.SMALL_MODEL_CONFIG, "base_url", "https://small.example/v1")
        monkeypatch.setitem(config.SMALL_MODEL_CONFIG, "api_key", "k-small")
        build_small_llm(object())
        assert seen == {"key": "k-small", "base": "https://small.example/v1",
                        "model": "small-model-x"}

    def test_falls_back_to_main_llm(self, monkeypatch):
        monkeypatch.setitem(config.SMALL_MODEL_CONFIG, "model", "")
        sentinel = object()
        assert build_small_llm(sentinel) is sentinel

    def test_config_knobs_exist(self):
        for k in ("enabled", "model", "base_url", "api_key", "timeout", "chores",
                  "title_max_chars"):
            assert k in config.SMALL_MODEL_CONFIG


class TestAgentWiring:
    def test_agent_builds_small_model_and_passes_title_fn(self, monkeypatch, tmp_path):
        from agent import Agent, AgentConfig
        cfg = AgentConfig(verbose=False, guardian_enabled=False, approval_policy="never",
                          approval_interactive=False, session_name="conv-sm",
                          enable_vision=False, snapshot_enabled=False)
        a = Agent(config=cfg)
        assert a.small_model is not None, "默认应启用小快模型"
        # 用假小模型验证 title_fn 真的接到了保存路径上
        monkeypatch.setattr(a.small_model, "title_for", lambda msgs: "小模型标题")
        a.memory.add_message("user", "写一部五章连载短篇，主题海边")
        a.memory.add_message("assistant", "第 1 章写好了。")
        a._save_session_if_requested("测试摘要")
        saved = a.session_store.load_conversation("conv-sm") or {}
        assert saved.get("title") == "小模型标题", saved

    def test_disabled_config_skips_small_model(self, monkeypatch):
        from agent import Agent, AgentConfig
        monkeypatch.setitem(config.SMALL_MODEL_CONFIG, "enabled", False)
        cfg = AgentConfig(verbose=False, guardian_enabled=False, approval_policy="never",
                          approval_interactive=False, session_name="", enable_vision=False)
        assert Agent(config=cfg).small_model is None
