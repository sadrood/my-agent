"""
Git 快照模块：Agent 自我修改代码前的安全网。

- ensure_repo(): 项目不是 git 仓库时自动 git init + 首次提交（基线）
- snapshot(): 每次运行开始前形成回滚点（full=True 全量 add -A；
  full=False 选择性模式，只提交暂存区——逐工具 checkpoint 已覆盖
  Agent 自身修改时用，避免把用户并行未提交工作卷进 Agent 提交）
- checkpoint(): 逐操作检查点，提供 changed_file 时只提交该文件（归因提交）
  提交信息形如 "snapshot: 运行前快照 <时间> — <目标摘要>"

设计原则：
- 静默失败：任何 git 问题都不影响 Agent 正常运行（只打警告）
- 归因提交：Agent 的提交只包含 Agent 改动的文件，并行工作不卷入
- 身份兜底：git 未配置 user.name 时使用 my-agent 身份提交
"""
import os
import subprocess
from datetime import datetime
from typing import Optional

SNAPSHOT_AUTHOR_NAME = "my-agent"
SNAPSHOT_AUTHOR_EMAIL = "my-agent@local"


def _git(args: list, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """执行 git 命令（静默 stdout）。"""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def is_git_repo(path: str) -> bool:
    """判断 path 是否在 git 仓库内。"""
    try:
        result = _git(["rev-parse", "--is-inside-work-tree"], path)
        return result.returncode == 0 and "true" in result.stdout
    except Exception:
        return False


def ensure_repo(path: str) -> bool:
    """
    确保 path 是 git 仓库；不是则 init 并做首次提交（基线快照）。

    Returns:
        True = 已就绪（原本就是仓库 / 或初始化成功）
    """
    if is_git_repo(path):
        return True
    try:
        init = _git(["init", "-b", "main"], path, timeout=60)
        if init.returncode != 0:
            # 老版本 git 不支持 -b：回退普通 init
            init = _git(["init"], path, timeout=60)
        if init.returncode != 0:
            return False
        # 首次提交（空仓库允许空提交作为基线）
        _commit(path, "chore: 初始化仓库基线（my_agent 自我升级安全网）", allow_empty=True)
        return is_git_repo(path)
    except Exception:
        return False


def _commit(path: str, message: str, allow_empty: bool = False,
            add_paths=None) -> bool:
    """commit（身份缺失时用 my-agent 兜底）。

    Args:
        add_paths: None = git add -A 全量快照（旧行为）；
                   非空 list = 只 add 指定路径（Agent 归因提交，不卷入并行工作）；
                   空 list = 不执行 add，只提交当前暂存区。
    """
    if add_paths is None:
        add = _git(["add", "-A"], path)
        if add.returncode != 0:
            return False
    elif add_paths:
        add = _git(["add", "--"] + [str(p) for p in add_paths], path)
        if add.returncode != 0:
            return False
    cmd = ["commit"] + (["--allow-empty"] if allow_empty else []) + ["-m", message]
    commit = _git(cmd, path, timeout=120)
    if commit.returncode == 0:
        return True
    # 身份未配置 → 兜底身份重试
    if "identity" in commit.stderr.lower() or "user.name" in commit.stderr.lower():
        commit = _git(
            ["-c", f"user.name={SNAPSHOT_AUTHOR_NAME}",
             "-c", f"user.email={SNAPSHOT_AUTHOR_EMAIL}",
             "commit"] + (["--allow-empty"] if allow_empty else []) + ["-m", message],
            path, timeout=120,
        )
        return commit.returncode == 0
    return False


def snapshot(path: str, goal: str = "", full: bool = True) -> bool:
    """
    运行前快照：形成回滚点。

    Args:
        goal: 目标摘要（写入提交信息）
        full: True = 全量快照（git add -A，旧行为）。适用于未开启逐工具
              checkpoint 的场景——此时它是唯一的回滚安全网。
              False = 选择性快照（不主动 add，只提交暂存区已有的改动）。
              逐工具 checkpoint 已把 Agent 的每次修改独立提交，这里再全量
              add -A 只会把用户的并行未提交工作卷进 Agent 提交。

    Returns:
        True = 快照成功（或无需提交）
    """
    if not is_git_repo(path):
        return False

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    goal_summary = (goal or "").strip().replace("\n", " ")[:60]
    message = f"snapshot: 运行前快照 {ts}" + (f" — {goal_summary}" if goal_summary else "")
    return _commit_or_clean(path, message, add_paths=None if full else [])


def checkpoint(path: str, label: str = "", changed_file: str = "") -> str:
    """
    工具级检查点（逐操作 checkpoint）：
    在修改文件的操作（edit / file write）执行前提交当前状态，
    使每次自我修改都可逐操作回滚。

    Args:
        label: 提交信息（工具名 + 文件）
        changed_file: 本次修改的文件路径。提供时只提交该文件（Agent 归因
                      提交），仓库里其他未提交改动（如用户并行工作）保持
                      不动；留空时退回全量 add -A（向后兼容）。

    Returns:
        成功返回该检查点的 commit hash（无实际提交——改动为空——时返回
        HEAD hash，状态与检查点一致）；失败返回 ""。
    """
    if not is_git_repo(path):
        return ""
    ts = datetime.now().strftime("%H:%M:%S")
    message = f"checkpoint: {ts} {label}".strip()[:200]
    if not _commit_or_clean(path, message, add_paths=[changed_file] if changed_file else None):
        return ""
    head = _git(["rev-parse", "HEAD"], path)
    return head.stdout.strip() if head.returncode == 0 else ""


def rollback_to(path: str, commit: str) -> str:
    """
    回滚工作区到某个历史提交的树状态。借鉴同类实现的 checkpoint 恢复，
    但**不重写历史**：把 commit 与 HEAD 之间所有差异文件按类型恢复/移除
    （hash 之后新增的文件删除、修改/删除的文件恢复内容），再作为新提交
    落库——旧提交全部保留，之后仍可回滚到任何更早的检查点。

    Args:
        path: 仓库路径
        commit: 目标提交 hash（必须是 HEAD 的祖先）

    Returns:
        成功返回回滚提交后的 HEAD hash（无差异时返回原 HEAD）；失败返回 ""。
    """
    if not is_git_repo(path):
        return ""
    commit = str(commit or "").strip()
    if not commit:
        return ""
    # 目标提交必须存在且是 HEAD 祖先（防止回滚到无关/未来提交）
    if _git(["merge-base", "--is-ancestor", commit, "HEAD"], path).returncode != 0:
        return ""
    diff = _git(["diff", "--name-status", "--no-renames", commit, "HEAD"], path)
    if diff.returncode != 0:
        return ""
    touched: list = []
    for line in diff.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, rel = parts[0].strip(), parts[1].strip()
        target = os.path.join(path, rel)
        if status == "A":
            # commit 之后新增的文件 → 移除
            try:
                if os.path.isfile(target):
                    os.remove(target)
                    touched.append(rel)
            except Exception:
                return ""
        else:
            # M/D/T → 从 commit 恢复内容（同时写入暂存区与工作区，含被删文件）
            rec = _git(["checkout", commit, "--", rel], path)
            if rec.returncode != 0:
                return ""
            touched.append(rel)
    if not touched:
        return _head(path)
    # 只提交本次触碰的路径（归因提交，不卷入用户并行未提交工作）
    if _commit(path, f"rollback: 回滚到检查点 {commit[:10]}（my-agent）", add_paths=touched):
        return _head(path)
    return ""


def _head(path: str) -> str:
    """当前 HEAD hash；失败返回空串。"""
    r = _git(["rev-parse", "HEAD"], path)
    return r.stdout.strip() if r.returncode == 0 else ""


def _commit_or_clean(path: str, message: str, add_paths=None) -> bool:
    """提交；"nothing to commit"（无待提交内容）同样视为成功。

    选择性模式（add_paths=[]）下暂存区为空 = 没有 Agent 改动需要保护，
    工作区里的并行未提交改动不属于 Agent，直接跳过视为成功。
    """
    if _commit(path, message, add_paths=add_paths):
        return True
    try:
        if add_paths is not None and not add_paths:
            r = _git(["diff", "--cached", "--quiet"], path)
            return r.returncode == 0   # 0=暂存区干净 → 无需提交
        r = _git(["status", "--porcelain"], path)
        return r.returncode == 0 and not r.stdout.strip()
    except Exception:
        return False


def has_pending_changes(path: str) -> Optional[bool]:
    """是否有未提交改动（失败返回 None）。"""
    try:
        result = _git(["status", "--porcelain"], path)
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())
    except Exception:
        return None
