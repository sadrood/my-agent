"""
EditTool（精确编辑）与 Git 快照测试。
"""
import os
import shutil

import pytest

from tools.patch import EditTool
from tools.tool_manager import ToolManager


class TestEditTool:
    def _write(self, tmp_path, name="a.py", content="line1\nline2\nline3\n"):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        return str(f)

    def test_single_replacement(self, tmp_path):
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "line2", "new_string": "LINE2",
        })
        assert r.success is True
        content = open(path, encoding="utf-8").read()
        assert content == "line1\nLINE2\nline3\n"
        assert r.metadata["matches"] == 1
        # .bak 生命周期：修改成功后备份即时清理，不残留垃圾文件
        import os as _os
        assert not _os.path.exists(path + ".bak")
        assert "backup_path" not in r.metadata
        # 旧内容快照改由 metadata.old_text 携带（供 unified diff 渲染）
        assert r.metadata["old_text"] == "line1\nline2\nline3\n"
        assert r.metadata["old_text_truncated"] is False

    def test_old_text_truncated_for_large_file(self, tmp_path):
        from tools.patch import _OLD_TEXT_META_MAX
        big = "x = 1\n" + ("# pad\n" * (_OLD_TEXT_META_MAX // 6 * 2))
        path = self._write(tmp_path, content=big)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True
        assert r.metadata["old_text_truncated"] is True
        assert len(r.metadata["old_text"]) == _OLD_TEXT_META_MAX

    def test_delete_with_empty_new_string(self, tmp_path):
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "line2\n", "new_string": "",
        })
        assert r.success is True
        content = open(path, encoding="utf-8").read()
        assert content == "line1\nline3\n"

    def test_not_found(self, tmp_path):
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "不存在的内容", "new_string": "x",
        })
        assert r.success is False
        assert "0 次" in r.error

    def test_multiple_matches_require_replace_all(self, tmp_path):
        path = self._write(tmp_path, content="dup\ndup\ndup\n")
        r = EditTool().execute_json({
            "file_path": path, "old_string": "dup", "new_string": "x",
        })
        assert r.success is False
        assert "3 处" in r.error

        r2 = EditTool().execute_json({
            "file_path": path, "old_string": "dup", "new_string": "x",
            "replace_all": True,
        })
        assert r2.success is True
        content = open(path, encoding="utf-8").read()
        assert content == "x\nx\nx\n"

    def test_crlf_old_string_normalized(self, tmp_path):
        # 用二进制写避免 Windows 文本模式的换行二次转译
        f = tmp_path / "a.py"
        f.write_bytes("a\r\nb\r\n".encode("utf-8"))
        path = str(f)
        # 模型传来的 old_string 可能带 \r\n：归一化后仍能匹配
        r = EditTool().execute_json({
            "file_path": path, "old_string": "a\r\nb", "new_string": "z",
        })
        assert r.success is True
        # 文件被归一化为 LF 后写入
        assert open(path, encoding="utf-8").read() == "z\n"

    def test_missing_file(self, tmp_path):
        r = EditTool().execute_json({
            "file_path": str(tmp_path / "nope.py"), "old_string": "a", "new_string": "b",
        })
        assert r.success is False
        assert "不存在" in r.error

    def test_registered_in_tool_manager(self):
        tm = ToolManager()
        assert "edit" in tm.list_tools()
        schemas = tm.list_openai_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "edit" in names

    def test_approval_metadata(self):
        req = EditTool().build_approval_request({"file_path": "x.py", "old_string": "a", "new_string": "b"})
        assert req.min_sandbox_mode == "workspace-write"
        assert req.risk_level == "medium"


def _git_available():
    import subprocess
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


class TestSnapshot:
    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_ensure_repo_and_snapshot(self, tmp_path):
        from agent.snapshot import ensure_repo, snapshot, is_git_repo, has_pending_changes

        assert ensure_repo(str(tmp_path)) is True
        assert is_git_repo(str(tmp_path)) is True

        # 基线提交后干净
        assert has_pending_changes(str(tmp_path)) is False

        # 制造改动 → 快照 → 干净
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        assert snapshot(str(tmp_path), "测试任务") is True
        assert has_pending_changes(str(tmp_path)) is False

        # 快照记录在 git log 里
        import subprocess
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(tmp_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert "运行前快照" in log.stdout
        assert "初始化仓库基线" in log.stdout

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_snapshot_non_repo_returns_false(self, tmp_path):
        from agent.snapshot import snapshot
        assert snapshot(str(tmp_path), "x") is False

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_checkpoint_per_tool(self, tmp_path):
        """逐操作式检查点：每次修改前 git 提交，可逐操作回滚。"""
        from agent.snapshot import checkpoint, ensure_repo, is_git_repo
        import subprocess

        ensure_repo(str(tmp_path))
        assert is_git_repo(str(tmp_path)) is True

        # 第一次修改 → checkpoint 提交（返回该检查点的 commit hash）
        (tmp_path / "a.py").write_text("v1", encoding="utf-8")
        hash1 = checkpoint(str(tmp_path), "edit a.py")
        assert hash1

        # 第二次修改 → 再一个 checkpoint
        (tmp_path / "a.py").write_text("v2", encoding="utf-8")
        hash2 = checkpoint(str(tmp_path), "edit a.py")
        assert hash2 and hash2 != hash1

        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(tmp_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert log.stdout.count("checkpoint:") == 2

        # 干净仓库 → checkpoint 也成功（无需提交，返回 HEAD hash）
        assert checkpoint(str(tmp_path), "noop") == hash2

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_rollback_to_checkpoint(self, tmp_path):
        """rollback_to：恢复文件内容、移除新增文件，作为新提交落库（不重写历史）。"""
        import subprocess
        from agent.snapshot import checkpoint, ensure_repo, rollback_to

        ensure_repo(str(tmp_path))

        # 基线：a.py = v1，做一次检查点
        (tmp_path / "a.py").write_text("v1", encoding="utf-8")
        base_hash = checkpoint(str(tmp_path), "edit a.py", changed_file="a.py")
        assert base_hash

        # 第二次修改：改 a.py + 新增 b.py
        (tmp_path / "a.py").write_text("v2", encoding="utf-8")
        (tmp_path / "b.py").write_text("new", encoding="utf-8")
        second_hash = checkpoint(str(tmp_path), "edit a.py b.py")
        assert second_hash

        # 回滚到第一次检查点：a.py 恢复 v1，b.py 应被移除
        new_head = rollback_to(str(tmp_path), base_hash)
        assert new_head and new_head != second_hash
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "v1"
        assert not (tmp_path / "b.py").exists()

        # 历史保留：所有旧提交仍在（不重写历史）
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(tmp_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert "rollback:" in log.stdout
        assert second_hash[:10] in log.stdout or base_hash[:10] in log.stdout

        # 工作区干净（回滚已提交）
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(tmp_path),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert not status.stdout.strip()

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_rollback_rejects_non_ancestor(self, tmp_path):
        """非祖先提交（无关仓库的 hash / 乱码）必须拒绝。"""
        from agent.snapshot import ensure_repo, rollback_to

        ensure_repo(str(tmp_path))
        assert rollback_to(str(tmp_path), "deadbeef" * 5) == ""
        assert rollback_to(str(tmp_path), "") == ""


class TestEditPreflight:
    """edit 验证式应用：跑测试，失败自动回滚。"""

    def _write(self, tmp_path, name="mod.py"):
        f = tmp_path / name
        f.write_text("x = 1\n", encoding="utf-8")
        return str(f)

    def test_preflight_pass(self, tmp_path, monkeypatch):
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight_timeout", 60)
        monkeypatch.setitem(TEST_CONFIG, "command", "exit 0")
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True
        assert "preflight 测试通过" in r.output
        assert open(path, encoding="utf-8").read() == "x = 2\n"

    def test_preflight_failure_rolls_back(self, tmp_path, monkeypatch):
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TEST_CONFIG, "command", "exit 1")
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is False
        assert "已自动回滚" in r.error
        # 文件已恢复原样
        assert open(path, encoding="utf-8").read() == "x = 1\n"
        # 回滚成功后备份已用完，.bak 即时清理
        import os as _os
        assert not _os.path.exists(path + ".bak")

    def test_preflight_skipped_when_disabled(self, tmp_path, monkeypatch):
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", False)
        monkeypatch.setitem(TEST_CONFIG, "command", "exit 1")   # 关着：不跑测试
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True
        assert "preflight 测试通过" not in r.output
        assert r.metadata.get("preflight") is False

    def test_preflight_skipped_inside_pytest_recursion(self, tmp_path, monkeypatch):
        """递归保护：pytest 内 + 命令含 pytest → 跳过（防止测试→edit→pytest 死循环）。"""
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TEST_CONFIG, "command", "pytest tests -q")
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "TestEditPreflight::x")
        path = self._write(tmp_path)
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True
        assert r.metadata.get("preflight") is False      # 被跳过
        assert open(path, encoding="utf-8").read() == "x = 2\n"

    def test_preflight_skipped_non_python(self, tmp_path, monkeypatch):
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TEST_CONFIG, "command", "exit 1")
        path = self._write(tmp_path, name="notes.txt")
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True

    def test_preflight_skipped_venv(self, tmp_path, monkeypatch):
        # .venv 内的文件不触发 preflight
        from config import TOOL_CONFIG, TEST_CONFIG
        monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", True)
        monkeypatch.setitem(TEST_CONFIG, "command", "exit 1")
        venv = tmp_path / ".venv"
        venv.mkdir()
        path = str(venv / "mod.py")
        open(path, "w", encoding="utf-8").write("x = 1\n")
        r = EditTool().execute_json({
            "file_path": path, "old_string": "x = 1", "new_string": "x = 2",
        })
        assert r.success is True


def _commit_count(repo: str) -> int:
    import subprocess
    log = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return int(log.stdout.strip() or "0")


def _dirty(repo: str) -> list:
    import subprocess
    r = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return [line[3:].strip() for line in r.stdout.splitlines() if line.strip()]


class TestSelectiveSnapshot:
    """选择性快照（full=False / changed_file）：Agent 提交不卷入并行工作。"""

    def _ensure_repo_with_identity(self, tmp_path):
        from agent.snapshot import ensure_repo
        import subprocess
        assert ensure_repo(str(tmp_path)) is True
        subprocess.run(["git", "config", "user.email", "test@local"],
                       cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"],
                       cwd=str(tmp_path), capture_output=True)

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_selective_snapshot_skips_parallel_work(self, tmp_path):
        """full=False：并行未提交改动不被卷进 Agent 提交，保持原样。"""
        from agent.snapshot import snapshot, has_pending_changes

        self._ensure_repo_with_identity(tmp_path)
        (tmp_path / "user_wip.txt").write_text("并行工作", encoding="utf-8")
        commits_before = _commit_count(str(tmp_path))

        assert snapshot(str(tmp_path), "测试任务", full=False) is True
        assert _commit_count(str(tmp_path)) == commits_before   # 没有新提交
        assert has_pending_changes(str(tmp_path)) is True       # 并行改动原样保留
        assert _dirty(str(tmp_path)) == ["user_wip.txt"]

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_selective_snapshot_commits_staged_only(self, tmp_path):
        """full=False：暂存区已有内容时正常提交。"""
        from agent.snapshot import snapshot, has_pending_changes
        import subprocess

        self._ensure_repo_with_identity(tmp_path)
        (tmp_path / "staged.txt").write_text("已暂存", encoding="utf-8")
        subprocess.run(["git", "add", "staged.txt"], cwd=str(tmp_path),
                       capture_output=True)
        commits_before = _commit_count(str(tmp_path))

        assert snapshot(str(tmp_path), "测试任务", full=False) is True
        assert _commit_count(str(tmp_path)) == commits_before + 1
        assert "staged.txt" not in _dirty(str(tmp_path))

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_full_snapshot_keeps_legacy_behavior(self, tmp_path):
        """full=True（默认）：全量 add -A，旧行为不变。"""
        from agent.snapshot import snapshot, has_pending_changes

        self._ensure_repo_with_identity(tmp_path)
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        assert snapshot(str(tmp_path), "测试任务", full=True) is True
        assert has_pending_changes(str(tmp_path)) is False

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_checkpoint_with_changed_file_commits_only_that_file(self, tmp_path):
        """checkpoint(changed_file=...)：只提交本次修改的文件（归因提交）。"""
        from agent.snapshot import checkpoint, has_pending_changes

        self._ensure_repo_with_identity(tmp_path)
        (tmp_path / "mine.py").write_text("v1", encoding="utf-8")
        (tmp_path / "user_wip.txt").write_text("并行工作", encoding="utf-8")

        assert checkpoint(str(tmp_path), "edit mine.py",
                          changed_file="mine.py")
        # Agent 的文件已提交；并行工作保持未提交
        assert "mine.py" not in _dirty(str(tmp_path))
        assert "user_wip.txt" in _dirty(str(tmp_path))
        assert has_pending_changes(str(tmp_path)) is True

    @pytest.mark.skipif(not _git_available(), reason="git 不可用")
    def test_checkpoint_without_changed_file_keeps_legacy(self, tmp_path):
        """不传 changed_file：退回全量 add -A（向后兼容）。"""
        from agent.snapshot import checkpoint, has_pending_changes

        self._ensure_repo_with_identity(tmp_path)
        (tmp_path / "a.py").write_text("v1", encoding="utf-8")
        assert checkpoint(str(tmp_path), "edit a.py")
        assert has_pending_changes(str(tmp_path)) is False


class TestDiffMetadataFallback:
    """edit 成功后 .bak 已删：diff 渲染退回 metadata.old_text，质量不降级。"""

    def _make_agent(self, tmp_path):
        from agent import Agent, AgentConfig
        from agent.memory import Memory
        from tools.tool_manager import ToolManager
        from tests.test_agent_loop import FakeLLM

        config = AgentConfig(
            verbose=True,
            rollout_enabled=False,
            guardian_enabled=False,
            approval_policy="never",
            sandbox_mode="workspace-write",
            approval_interactive=False,
            instructions_enabled=False,
            enable_vision=False,
            enable_frame_compare=False,
            enable_anomaly_detect=False,
            snapshot_enabled=False,
            checkpoint_per_tool=False,
            repomap_enabled=False,
        )
        return Agent(
            llm=FakeLLM([]),
            tool_manager=ToolManager(),
            memory=Memory(db_path=str(tmp_path)),
            config=config,
        )

    @pytest.fixture(autouse=True)
    def _fresh_console(self, monkeypatch):
        import agent.ui_theme as ui
        monkeypatch.setattr(ui, "_console", None)
        monkeypatch.setattr(ui, "_legacy_console", None)
        yield

    def test_old_text_metadata_renders_unified_diff(self, tmp_path, capsys):
        agent = self._make_agent(tmp_path)
        f = tmp_path / "mod.py"
        # 模拟 edit 后的磁盘状态：内容已变，旧内容快照由 metadata 携带
        f.write_text("x = 2\ny = 2\n", encoding="utf-8")
        agent._loop_tool_event("tool_result", {
            "tool": "edit",
            "success": True,
            "arguments": {"file_path": str(f), "old_string": "x = 1", "new_string": "x = 2"},
            "metadata": {"old_text": "x = 1\ny = 2\n", "old_text_truncated": False},
        })
        out = capsys.readouterr().out
        assert "@@" in out            # unified diff 标记（@@ 行号头）
        assert "+x = 2" in out

    def test_no_old_text_falls_back_to_simple_diff(self, tmp_path, capsys):
        agent = self._make_agent(tmp_path)
        agent._loop_tool_event("tool_result", {
            "tool": "edit",
            "success": True,
            "arguments": {"file_path": "nope.py", "old_string": "a", "new_string": "b"},
            "metadata": {},
        })
        out = capsys.readouterr().out
        assert "@@" not in out        # 简式 diff，无 unified 标记
