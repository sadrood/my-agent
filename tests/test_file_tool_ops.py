"""
file 工具 v3 测试：补齐被提示词承诺却从未实现的操作。

背景（2026-09-15 漫剧任务实测）：
- schema 与 executor 提示词一直教模型「写大文件用 file append 分段追加」，
  但 action_map 里没有 append，模型照做只会拿到「未知操作」。
- 模型想复制 5 张产物图片时：shutil 被 python 工具黑名单正确拦下，
  而 file 工具只有 write（纯文本覆盖），于是无路可走、连续 3 轮瞎猜。
"""
import os

import pytest

from tools.file import FileTool


@pytest.fixture()
def tool():
    return FileTool()


# ----------------------------------------------------------------------
# append
# ----------------------------------------------------------------------

def test_append_creates_file_when_missing(tool, tmp_path):
    p = tmp_path / "sub" / "log.txt"
    r = tool.execute_json({"operation": "append", "path": str(p), "content": "第一行\n"})
    assert r.success, r.error
    assert p.read_text(encoding="utf-8") == "第一行\n"


def test_append_adds_to_existing_content(tool, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("A", encoding="utf-8")
    r = tool.execute_json({"operation": "append", "path": str(p), "content": "B"})
    assert r.success, r.error
    assert p.read_text(encoding="utf-8") == "AB"


def test_append_does_not_truncate(tool, tmp_path):
    """关键回归：append 绝不能像 write 那样覆盖已有内容。"""
    p = tmp_path / "big.txt"
    p.write_text("x" * 5000, encoding="utf-8")
    tool.execute_json({"operation": "append", "path": str(p), "content": "tail"})
    assert p.read_text(encoding="utf-8") == "x" * 5000 + "tail"


def test_append_requires_content(tool, tmp_path):
    r = tool.execute("append " + str(tmp_path / "f.txt"))
    assert not r.success
    assert "append" in r.error


def test_append_reported_in_description_and_schema(tool):
    assert "append" in tool.description
    assert "append" in tool.schema["properties"]["operation"]["enum"]


# ----------------------------------------------------------------------
# copy
# ----------------------------------------------------------------------

def test_copy_file(tool, tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(b"\x89PNG fake")
    dst = tmp_path / "out" / "b.png"
    r = tool.execute_json({"operation": "copy", "path": str(src), "destination": str(dst)})
    assert r.success, r.error
    assert dst.read_bytes() == b"\x89PNG fake"
    assert src.exists(), "copy 不应移动/删除源文件"


def test_copy_creates_parent_dirs(tool, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    dst = tmp_path / "deep" / "nest" / "a.txt"
    r = tool.execute_json({"operation": "copy", "path": str(src), "destination": str(dst)})
    assert r.success, r.error
    assert dst.read_text(encoding="utf-8") == "hi"


def test_copy_into_existing_directory(tool, tmp_path):
    """目标已是目录时复制到其中（与 shell cp 语义一致）。"""
    src = tmp_path / "shot1.png"
    src.write_bytes(b"img")
    d = tmp_path / "frames"
    d.mkdir()
    r = tool.execute_json({"operation": "copy", "path": str(src), "destination": str(d)})
    assert r.success, r.error
    assert (d / "shot1.png").read_bytes() == b"img"


def test_copy_directory_recursively(tool, tmp_path):
    src = tmp_path / "srcdir"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "nested" / "b.txt").write_text("b", encoding="utf-8")
    dst = tmp_path / "dstdir"
    r = tool.execute_json({"operation": "copy", "path": str(src), "destination": str(dst)})
    assert r.success, r.error
    assert (dst / "a.txt").read_text(encoding="utf-8") == "a"
    assert (dst / "nested" / "b.txt").read_text(encoding="utf-8") == "b"


def test_copy_missing_source_fails_clearly(tool, tmp_path):
    r = tool.execute_json({
        "operation": "copy",
        "path": str(tmp_path / "nope.png"),
        "destination": str(tmp_path / "x.png"),
    })
    assert not r.success
    assert "不存在" in r.error


def test_copy_without_destination_fails(tool, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    r = tool.execute_json({"operation": "copy", "path": str(src)})
    assert not r.success
    assert "copy" in r.error


# ----------------------------------------------------------------------
# move
# ----------------------------------------------------------------------

def test_move_renames_file(tool, tmp_path):
    src = tmp_path / "old.txt"
    src.write_text("data", encoding="utf-8")
    dst = tmp_path / "new.txt"
    r = tool.execute_json({"operation": "move", "path": str(src), "destination": str(dst)})
    assert r.success, r.error
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "data"


def test_move_missing_source_fails_clearly(tool, tmp_path):
    r = tool.execute_json({
        "operation": "move",
        "path": str(tmp_path / "nope.txt"),
        "destination": str(tmp_path / "x.txt"),
    })
    assert not r.success
    assert "不存在" in r.error


# ----------------------------------------------------------------------
# 元数据 / 审批 / 并行安全
# ----------------------------------------------------------------------

def test_copy_move_are_not_parallel_safe(tool):
    assert tool.is_parallel_safe({"operation": "read"}) is True
    assert tool.is_parallel_safe({"operation": "copy"}) is False
    assert tool.is_parallel_safe({"operation": "move"}) is False
    assert tool.is_parallel_safe({"operation": "append"}) is False


def test_copy_and_move_require_workspace_write(tool):
    for op, extra in (("copy", {"destination": "d"}), ("move", {"destination": "d"}),
                      ("append", {"content": "x"})):
        req = tool.build_approval_request({"operation": op, "path": "p", **extra})
        assert req.risk_level == "medium", op
        assert req.min_sandbox_mode == "workspace-write", op


def test_read_only_ops_stay_low_risk(tool):
    for op in ("read", "list", "exists", "info"):
        req = tool.build_approval_request({"operation": op, "path": "p"})
        assert req.risk_level == "low", op
        assert req.min_sandbox_mode == "read-only", op


def test_unknown_operation_error_lists_new_ops(tool):
    r = tool.execute_json({"operation": "teleport", "path": "x"})
    assert not r.success
    for op in ("append", "copy", "move"):
        assert op in r.error


# ----------------------------------------------------------------------
# 回归：原有操作未被破坏
# ----------------------------------------------------------------------

def test_existing_ops_still_work(tool, tmp_path):
    p = tmp_path / "f.txt"
    assert tool.execute_json({"operation": "write", "path": str(p), "content": "v1"}).success
    assert p.read_text(encoding="utf-8") == "v1"
    assert tool.execute_json({"operation": "read", "path": str(p)}).output == "v1"
    assert tool.execute_json({"operation": "exists", "path": str(p)}).success
    assert "f.txt" in tool.execute_json({"operation": "list", "path": str(tmp_path)}).output
    assert "大小" in tool.execute_json({"operation": "info", "path": str(p)}).output


def test_copy_preserves_mtime(tool, tmp_path):
    """copy2 语义：产物视频/音频的时间戳有意义，不要退化成 copyfile。"""
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 10)
    os.utime(src, (1000000, 1000000))
    dst = tmp_path / "b.bin"
    tool.execute_json({"operation": "copy", "path": str(src), "destination": str(dst)})
    assert abs(os.path.getmtime(dst) - 1000000) < 2
