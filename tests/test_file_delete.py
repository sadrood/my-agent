"""file 工具 v4：delete 操作（破坏性，需多重守卫）。"""
import os

import pytest

from tools.file import FileTool


@pytest.fixture()
def tool():
    return FileTool()


def test_delete_file(tool, tmp_path):
    p = tmp_path / "junk.txt"
    p.write_text("x" * 100, encoding="utf-8")
    r = tool.execute_json({"operation": "delete", "path": str(p)})
    assert r.success, r.error
    assert not p.exists()
    assert r.metadata["bytes"] == 100


def test_delete_missing_path_is_error(tool, tmp_path):
    r = tool.execute_json({"operation": "delete", "path": str(tmp_path / "nope.txt")})
    assert r.success is False and "不存在" in r.error


def test_delete_dir_without_recursive_refused(tool, tmp_path):
    d = tmp_path / "out"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.bin").write_bytes(b"0" * 10)
    r = tool.execute_json({"operation": "delete", "path": str(d)})
    assert r.success is False
    assert "recursive=true" in r.error and "1 个文件" in r.error
    assert d.exists()                      # 目录必须原样保留


def test_delete_dir_recursive(tool, tmp_path):
    d = tmp_path / "out"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "a.bin").write_bytes(b"0" * 2048)
    r = tool.execute_json({"operation": "delete", "path": str(d), "recursive": True})
    assert r.success, r.error
    assert not d.exists()
    assert r.metadata["files"] == 1 and r.metadata["recursive"] is True


class TestDeleteGuards:
    def test_refuses_git_dir(self, tool, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]", encoding="utf-8")
        r = tool.execute_json({"operation": "delete", "path": str(git_dir), "recursive": True})
        assert r.success is False and ".git" in r.error
        assert git_dir.exists()

    def test_refuses_env_file(self, tool, tmp_path):
        env = tmp_path / ".env"
        env.write_text("KEY=1", encoding="utf-8")
        r = tool.execute_json({"operation": "delete", "path": str(env)})
        assert r.success is False and ".env" in r.error
        assert env.exists()

    def test_refuses_project_root(self, tool):
        root = FileTool._project_root()
        r = tool.execute_json({"operation": "delete", "path": root, "recursive": True})
        assert r.success is False and "项目根" in r.error
        assert os.path.isdir(root)

    def test_refuses_drive_root(self, tool):
        root = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
        r = tool.execute_json({"operation": "delete", "path": root, "recursive": True})
        assert r.success is False and "盘根" in r.error


class TestDeleteGuardCaseInsensitive:
    """Windows 上守卫必须按大小写不敏感比较（`os.path.normcase`）。

    实测故障（2026-09-22 审计）：`_delete_guard` 原先用大小写敏感的
    `target == root` / `root.startswith` / `bad in parts`，而 Windows 文件系统
    不区分大小写，于是：
    - `...\\.GIT` / `.ENV` → 放行，而 `os.path.isdir` 为真（就是真 .git / .env）
    - `D:\\AAA\\WORK\\WORK\\MY_AGENT` → 放行，`recursive: true` 会删掉整个仓库
    """

    _posix = pytest.mark.skipif(
        os.path.normcase("A") == "A",
        reason="POSIX 大小写敏感，大小写变体本就是不同路径")

    @pytest.mark.parametrize("variant", [".GIT", ".Git", ".ENV", ".Env"])
    @_posix
    def test_case_variant_of_protected_name_denied(self, tool, variant):
        assert tool._delete_guard(os.path.join(FileTool._project_root(), variant)) != ""

    @_posix
    def test_case_variant_of_project_root_denied(self, tool):
        root = FileTool._project_root()
        for variant in (root.upper(), root.swapcase()):
            assert "项目根" in tool._delete_guard(variant), variant

    @_posix
    def test_case_variant_of_parent_denied(self, tool):
        parent = os.path.dirname(FileTool._project_root())
        assert "上级" in tool._delete_guard(parent.upper())

    def test_normal_path_still_allowed(self, tool, tmp_path):
        assert tool._delete_guard(str(tmp_path / "junk.txt")) == ""

    def test_similar_name_is_not_a_match(self, tool, tmp_path):
        """`.gitx` 不是 `.git`：按路径分量精确比较，不是子串匹配。"""
        assert tool._delete_guard(str(tmp_path / "a" / ".gitx")) == ""


class TestApprovalMetadata:
    def test_delete_is_workspace_write(self, tool, tmp_path):
        req = tool.build_approval_request(
            {"operation": "delete", "path": str(tmp_path / "a.txt")})
        assert req.risk_level == "medium" and req.min_sandbox_mode == "workspace-write"

    def test_recursive_delete_is_high_risk(self, tool, tmp_path):
        req = tool.build_approval_request(
            {"operation": "delete", "path": str(tmp_path / "d"), "recursive": True})
        assert req.risk_level == "high"
        assert "recursive" in req.command

    def test_delete_not_parallel_safe(self, tool):
        assert tool.is_parallel_safe({"operation": "delete"}) is False


def test_string_entry_delete(tool, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.txt").write_text("y", encoding="utf-8")
    assert tool.execute(f"delete {p}").success
    assert not p.exists()
    assert tool.execute(f"delete {d} recursive").success
    assert not d.exists()


def test_schema_exposes_delete(tool):
    ops = tool.schema["properties"]["operation"]["enum"]
    assert "delete" in ops
    assert "recursive" in tool.schema["properties"]
