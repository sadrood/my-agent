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


class TestDomainPathContainment:
    """domain 必须落在经验仓**之内**（路径穿越回归）。

    实测漏洞（2026-09-22 审计）：旧实现是
    `os.path.join(path, "experiences", domain.replace("/", os.sep))` ——
    既不挡 `..`，也不挡绝对路径（`os.path.join` 遇绝对路径会丢弃前面的部分）。
    `domain: "C:/Windows/Temp/pwn"` 能把文件写到仓外，而随后的 `git add` 因为
    relpath 指向仓外而失败，工具报"保存失败"**却把文件留在了磁盘上**。
    """

    @pytest.mark.parametrize("domain", [
        "../../../../tmp/pwn", "..", "../..", "/etc/passwd",
        "C:/Windows/Temp/pwn", "..\\..\\..\\Windows",
        "....//....//x", "//server/share/x",
    ])
    def test_traversal_stays_inside(self, tmp_path, domain):
        from tools.experience import _safe_domain_dir
        repo = str(tmp_path / "repo")
        base = os.path.abspath(os.path.join(repo, "experiences"))
        target = os.path.abspath(_safe_domain_dir(repo, domain))
        assert target == base or target.startswith(base + os.sep), \
            f"{domain!r} 逃出了经验仓: {target}"

    def test_nested_domain_structure_preserved(self, tmp_path):
        """别为了安全把 `棋类/象棋` 这种正常分层压平。"""
        from tools.experience import _safe_domain_dir
        repo = str(tmp_path / "repo")
        rel = os.path.relpath(_safe_domain_dir(repo, "棋类/象棋"),
                              os.path.join(repo, "experiences"))
        assert rel == os.path.join("棋类", "象棋")

    def test_save_traversal_lands_in_repo(self, lib_repo):
        """端到端：越界 domain 仍要能正常提交，且文件留在仓内。"""
        repo, cache = lib_repo
        tool = ExperienceTool()
        res = tool.execute_json({
            "action": "save", "domain": "../../../../tmp/pwn", "topic": "越界尝试",
            "body": "教训：domain 不能带 .. 或绝对路径，否则会写到仓外。",
        })
        assert res.success is True, res.error
        ls = subprocess.run(["git", "-c", "core.quotePath=false",
                             "ls-tree", "-r", "--name-only", "HEAD"],
                            cwd=str(repo), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=True).stdout
        assert "experiences/" in ls
        assert ".." not in ls


class TestSecretScanCoverage:
    """密钥扫描必须覆盖 topic / domain，且认得带连字符的 key。

    实测漏洞（2026-09-22 审计）：扫描串是 `body + tags`，**topic 与 domain 不扫**
    —— 而 topic 会写进 frontmatter 和 git commit message，把 key 写在 topic 里就
    明文推上远端仓；另外 `sk-[A-Za-z0-9]{16,}` 匹配不了 `sk-ant-api03-…` 这类
    带连字符的形态。
    """

    @pytest.mark.parametrize("field", ["topic", "domain"])
    def test_secret_in_topic_or_domain_rejected(self, lib_repo, field):
        tool = ExperienceTool()
        args = {"action": "save", "domain": "ops", "topic": "正常标题",
                "body": "教训：轮换即可，不要写真实值。"}
        args[field] = "修复 sk-abcdefghijklmnopqrstuvwxyz123456 泄漏"
        if field == "domain":
            args[field] = "sk-abcdefghijklmnopqrstuvwxyz123456"
        r = tool.execute_json(args)
        assert r.success is False, f"{field} 里的密钥没被拦下"
        assert "密钥" in r.error or "凭据" in r.error

    def test_hyphenated_key_rejected(self, lib_repo):
        tool = ExperienceTool()
        r = tool.execute_json({
            "action": "save", "domain": "ops", "topic": "正常标题",
            "body": "教训：见 sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz012345 。",
        })
        assert r.success is False
        assert "密钥" in r.error or "凭据" in r.error

    def test_normal_text_not_flagged(self, lib_repo):
        """别把正常文本当密钥拦了。"""
        tool = ExperienceTool()
        r = tool.execute_json({
            "action": "save", "domain": "ops", "topic": "sk- 前缀的来历",
            "body": "教训：token 要定期轮换，但经验里绝不写真实值。",
        })
        assert r.success is True, r.error

