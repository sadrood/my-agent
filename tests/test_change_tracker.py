"""
文件变更追踪器 + /api/diff 端到端测试（不依赖网络）。
"""
import os

from tools.change_tracker import ChangeTracker, get_change_tracker
from tools.file import FileTool
from tools.patch import EditTool


def test_record_keeps_oldest_snapshot(tmp_path):
    """同一路径多次修改只保留最旧快照（diff 展示会话累计变更）。"""
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")
    t = ChangeTracker()
    t.record_with_old(str(f), "file", "v1")
    t.record_with_old(str(f), "file", "v2")
    rec = t.get(str(f))
    assert rec is not None
    assert rec.old_content == "v1"
    assert rec.writes == 2
    assert rec.is_new is False


def test_new_file_marked(tmp_path):
    """新建文件：old_content 为 None 且 is_new=True。"""
    t = ChangeTracker()
    t.record_with_old(str(tmp_path / "new.txt"), "file", None)
    rec = t.get(str(tmp_path / "new.txt"))
    assert rec is not None and rec.is_new is True


def test_max_paths_cap(tmp_path):
    """路径数超限后静默降级，不抛异常。"""
    t = ChangeTracker(max_paths=2)
    for i in range(5):
        t.record_with_old(str(tmp_path / f"f{i}.txt"), "file", None)
    assert len(t) == 2


def test_snapshot_size_cap(tmp_path):
    """超大文件不留快照（old_content=None，has_snapshot=False）。"""
    big = tmp_path / "big.txt"
    big.write_text("x" * 1000, encoding="utf-8")
    t = ChangeTracker(snapshot_max_bytes=100)
    assert t.snapshot(str(big)) is None


def test_file_tool_write_records_change(tmp_path):
    """FileTool 写入成功后自动登记变更记录。"""
    tracker = get_change_tracker()
    tracker.clear()
    f = tmp_path / "t.txt"
    f.write_text("old", encoding="utf-8")
    r = FileTool().execute(f"write {f} new-content")
    assert r.success
    rec = tracker.get(str(f))
    assert rec is not None
    assert rec.old_content == "old"
    assert rec.tool == "file"
    tracker.clear()


def test_edit_tool_records_change(tmp_path):
    """EditTool 修改成功后自动登记变更记录。"""
    tracker = get_change_tracker()
    tracker.clear()
    f = tmp_path / "e.txt"
    f.write_text("hello world", encoding="utf-8")
    r = EditTool().edit(str(f), "world", "agent", backup=False)
    assert r.success
    rec = tracker.get(str(f))
    assert rec is not None
    assert rec.old_content == "hello world"
    assert rec.tool == "edit"
    tracker.clear()


def test_failed_write_not_recorded(tmp_path):
    """写入失败不产生变更记录。"""
    tracker = get_change_tracker()
    tracker.clear()
    r = EditTool().edit(str(tmp_path / "nope.txt"), "a", "b", backup=False)
    assert not r.success
    assert len(tracker) == 0


def test_api_diff_endpoints(tmp_path, monkeypatch):
    """GET /api/diff：返回 unified diff 与增删统计；越界/未追踪报错。"""
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    tracker = get_change_tracker()
    tracker.clear()

    # 在工作区内造一个被"修改"的文件（工作区根 = 当前 cwd）
    target = os.path.join(srv.WORKSPACE_ROOT, "tests", "_diff_target_tmp.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("line1\nline2\n")
    # 模拟 Agent 修改：先登记旧快照，再改内容
    tracker.record_with_old(target, "edit", "line1\nline2\n")
    with open(target, "w", encoding="utf-8") as f:
        f.write("line1\nline2 changed\nline3\n")
    try:
        client = TestClient(srv.app)
        r = client.get("/api/diff", params={"path": "tests/_diff_target_tmp.txt"})
        data = r.json()
        assert data["ok"] is True
        assert data["added"] == 2 and data["removed"] == 1
        assert "+line2 changed" in data["unified_diff"]
        assert "-line2" in data["unified_diff"]
        assert data["tool"] == "edit"

        # /api/changes 清单包含该文件
        rc = client.get("/api/changes")
        paths = [c["path"].replace("\\", "/") for c in rc.json()["changes"]]
        assert any(p.endswith("tests/_diff_target_tmp.txt") for p in paths)

        # 无 tracker 记录且无 git 改动（未跟踪新文件）→ 明确报错。
        # 注意：tracked 且有改动的文件现在会走 git diff 回退（python/terminal 改的文件也能看 diff）
        untracked = os.path.join(srv.WORKSPACE_ROOT, "tests", "_diff_untracked_tmp.txt")
        with open(untracked, "w", encoding="utf-8") as f:
            f.write("nope\n")
        r2 = client.get("/api/diff", params={"path": "tests/_diff_untracked_tmp.txt"})
        assert r2.json()["ok"] is False
        # 路径越界
        r3 = client.get("/api/diff", params={"path": "../../etc/passwd"})
        assert r3.json()["ok"] is False
        assert "越界" in r3.json()["error"]
    finally:
        tracker.clear()
        if os.path.exists(target):
            os.remove(target)
        if os.path.exists(untracked):
            os.remove(untracked)


def test_api_diff_new_file(tmp_path):
    """新建文件的 diff：全部行为新增。"""
    from fastapi.testclient import TestClient
    import dashboard.server as srv

    tracker = get_change_tracker()
    tracker.clear()
    target = os.path.join(srv.WORKSPACE_ROOT, "tests", "_diff_new_tmp.txt")
    tracker.record_with_old(target, "file", None)
    with open(target, "w", encoding="utf-8") as f:
        f.write("fresh\n")
    try:
        client = TestClient(srv.app)
        data = client.get("/api/diff", params={"path": "tests/_diff_new_tmp.txt"}).json()
        assert data["ok"] is True
        assert data["is_new"] is True
        assert data["added"] == 1 and data["removed"] == 0
    finally:
        tracker.clear()
        if os.path.exists(target):
            os.remove(target)