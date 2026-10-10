"""自我升级器（agent/upgrade.py）：只快进、脏工作区拒绝、离线可测。"""
import pytest

from agent.upgrade import UpgradeError, Upgrader


class FakeGit:
    """脚本化 git/pip：按子命令前缀匹配返回预设结果，并记录调用。

    head_after：模拟"拉取成功后 HEAD 变了"（真 git 会变，静态假件不会）。
    """

    def __init__(self, script=None, default=(0, "", ""), head_after=None):
        self.script = dict(script or {})
        self.default = default
        self.head_after = head_after
        self.pulled = False
        self.calls = []

    def __call__(self, cmd, cwd, timeout):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "pull" in joined:
            self.pulled = True
        if self.head_after and self.pulled and joined.endswith("rev-parse HEAD"):
            return 0, self.head_after + "\n", ""
        # 子串匹配、长键优先：`git pull --rebase` 不会被 `git pull` 抢走
        for key in sorted(self.script, key=len, reverse=True):
            if key in joined:
                return self.script[key]
        return self.default

    def ran(self, needle) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


def _upgrader(script=None, default=(0, "", ""), head_after=None):
    fake = FakeGit(script, default, head_after=head_after)
    logs = []
    up = Upgrader(repo=".", runner=fake, say=logs.append)
    return up, fake, logs


BASE = {
    "git rev-parse --is-inside-work-tree": (0, "true\n", ""),
    "git remote get-url": (0, "git@github.com:sadrood/my-agent.git\n", ""),
    "git status --porcelain": (0, "", ""),
    "git fetch": (0, "", ""),
    "git stash": (0, "", ""),
    "git pull": (0, "ok", ""),
}


class TestPreflight:
    def test_rejects_non_repo(self):
        up, _, _ = _upgrader({"git rev-parse --is-inside-work-tree": (1, "", "not a repo")})
        with pytest.raises(UpgradeError) as err:
            up.run()
        assert "不是 git 仓库" in str(err.value)

    def test_rejects_missing_remote(self):
        up, _, _ = _upgrader({**BASE, "git remote get-url": (2, "", "no such remote")})
        with pytest.raises(UpgradeError) as err:
            up.run()
        assert "未配置" in str(err.value)

    def test_can_be_disabled_by_config(self, monkeypatch):
        import agent.upgrade as mod
        monkeypatch.setattr(mod, "UPGRADE_CONFIG", {**mod.UPGRADE_CONFIG, "enabled": False},
                            raising=False)
        up, _, _ = _upgrader(BASE)
        with pytest.raises(UpgradeError):
            up.run()


class TestUpToDate:
    def test_no_pull_when_already_latest(self):
        script = {**BASE, "git rev-parse origin/main": (0, "abc123\n", ""),
                  "git rev-parse HEAD": (0, "abc123\n", "")}
        up, fake, _ = _upgrader(script)
        result = up.run()
        assert result.up_to_date is True and result.pulled is False
        assert not fake.ran("pull"), "已是最新时不该动仓库"
        assert "已是最新" in result.summary()

    def test_check_only_never_mutates(self):
        script = {**BASE, "git rev-parse origin/main": (0, "new99\n", ""),
                  "git rev-parse HEAD": (0, "old11\n", "")}
        up, fake, _ = _upgrader(script)
        result = up.run(check_only=True)
        assert result.pulled is False and result.new_head == "new99"
        assert not fake.ran("pull") and not fake.ran("stash")
        assert any("可升级" in n for n in result.notes)


class TestDirtyTree:
    def test_dirty_tree_is_refused_by_default(self):
        up, fake, _ = _upgrader({**BASE, "git status --porcelain": (0, " M main.py\n", "")})
        with pytest.raises(UpgradeError) as err:
            up.run()
        assert "未提交改动" in str(err.value) and "main.py" in str(err.value)
        assert not fake.ran("pull"), "有未提交改动时不能动仓库"

    def test_stash_flag_stashes_and_pops(self):
        script = {**BASE,
                  "git status --porcelain": (0, " M main.py\n", ""),
                  "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "0\n", "")}
        up, fake, logs = _upgrader(script)
        result = up.run(stash=True, install_deps=False)
        assert result.stashed is True and result.pulled is True
        assert fake.ran("stash push") and fake.ran("stash pop"), "改了就要还回去"
        assert any("恢复" in line for line in logs)

    def test_pull_failure_restores_the_stash(self):
        script = {**BASE,
                  "git status --porcelain": (0, " M main.py\n", ""),
                  "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "0\n", ""),
                  "git pull": (1, "", "网络炸了")}
        up, fake, _ = _upgrader(script)
        with pytest.raises(UpgradeError) as err:
            up.run(stash=True, install_deps=False)
        assert "网络炸了" in str(err.value)
        assert fake.ran("stash pop"), "拉取失败也要把用户的改动还回去"


class TestDiverged:
    def test_local_commits_are_refused_with_options(self):
        script = {**BASE, "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "3\n", "")}
        up, fake, _ = _upgrader(script)
        with pytest.raises(UpgradeError) as err:
            up.run(install_deps=False)
        msg = str(err.value)
        assert "3 个远端没有的提交" in msg and "rebase" in msg and "reset --hard" in msg
        assert not fake.ran("reset"), "永不替用户 reset --hard"

    def test_rebase_flag_uses_rebase(self):
        script = {**BASE, "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "2\n", ""),
                  "git pull --rebase": (0, "", "")}
        up, fake, _ = _upgrader(script)
        result = up.run(rebase=True, install_deps=False)
        assert result.pulled is True and fake.ran("pull --rebase")


class TestDepsAndConfig:
    def test_ff_only_pull_is_used_by_default(self):
        script = {**BASE, "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "0\n", "")}
        up, fake, _ = _upgrader(script)
        up.run(install_deps=False)
        assert fake.ran("pull --ff-only"), "默认必须走快进，不做合并提交"

    def test_dependency_install_is_reported(self):
        script = {**BASE, "git rev-parse HEAD": (0, "a\n", ""),
                  "git rev-parse origin/main": (0, "b\n", ""),
                  "git rev-list --count": (0, "0\n", "")}
        up, fake, _ = _upgrader(script)
        result = up.run()
        assert result.deps_ok is True
        assert any("pip" in " ".join(c) for c in fake.calls)

    def test_pip_failure_is_surfaced_without_rollback(self):
        script = {**BASE, "git rev-parse HEAD": (0, "a\n", ""),
                  "git rev-parse origin/main": (0, "b\n", ""),
                  "git rev-list --count": (0, "0\n", "")}
        up, fake, logs = _upgrader(script)
        fake.script["pip install"] = (1, "", "No matching distribution")
        result = up.run()
        assert result.deps_ok is False
        assert any("依赖安装失败" in line for line in logs)
        assert "依赖: 安装失败" in result.summary()

    def test_new_env_keys_are_listed(self):
        diff = ("+++ b/.env.example\n"
                "@@ -1,0 +2,4 @@\n"
                "+# 自我升级\n"
                "+UPGRADE_REMOTE=origin\n"
                "+UPGRADE_BRANCH=main\n"
                "+SOME_NEW_FEATURE_FLAG=true\n")
        script = {**BASE, "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "0\n", ""),
                  "git diff --unified=0 old..new -- .env.example": (0, diff, "")}
        up, _, _ = _upgrader(script, head_after="new")
        result = up.run(install_deps=False)
        assert result.new_env_keys == ["UPGRADE_REMOTE", "UPGRADE_BRANCH", "SOME_NEW_FEATURE_FLAG"]
        assert "UPGRADE_BRANCH" in result.summary()

    def test_env_file_hash_is_compared(self, tmp_path, monkeypatch):
        """升级器绝不写 .env：哈希不变才算没被动过。"""
        env = tmp_path / ".env"
        env.write_text("LLM_API_KEY=secret\n", encoding="utf-8")
        monkeypatch.setattr("agent.upgrade.resolve_under_root", lambda p: str(env))
        script = {**BASE, "git rev-parse HEAD": (0, "old\n", ""),
                  "git rev-parse origin/main": (0, "new\n", ""),
                  "git rev-list --count": (0, "0\n", "")}
        up, _, _ = _upgrader(script)
        result = up.run(install_deps=False)
        assert result.env_untouched is True
        assert env.read_text(encoding="utf-8") == "LLM_API_KEY=secret\n"


class TestCli:
    def test_main_reports_up_to_date(self, monkeypatch, capsys):
        from agent import upgrade as mod
        monkeypatch.setattr(mod, "Upgrader", lambda repo=None: type("U", (), {
            "run": lambda self, **kw: type("R", (), {
                "summary": lambda self: "已是最新（abc），没有需要做的事。",
                "up_to_date": True, "deps_ok": None})()})())
        assert mod.main(["--check"]) == 0
        assert "已是最新" in capsys.readouterr().out

    def test_main_returns_nonzero_on_error(self, monkeypatch, capsys):
        from agent import upgrade as mod

        class Boom:
            def __init__(self, repo=None):
                pass

            def run(self, **kw):
                raise UpgradeError("工作区有未提交改动")

        monkeypatch.setattr(mod, "Upgrader", Boom)
        assert mod.main([]) == 1
        assert "工作区有未提交改动" in capsys.readouterr().out

    def test_main_returns_two_when_deps_failed(self, monkeypatch, capsys):
        from agent import upgrade as mod

        class Partial:
            def __init__(self, repo=None):
                pass

            def run(self, **kw):
                r = mod.UpgradeResult()
                r.old_head, r.new_head, r.pulled, r.deps_ok = "a", "b", True, False
                return r

        monkeypatch.setattr(mod, "Upgrader", Partial)
        assert mod.main([]) == 2
