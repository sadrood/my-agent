"""
环境自检（--doctor）测试。
"""
import pytest

import agent.doctor as doc


def test_python_check():
    r = doc._check_python()
    assert r["ok"] is True
    assert "Python" in r["message"]


def test_deps_check():
    r = doc._check_deps()
    # 测试环境已装齐依赖（websockets 等）
    assert r["ok"] is True


def test_tools_check():
    r = doc._check_tools()
    assert r["ok"] is True
    assert "edit" in r["message"]


def test_ws_support_check():
    r = doc._check_ws_support()
    assert r["ok"] is True
    assert "/ws" in r["message"]


def test_git_check():
    r = doc._check_git()
    # 本机有 git 且项目已是仓库（snapshot 安全网已初始化）
    assert r["ok"] is True


def test_env_check_without_key(monkeypatch):
    import config
    saved = dict(config.LLM_CONFIG)
    try:
        config.LLM_CONFIG["api_key"] = None
        r = doc._check_env()
        assert r["ok"] is False
        assert "LLM_API_KEY" in r["message"]
    finally:
        config.LLM_CONFIG.clear()
        config.LLM_CONFIG.update(saved)


def test_llm_check_ok():
    class FakeLLM:
        def chat(self, messages, **kwargs):
            return "pong"

    r = doc._check_llm(llm=FakeLLM())
    assert r["ok"] is True
    assert "响应正常" in r["message"]


def test_llm_check_fail():
    class BadLLM:
        def chat(self, messages, **kwargs):
            raise ConnectionError("无法连接")

    r = doc._check_llm(llm=BadLLM())
    assert r["ok"] is False
    assert "无法连接" in r["message"]
    assert "LLM_BASE_URL" in r["hint"]


def test_run_doctor_without_llm():
    results = doc.run_doctor(include_llm=False)
    names = {r["name"] for r in results}
    assert "Python 版本" in names
    assert "依赖完整性" in names
    assert "Dashboard WebSocket" in names
    assert "主模型连通" not in names   # include_llm=False 跳过


def test_render_report():
    results = [
        {"name": "检查A", "ok": True, "message": "正常", "hint": ""},
        {"name": "检查B", "ok": False, "message": "缺依赖", "hint": "pip install x"},
    ]
    text = doc.render_report(results)
    assert "✓" in text and "✗" in text
    assert "1/2" in text
    assert "pip install x" in text


class TestNewSubsystemChecks:
    """今日新增子系统的自检：hooks / execpolicy / skills / sandbox。"""

    def test_hooks_disabled_ok(self, monkeypatch):
        from config import HOOKS_CONFIG
        monkeypatch.setitem(HOOKS_CONFIG, "enabled", False)
        assert doc._check_hooks()["ok"] is True
        assert "未启用" in doc._check_hooks()["message"]

    def test_hooks_enabled_missing_file(self, monkeypatch, tmp_path):
        from config import HOOKS_CONFIG
        monkeypatch.setitem(HOOKS_CONFIG, "enabled", True)
        monkeypatch.setitem(HOOKS_CONFIG, "hooks_file", str(tmp_path / "nope.py"))
        r = doc._check_hooks()
        assert r["ok"] is False
        assert "不存在" in r["message"]

    def test_hooks_enabled_broken_syntax(self, monkeypatch, tmp_path):
        from config import HOOKS_CONFIG
        bad = tmp_path / "hooks.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        monkeypatch.setitem(HOOKS_CONFIG, "enabled", True)
        monkeypatch.setitem(HOOKS_CONFIG, "hooks_file", str(bad))
        r = doc._check_hooks()
        assert r["ok"] is False
        assert "语法错误" in r["message"]

    def test_hooks_enabled_valid_file(self, monkeypatch, tmp_path):
        from config import HOOKS_CONFIG
        good = tmp_path / "hooks.py"
        good.write_text("def on_pre_tool_use(t, a):\n    pass\n", encoding="utf-8")
        monkeypatch.setitem(HOOKS_CONFIG, "enabled", True)
        monkeypatch.setitem(HOOKS_CONFIG, "hooks_file", str(good))
        r = doc._check_hooks()
        assert r["ok"] is True

    def test_exec_policy_disabled_ok(self, monkeypatch):
        from config import APPROVAL_CONFIG
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", False)
        assert doc._check_exec_policy()["ok"] is True

    def test_exec_policy_invalid_json(self, monkeypatch, tmp_path):
        from config import APPROVAL_CONFIG
        bad = tmp_path / "execpolicy.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", True)
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_file", str(bad))
        r = doc._check_exec_policy()
        assert r["ok"] is False
        assert "损坏" in r["message"] and "fail-open" in r["message"]

    def test_exec_policy_valid_array(self, monkeypatch, tmp_path):
        from config import APPROVAL_CONFIG
        good = tmp_path / "execpolicy.json"
        good.write_text('[{"match": {"tool": "terminal"}, "decision": "deny"}]',
                        encoding="utf-8")
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_enabled", True)
        monkeypatch.setitem(APPROVAL_CONFIG, "exec_policy_file", str(good))
        r = doc._check_exec_policy()
        assert r["ok"] is True

    def test_skills_counts_discovered(self, monkeypatch, tmp_path):
        from config import SKILLS_CONFIG
        skill_dir = tmp_path / "skills" / "excel"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: excel\ndescription: 报表\ntriggers: excel\n---\nbody",
            encoding="utf-8")
        monkeypatch.setitem(SKILLS_CONFIG, "enabled", True)
        monkeypatch.setitem(SKILLS_CONFIG, "project_dir", str(tmp_path / "skills"))
        monkeypatch.setitem(SKILLS_CONFIG, "user_dir", str(tmp_path / "no_user"))
        r = doc._check_skills()
        assert r["ok"] is True
        assert "1 个技能" in r["message"]

    def test_sandbox_off_ok(self, monkeypatch):
        from config import SANDBOX_EXEC_CONFIG
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "off")
        r = doc._check_sandbox()
        assert r["ok"] is True

    def test_sandbox_appcontainer_mode(self, monkeypatch):
        from config import SANDBOX_EXEC_CONFIG
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "mode", "appcontainer")
        monkeypatch.setitem(SANDBOX_EXEC_CONFIG, "allow_network", True)
        r = doc._check_sandbox()
        assert r["ok"] is True
        assert "网络 放行" in r["message"]

    def test_new_checks_in_run_doctor(self):
        names = {r["name"] for r in doc.run_doctor(include_llm=False)}
        assert {"Hooks 钩子", "execpolicy 策略", "Skills 技能", "OS 级沙箱"} <= names
