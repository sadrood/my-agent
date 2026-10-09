"""ego 会话隔离：每个会话自己的任务空间与标签页，两个 agent 不再抢页面。"""
import json

import pytest

from tools.ego_browser import EgoBrowserTool


@pytest.fixture
def tool(monkeypatch, tmp_path):
    from config import BROWSER_CONFIG
    monkeypatch.setitem(BROWSER_CONFIG, "ego_isolate", True)
    monkeypatch.setenv("MY_AGENT_SESSION_ID", "conv-abc")
    monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
    return EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")


class TestSessionSpace:
    def test_space_is_suffixed_by_session(self, tool):
        assert tool._space_name == "my-agent-conv-abc"

    def test_two_sessions_never_share_a_space(self, monkeypatch, tmp_path):
        from config import BROWSER_CONFIG
        monkeypatch.setitem(BROWSER_CONFIG, "ego_isolate", True)
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("MY_AGENT_SESSION_ID", "sess-1")
        first = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        monkeypatch.setenv("MY_AGENT_SESSION_ID", "sess-2")
        second = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        assert first._space_name != second._space_name

    def test_falls_back_to_pid_without_session_id(self, monkeypatch, tmp_path):
        from config import BROWSER_CONFIG
        import os
        monkeypatch.setitem(BROWSER_CONFIG, "ego_isolate", True)
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.delenv("MY_AGENT_SESSION_ID", raising=False)
        monkeypatch.delenv("DSH_SESSION_ID", raising=False)
        tool = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        assert tool._space_name.endswith(f"p{os.getpid()}")

    def test_isolate_off_keeps_old_shared_behaviour(self, monkeypatch, tmp_path):
        from config import BROWSER_CONFIG
        monkeypatch.setitem(BROWSER_CONFIG, "ego_isolate", False)
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("MY_AGENT_SESSION_ID", "conv-abc")
        tool = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        assert tool._space_name == "my-agent", "关掉隔离时应回到共享空间的旧行为"

    def test_session_id_is_sanitised(self, monkeypatch, tmp_path):
        from config import BROWSER_CONFIG
        monkeypatch.setitem(BROWSER_CONFIG, "ego_isolate", True)
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("MY_AGENT_SESSION_ID", "conv/../../evil id")
        tool = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        assert "/" not in tool._space_name and " " not in tool._space_name
        assert tool._space_name.startswith("my-agent-")


class TestTabBinding:
    def test_remembers_tab_from_script_output(self, tool):
        tool._bind_from_stdout('__EGO_BIND__{"space":"my-agent-conv-abc","spaceId":9,"tab":"ABC123"}\n')
        assert tool._remembered_tab() == "ABC123"
        data = json.load(open(tool._state_path(), encoding="utf-8"))
        assert data["tabs"]["my-agent-conv-abc"] == "ABC123"

    def test_tabs_are_kept_per_space(self, tool):
        """切到别的空间后，不能拿自己空间的标签页 id 去那边用。"""
        tool._bind_from_stdout('__EGO_BIND__{"tab":"MINE1"}\n')
        assert tool._remembered_tab() == "MINE1"
        tool._space_override = "my-agent"
        assert tool._remembered_tab() == "", "别的空间还没记过标签页"
        tool._bind_from_stdout('__EGO_BIND__{"tab":"OTHER2"}\n')
        assert tool._remembered_tab() == "OTHER2"
        tool._space_override = ""
        assert tool._remembered_tab() == "MINE1", "切回来仍记得自己的"

    def test_next_run_reuses_remembered_tab(self, tool):
        tool._bind_from_stdout('__EGO_BIND__{"tab":"ABC123"}\n')
        script = tool._script("  __out = {}\n")
        assert '"ABC123"' in script, "脚本要带上上次记住的标签页 id"
        assert '"my-agent-conv-abc"' in script

    def test_state_file_survives_new_instance(self, tool, tmp_path, monkeypatch):
        tool._bind_from_stdout('__EGO_BIND__{"tab":"KEEP9"}\n')
        monkeypatch.setattr("config.PROJECT_ROOT", str(tmp_path))
        fresh = EgoBrowserTool(cli="C:/fake/ego.mjs", space="my-agent")
        assert fresh._remembered_tab() == "KEEP9", "同一会话换进程后仍要复用同一个标签页"

    def test_bad_output_is_ignored(self, tool):
        tool._bind_from_stdout("没有标记的输出\n__EGO_BIND__{坏 json}\n")
        assert tool._remembered_tab() == ""


class TestQuietFastPath:
    """切空间/switchTab 会把窗口拉到最前（Target.activateTarget）：已经在本会话
    标签页上时必须什么都不做，否则每条命令都弹一次窗口、抢用户的焦点。"""

    def test_preamble_checks_current_tab_first(self, tool):
        script = tool._script("  __out = {}\n")
        assert "currentTab()" in script
        assert "__onMine" in script and "reused" in script

    def test_preamble_skips_space_and_tab_switch_when_on_our_tab(self, tool):
        script = tool._script("  __out = {}\n")
        # 快路径必须把 useOrCreate/switchTab 包在"不在自己的标签页上"的分支里
        head, _, tail = script.partition("if (!__onMine) {")
        assert head and "taskSpaces.useOrCreate" not in head, "认领空间不能无条件执行"
        assert "switchTab" not in head
        body, _, _ = tail.partition("\n}\n")
        assert "taskSpaces.useOrCreate" in body and "switchTab" in body

    def test_bind_reports_reuse(self, tool):
        calls = {}

        def fake_run(body, timeout=None):
            calls["body"] = body
            return {"ok": True, "__out": {}}

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(tool, "_run", fake_run)
            tool._bind_from_stdout('__EGO_BIND__{"tab":"T1","reused":true}\n')
            tool.execute("url")
            assert tool._remembered_tab() == "T1"
        finally:
            monkeypatch.undo()

    def test_spaces_is_read_only(self, tool):
        """以前挨个切空间列标签页 → 每个窗口闪一遍；现在只读元数据。"""
        body = tool._body_for("spaces", "")
        assert "listTaskSpaces" in body
        assert "taskSpaces.switch" not in body
        assert "browser.listTabs" not in body


class TestPreambleAndCommands:
    def test_preamble_selects_space_and_prefers_our_tab(self, tool):
        script = tool._script("  __out = {}\n")
        assert "taskSpaces.useOrCreate" in script
        assert "listTabs" in script and "switchTab" in script
        assert "createTab" in script, "自己的空间里没有标签页时要自己开一个，别去抢别人的"

    def test_new_commands_are_registered(self, tool):
        for cmd in ("tabs", "newtab", "switchtab", "spaces", "usespace"):
            assert cmd in tool._EGO_COMMANDS
            assert cmd in tool.schema["properties"]["command"]["enum"]

    def test_spaces_body_lists_every_space_with_pages(self, tool):
        body = tool._body_for("spaces", "")
        assert "listTaskSpaces" in body
        assert "urls" in body and "titles" in body, "空间里有哪些页面要从元数据里带出来"
        assert "mine" in body

    def test_usespace_body_accepts_name_or_id(self, tool):
        body = tool._body_for("usespace", "my-agent")
        assert "listTaskSpaces" in body and "taskSpaces.switch" in body and "claim" in body
        assert tool._body_for("usespace", "") is None

    def test_usespace_own_returns_to_session_space(self, tool, monkeypatch):
        calls = {}

        def fake_run(body, timeout=None):
            calls["body"] = body
            return {"ok": True, "__out": {"space": "my-agent-conv-abc", "id": 12}}

        monkeypatch.setattr(tool, "_run", fake_run)
        out = tool.execute("usespace own")
        assert out.success
        assert f'"my-agent-conv-abc"' in calls["body"], "own 要解析成本会话自己的空间名"
        data = json.load(open(tool._state_path(), encoding="utf-8"))
        assert data["active"] == "my-agent-conv-abc"

    def test_usespace_without_arg_is_rejected(self, tool):
        out = tool.execute("usespace")
        assert not out.success and "空间名" in out.error

    def test_newtab_body_creates_and_binds(self, tool):
        body = tool._body_for("newtab", "example.com")
        assert "ego.createTab" in body and "switchTab" in body
        assert '"https://example.com"' in body

    def test_switchtab_accepts_index_or_id(self, tool):
        body = tool._body_for("switchtab", "1")
        assert "listTabs" in body and '"1"' in body
        assert tool._body_for("switchtab", "") is None, "缺参数要报错而不是瞎切"

    def test_tabs_body_reports_space(self, tool):
        body = tool._body_for("tabs", "")
        assert "space" in body and "listTabs" in body
