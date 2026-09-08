"""
Repo Map 测试（仓库结构摘要注入）。
"""
from agent.repomap import build_repo_map, get_repo_map_for_goal, looks_like_code_task


class TestLooksLikeCodeTask:
    def test_code_keywords(self):
        assert looks_like_code_task("修复 agent 的 bug") is True
        assert looks_like_code_task("优化代码") is True
        assert looks_like_code_task("跑 pytest 测试") is True
        assert looks_like_code_task("用python计算1+1") is False   # 琐碎任务不注入
        assert looks_like_code_task("写一首诗") is False


class TestBuildRepoMap:
    def test_tree_with_line_counts(self, tmp_path):
        (tmp_path / "main.py").write_text("a\nb\nc\n", encoding="utf-8")
        (tmp_path / "agent").mkdir()
        (tmp_path / "agent" / "core.py").write_text("x\n" * 10, encoding="utf-8")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "junk.py").write_text("junk", encoding="utf-8")
        (tmp_path / ".git").mkdir()

        text = build_repo_map(str(tmp_path))
        assert "main.py（3 行）" in text
        assert "core.py（10 行）" in text
        assert "junk.py" not in text        # .venv 跳过
        assert ".git" not in text

    def test_truncation(self, tmp_path):
        for i in range(30):
            (tmp_path / f"f{i:02d}.py").write_text("x\n" * 5, encoding="utf-8")
        text = build_repo_map(str(tmp_path), max_chars=200)
        assert len(text) <= 230
        assert "截断" in text

    def test_empty_dir(self, tmp_path):
        text = build_repo_map(str(tmp_path))
        assert text == f"{tmp_path.name}/"


class TestRepoMapForGoal:
    def test_code_goal_includes_map(self, tmp_path):
        (tmp_path / "a.py").write_text("code", encoding="utf-8")
        text = get_repo_map_for_goal("修复代码", str(tmp_path))
        assert "a.py" in text

    def test_trivial_goal_skips_map(self, tmp_path):
        assert get_repo_map_for_goal("算个 1+1", str(tmp_path)) == ""
