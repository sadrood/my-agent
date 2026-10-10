"""自我升级：把"拉取新版本 → 装依赖 → 自检"做成一条命令。

默认安全策略（都可用参数放宽，但绝不默认做破坏性动作）：
- 只走 `git pull --ff-only`，**永不** reset --hard / force；
- 工作区有未提交改动时**拒绝升级**（要升就显式 `--stash`，升完自动 pop）；
- 本地提交与远端分叉时拒绝并给出处理办法（或显式 `--rebase`）；
- 升级前记下 `.env` 的哈希、升级后比对，证明你的密钥文件没被动过；
- 升级后列出**新增的配置项**（.env.example 的 diff），并提示重启服务。

入口：`python -m agent.upgrade [--check]`，或 `my-agent --upgrade`，或会话内 `/upgrade`。
"""
import hashlib
import os
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from config import UPGRADE_CONFIG, resolve_under_root

#: 跑外部命令的默认实现：(返回码, stdout, stderr)。测试注入替身即可完全离线。
Runner = Callable[[List[str], str, float], Tuple[int, str, str]]


def _default_runner(cmd: List[str], cwd: str, timeout: float) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "", f"找不到命令: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（>{timeout:.0f}s）: {' '.join(cmd)}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


class UpgradeError(Exception):
    """升级被拒绝或失败（消息直接给用户看）。"""


class UpgradeResult:
    def __init__(self):
        self.old_head = ""
        self.new_head = ""
        self.up_to_date = False
        self.pulled = False
        self.stashed = False
        self.deps_ok: Optional[bool] = None
        self.env_untouched = True
        self.new_env_keys: List[str] = []
        self.notes: List[str] = []

    def summary(self) -> str:
        if self.up_to_date:
            return f"已是最新（{self.old_head[:8] if self.old_head else '?'}），没有需要做的事。"
        lines = [f"版本: {self.old_head[:8]} → {self.new_head[:8]}"]
        if self.pulled:
            lines.append("代码: 已快进更新")
        if self.stashed:
            lines.append("未提交改动: 已 stash 并恢复")
        if self.deps_ok is True:
            lines.append("依赖: requirements.txt 已安装")
        elif self.deps_ok is False:
            lines.append("依赖: 安装失败（见上方输出）")
        lines.append("配置: .env 未被改动" if self.env_untouched else "配置: ⚠ .env 发生变化")
        if self.new_env_keys:
            lines.append("新增配置项（可补进 .env，不补也有默认值）: " + ", ".join(self.new_env_keys))
        lines.extend(self.notes)
        lines.append("下一步: 重启你的服务（systemd/pm2/nohup）让新代码生效。")
        return "\n".join(lines)


class Upgrader:
    """一次升级流程；所有外部动作用 runner 注入，便于离线测试。"""

    def __init__(self, repo: Optional[str] = None, runner: Optional[Runner] = None,
                 say: Callable[[str], None] = print):
        self.repo = repo or str(UPGRADE_CONFIG.get("repo_dir") or os.getcwd())
        self.runner = runner or _default_runner
        self.say = say
        self.remote = str(UPGRADE_CONFIG.get("remote") or "origin")
        self.branch = str(UPGRADE_CONFIG.get("branch") or "main")
        self.timeout = float(UPGRADE_CONFIG.get("timeout_seconds") or 300)

    # ---------------- 基础 ----------------

    def git(self, *args: str) -> Tuple[int, str, str]:
        return self.runner(["git", *args], self.repo, self.timeout)

    def _head(self, ref: str = "HEAD") -> str:
        code, out, _ = self.git("rev-parse", ref)
        return out.strip() if code == 0 else ""

    def _tracked_dirty(self) -> List[str]:
        code, out, _ = self.git("status", "--porcelain", "--untracked-files=no")
        if code != 0:
            raise UpgradeError(f"无法读取仓库状态：{out.strip()[:200]}")
        return [ln for ln in out.splitlines() if ln.strip()]

    # ---------------- 前置检查 ----------------

    def preflight(self) -> None:
        if not UPGRADE_CONFIG.get("enabled", True):
            raise UpgradeError("升级功能已关闭（UPGRADE_ENABLED=false）。")
        code, _, err = self.git("rev-parse", "--is-inside-work-tree")
        if code != 0:
            raise UpgradeError(f"{self.repo} 不是 git 仓库，无法升级（{err.strip()[:120]}）。")
        code, out, _ = self.git("remote", "get-url", self.remote)
        if code != 0 or not out.strip():
            raise UpgradeError(f"远端 {self.remote} 未配置：先 git remote add {self.remote} <url>。")

    # ---------------- 主流程 ----------------

    def run(self, check_only: bool = False, stash: bool = False, rebase: bool = False,
            install_deps: bool = True) -> UpgradeResult:
        result = UpgradeResult()
        self.preflight()
        result.old_head = self._head() or "unknown"
        env_path = resolve_under_root(".env")
        env_before = _file_hash(env_path)
        dirty = self._tracked_dirty()
        if dirty and not check_only:
            if not stash:
                raise UpgradeError(
                    "工作区有未提交改动，拒绝升级（避免把你的改动卷进 pull）：\n  "
                    + "\n  ".join(dirty[:8])
                    + "\n处理：先 git stash / git commit，或用 --stash 让升级器自动 stash→pop。")
            self.say(f"· 暂存 {len(dirty)} 个未提交改动（升级后自动恢复）")
            code, _, err = self.git("stash", "push", "-u", "-m", f"pre-upgrade-{int(time.time())}")
            if code != 0:
                raise UpgradeError(f"git stash 失败：{err.strip()[:200]}")
            result.stashed = True

        self.say(f"· 拉取 {self.remote}/{self.branch} 的最新信息")
        code, _, err = self.git("fetch", "--prune", self.remote, self.branch)
        if code != 0:
            raise UpgradeError(f"git fetch 失败（网络或权限？）：{err.strip()[:200]}")
        target = f"{self.remote}/{self.branch}"
        result.new_head = self._head(target)

        if result.old_head == result.new_head:
            result.up_to_date = True
            if result.stashed:
                self._pop(result)
            return result
        if check_only:
            result.notes.append(f"可升级：git pull --ff-only {self.remote} {self.branch}")
            return result

        # 分叉检测：本地有远端没有的提交 → 快进不可能
        code, ahead, _ = self.git("rev-list", "--count", f"{target}..HEAD")
        if code == 0 and ahead.strip().isdigit() and int(ahead.strip()) > 0:
            if not rebase:
                if result.stashed:
                    self._pop(result, quiet=True)
                raise UpgradeError(
                    f"本地有 {ahead.strip()} 个远端没有的提交，无法快进升级。\n"
                    f"选择：① 保留本地提交：git pull --rebase {self.remote} {self.branch}（或用 --rebase）\n"
                    f"      ② 放弃本地提交：先 git branch backup-$(date +%F) 再 git reset --hard {target}\n"
                    "升级器不会替你选，避免丢东西。")
            self.say(f"· 本地领先 {ahead.strip()} 个提交，用 rebase 方式升级")
            code, out, err = self.git("pull", "--rebase", self.remote, self.branch)
        else:
            code, out, err = self.git("pull", "--ff-only", self.remote, self.branch)

        if code != 0:
            if result.stashed:
                self._pop(result, quiet=True)
            raise UpgradeError(f"git pull 失败：{(err or out).strip()[:300]}")
        result.pulled = True
        result.new_head = self._head() or result.new_head
        self.say(f"· 代码已更新到 {result.new_head[:8]}{'（rebase）' if rebase else ''}")

        if install_deps:
            result.deps_ok = self._install_deps()
        result.new_env_keys = self._new_env_keys(result.old_head, result.new_head)
        result.env_untouched = _file_hash(env_path) == env_before
        if result.stashed:
            self._pop(result)
        return result

    # ---------------- 子步骤 ----------------

    def _pop(self, result: UpgradeResult, quiet: bool = False) -> None:
        code, out, err = self.git("stash", "pop")
        if code != 0:
            raise UpgradeError(
                "代码已更新，但 git stash pop 失败（可能冲突）：请手工处理 stash，"
                f"你的改动在 git stash list 里。\n{(err or out).strip()[:200]}")
        if not quiet:
            self.say("· 未提交改动已恢复")

    def _install_deps(self) -> bool:
        req = resolve_under_root("requirements.txt")
        if not os.path.isfile(req):
            self.say("· 没有 requirements.txt，跳过依赖安装")
            return True
        self.say("· 安装依赖（pip install -r requirements.txt）")
        code, out, err = self.runner([sys.executable, "-m", "pip", "install", "-r", req],
                                     self.repo, max(self.timeout, 900))
        if code != 0:
            self.say((err or out).strip()[-500:])
            self.say("· 依赖安装失败：升级后的代码可能缺包，可手动重跑 pip install -r requirements.txt")
            return False
        return True

    def _new_env_keys(self, old: str, new: str) -> List[str]:
        """.env.example 里新增的 KEY=（你的 .env 不会被 pull 改动，需要手动补）。"""
        if not old or not new or old == new:
            return []
        code, out, _ = self.git("diff", "--unified=0", f"{old}..{new}", "--", ".env.example")
        if code != 0:
            return []
        keys: List[str] = []
        for line in out.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                body = line[1:].strip()
                if body.startswith("#") or "=" not in body:
                    continue
                key = body.split("=", 1)[0].strip()
                if key and key not in keys:
                    keys.append(key)
        return keys


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m agent.upgrade",
                                     description="安全地升级到远端最新版本")
    parser.add_argument("--check", action="store_true", help="只检查有没有新版本，不改动任何东西")
    parser.add_argument("--stash", action="store_true", help="自动 stash 未提交改动，升级后恢复")
    parser.add_argument("--rebase", action="store_true", help="本地有提交时用 rebase 方式升级")
    parser.add_argument("--no-deps", action="store_true", help="跳过 pip install -r requirements.txt")
    parser.add_argument("--repo", default=None, help="仓库目录（默认取 UPGRADE_REPO_DIR 或当前目录）")
    args = parser.parse_args(argv)

    up = Upgrader(repo=args.repo)
    try:
        result = up.run(check_only=args.check, stash=args.stash, rebase=args.rebase,
                        install_deps=not args.no_deps)
    except UpgradeError as e:
        print(f"升级未完成：{e}")
        return 1
    print(result.summary())
    if not result.up_to_date and result.deps_ok is False:
        return 2
    return 0


if __name__ == "__main__":      # pragma: no cover - 命令入口
    raise SystemExit(main())
