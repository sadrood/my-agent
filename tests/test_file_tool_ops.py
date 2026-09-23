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


# ----------------------------------------------------------------------
# 回归：JSON 入口不得回落到"按空格切分"的字符串解码
# ----------------------------------------------------------------------

class TestPathsWithSpaces:
    """路径含空格时绝不能写错文件。

    实测故障（2026-09-17 审计）：execute_json 曾把结构化参数拼成
    "write <path> <content>" 再交给字符串解码按空格切分，于是
    {"path": "...\\My Documents\\notes.txt", "content": "hi"} 实际写出一个
    名为 `My` 的文件、内容是 "Documents\\notes.txt hi"，**并且返回 success=True**。
    静默写错文件比直接报错危险得多。
    """

    def test_write_with_space_in_path(self, tool, tmp_path):
        d = tmp_path / "My Documents"
        target = d / "notes.txt"
        r = tool.execute_json({"operation": "write", "path": str(target),
                               "content": "hello"})
        assert r.success, r.error
        assert target.read_text(encoding="utf-8") == "hello"
        # 不得产生"被切坏"的杂散文件
        assert [p.name for p in tmp_path.iterdir()] == ["My Documents"]

    def test_append_with_space_in_path(self, tool, tmp_path):
        d = tmp_path / "My Documents"
        d.mkdir()
        target = d / "log.txt"
        target.write_text("A", encoding="utf-8")
        r = tool.execute_json({"operation": "append", "path": str(target),
                               "content": " B"})
        assert r.success, r.error
        assert target.read_text(encoding="utf-8") == "A B"
        assert [p.name for p in tmp_path.iterdir()] == ["My Documents"]

    def test_copy_with_space_in_source(self, tool, tmp_path):
        src_dir = tmp_path / "My Docs"
        src_dir.mkdir()
        src = src_dir / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = tmp_path / "out.txt"
        r = tool.execute_json({"operation": "copy", "path": str(src),
                               "destination": str(dst)})
        assert r.success, r.error
        assert dst.read_text(encoding="utf-8") == "data"

    def test_copy_both_sides_with_spaces(self, tool, tmp_path):
        src = tmp_path / "my file.txt"
        src.write_text("x", encoding="utf-8")
        dst = tmp_path / "Out Put" / "copy of file.txt"
        r = tool.execute_json({"operation": "copy", "path": str(src),
                               "destination": str(dst)})
        assert r.success, r.error
        assert dst.read_text(encoding="utf-8") == "x"

    def test_move_with_space_in_paths(self, tool, tmp_path):
        src = tmp_path / "old name.txt"
        src.write_text("m", encoding="utf-8")
        dst = tmp_path / "new name.txt"
        r = tool.execute_json({"operation": "move", "path": str(src),
                               "destination": str(dst)})
        assert r.success, r.error
        assert dst.read_text(encoding="utf-8") == "m"
        assert not src.exists()

    def test_content_spaces_preserved(self, tool, tmp_path):
        """内容里的连续空格/制表符/换行必须原样落盘（不能被当分隔符吃掉）。"""
        target = tmp_path / "c.txt"
        body = "a  b\tc\nd\n"
        r = tool.execute_json({"operation": "write", "path": str(target),
                               "content": body})
        assert r.success, r.error
        assert target.read_text(encoding="utf-8") == body

    def test_string_entry_still_works(self, tool, tmp_path):
        """字符串入口保持向后兼容（无空格路径）。"""
        target = tmp_path / "s.txt"
        assert tool.execute(f"write {target} hi there").success
        assert target.read_text(encoding="utf-8") == "hi there"
        assert tool.execute(f"append {target} !").success
        assert target.read_text(encoding="utf-8") == "hi there!"


class TestWriteDoesNotTranslateNewlines:
    """`file write/append` 不能做隐式换行翻译，否则与 `edit` 策略相反。

    实测故障（2026-09-22 审计）：文本模式在 Windows 上把 LF 自动翻成 CRLF，而
    `edit` 保留文件原有风格 —— 两个写工具交替使用会把整个文件的行尾来回翻。
    """

    def test_write_keeps_lf(self, tool, tmp_path):
        p = tmp_path / "a.txt"
        r = tool.execute_json({"operation": "write", "path": str(p), "content": "a\nb\n"})
        assert r.success is True, r.error
        assert p.read_bytes() == b"a\nb\n"

    def test_append_keeps_lf(self, tool, tmp_path):
        p = tmp_path / "a.txt"
        p.write_bytes(b"x\n")
        r = tool.execute_json({"operation": "append", "path": str(p), "content": "y\n"})
        assert r.success is True, r.error
        assert p.read_bytes() == b"x\ny\n"
