"""
AGENTS.md 分层指令加载测试。
"""
import os

from agent.instructions import InstructionsLoader


def test_load_project_agents(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# 项目规则\n不要删除任何文件。", encoding="utf-8")
    loader = InstructionsLoader()
    text = loader.load(project_dir=str(tmp_path))
    assert "项目规则" in text
    assert "不要删除任何文件" in text


def test_load_user_agents(tmp_path):
    user_file = tmp_path / "user_agents.md"
    user_file.write_text("全局偏好：输出用中文。", encoding="utf-8")

    loader = InstructionsLoader(config={
        "enabled": True,
        "project_file": "AGENTS.md",
        "user_file": str(user_file),
        "max_chars": 20000,
    })
    text = loader.load(project_dir=str(tmp_path / "empty"))
    assert "全局偏好" in text


def test_no_agents_file_returns_empty(tmp_path):
    loader = InstructionsLoader()
    assert loader.load(project_dir=str(tmp_path)) == ""


def test_truncation(tmp_path):
    (tmp_path / "AGENTS.md").write_text("规则" * 5000, encoding="utf-8")
    loader = InstructionsLoader(config={
        "enabled": True,
        "project_file": "AGENTS.md",
        "user_file": str(tmp_path / "none.md"),
        "max_chars": 500,
    })
    text = loader.load(project_dir=str(tmp_path))
    assert len(text) <= 700
    assert "截断" in text


def test_cache_invalidation(tmp_path):
    f = tmp_path / "AGENTS.md"
    f.write_text("v1", encoding="utf-8")
    loader = InstructionsLoader()
    assert "v1" in loader.load(project_dir=str(tmp_path))

    # 修改文件后重新加载应拿到新内容
    f.write_text("v2 内容", encoding="utf-8")
    # 刷新 mtime 确保缓存失效（同秒写入可能 mtime 相同）
    os.utime(f, None)
    assert "v2" in loader.load(project_dir=str(tmp_path))


def test_disabled(tmp_path):
    (tmp_path / "AGENTS.md").write_text("规则", encoding="utf-8")
    loader = InstructionsLoader(config={"enabled": False, "project_file": "AGENTS.md",
                                        "user_file": "/none", "max_chars": 100})
    assert loader.load(project_dir=str(tmp_path)) == ""
