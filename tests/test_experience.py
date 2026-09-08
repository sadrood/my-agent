"""云经验库工具测试：save→search 闭环（本地 git 仓）、密钥门禁、未配置提示。"""
import os
import subprocess

import pytest

from tools.experience import ExperienceTool
from config import EXPERIENCE_CONFIG


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True,
                   env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


@pytest.fixture()
def lib_repo(tmp_path, monkeypatch):
    """建本地 bare 仓库模拟 GitHub 经验仓（bare 才允许 push）。"""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    (work / "README.md").write_text("# 经验库\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    hub = tmp_path / "hub.git"
    _git(tmp_path, "clone", "--bare", str(work), str(hub))
    cache = tmp_path / "cache"
    monkeypatch.setitem(EXPERIENCE_CONFIG, "private_repo", str(hub))
    monkeypatch.setitem(EXPERIENCE_CONFIG, "public_repo", "")
    monkeypatch.setitem(EXPERIENCE_CONFIG, "cache_dir", str(cache))
    monkeypatch.setitem(EXPERIENCE_CONFIG, "pull_ttl_sec", "3600")
    return hub, cache


def test_save_then_search_roundtrip(lib_repo):
    repo, cache = lib_repo
    tool = ExperienceTool()
    res = tool.execute_json({
        "action": "save",
        "domain": "棋类/象棋",
        "topic": "中炮对屏风马开局心得",
        "body": "经过：中炮急进中兵被屏风马平炮兑车反制。\n教训：过河兵要配合左车，孤军深入必丢子。\n可复用：开局前先补士象再进中兵。",
        "tags": ["象棋", "开局"],
    })
    assert res.success is True, res.error
    assert "已上传" in res.output and "commit" in res.output
    # 文件确实进了远程仓（bare 里直接看对象树）
    ls = subprocess.run(["git", "-c", "core.quotePath=false",
                         "ls-tree", "-r", "--name-only", "HEAD"],
                        cwd=str(repo), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", check=True).stdout
    assert "experiences/棋类/象棋/" in ls and ls.strip().endswith(".md")

    # search 命中（私有仓缓存克隆会先 clone 到 cache）
    r2 = tool.execute_json({"action": "search", "query": "象棋 屏风马 开局"})
    assert r2.success is True, r2.error
    assert "中炮对屏风马" in r2.output or "经验库命中" in r2.output


def test_search_no_match_reports_gracefully(lib_repo):
    repo, cache = lib_repo
    tool = ExperienceTool()
    r = tool.execute_json({"action": "search", "query": "量子物理超弦理论"})
    assert r.success is True
    assert "无匹配" in r.output


def test_save_rejects_secret(lib_repo):
    repo, cache = lib_repo
    tool = ExperienceTool()
    r = tool.execute_json({
        "action": "save", "domain": "测试", "topic": "带密钥",
        "body": "教训：不要把 sk-AbCdEfGh1234567890abcdefghijklmn 写进经验。",
    })
    assert r.success is False
    assert "密钥" in r.error or "凭据" in r.error


def test_save_without_private_repo_configured(monkeypatch, tmp_path):
    monkeypatch.setitem(EXPERIENCE_CONFIG, "private_repo", "")
    tool = ExperienceTool()
    r = tool.execute_json({"action": "save", "domain": "x", "topic": "y", "body": "z" * 50})
    assert r.success is False
    assert "EXPERIENCE_PRIVATE_REPO" in r.error


def test_search_without_any_repo(monkeypatch, tmp_path):
    monkeypatch.setitem(EXPERIENCE_CONFIG, "private_repo", "")
    monkeypatch.setitem(EXPERIENCE_CONFIG, "public_repo", "")
    tool = ExperienceTool()
    r = tool.execute_json({"action": "search", "query": "象棋"})
    assert r.success is True
    assert "无匹配" in r.output or "EXPERIENCE_PRIVATE_REPO" in r.output
