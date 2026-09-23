"""
Git 快照模块：Agent 自我修改代码前的安全网。

- ensure_repo(): 项目不是 git 仓库时自动 git init + 首次提交（基线）
- snapshot(): 每次运行开始前形成回滚点（full=True 全量 add -A；
  full=False 选择性模式，只提交暂存区——逐工具 checkpoint 已覆盖
  Agent 自身修改时用，避免把用户并行未提交工作卷进 Agent 提交）
- checkpoint(): 逐操作检查点，提供 changed_file 时只快照该文件（归因提交）。
  **快照挂到 refs/snapshots/<时间戳>，不进任何分支历史**——早先直接
  `git commit` 的实现会让每次自我修改都在 main 上留一条 "checkpoint: …"，
  一次 git push 就把几十条流水账推到远端（2026-09-17 实测踩到）。
  快照仍然可回滚（rollback_to 接受快照 ref 上的提交）。

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


def _git(args: list, cwd: str, timeout: int = 30,
         env: dict = None) -> subprocess.CompletedProcess:
    """执行 git 命令（静默 stdout）。env 用于 GIT_INDEX_FILE 临时索引场景。"""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=full_env,
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
    pathspec = [str(p) for p in add_paths] if add_paths else []
    if add_paths is None:
        add = _git(["add", "-A"], path)
        if add.returncode != 0:
            return False
    elif pathspec:
        # -A：路径被删除时也要把"删除"暂存下来（回滚会删掉快照之后新增的文件）
        add = _git(["add", "-A", "--"] + pathspec, path)
        if add.returncode != 0:
            # 已删除且从未进过索引的路径会让 pathspec 匹配失败（快照走临时索引，
            # 不碰真实索引）：存在的照常 add，不存在的用 update-index 确保移除
            existing = [p for p in pathspec if os.path.exists(os.path.join(path, p))]
            if existing:
                _git(["add", "-A", "--"] + existing, path)
            for miss in [p for p in pathspec if p not in existing]:
                _git(["update-index", "--force-remove", "--", miss], path)
    # commit 也必须带同样的 pathspec：不带的话 `git commit` 提交的是**整个索引**，
    # 会把这之前用户自己 `git add` 过、还没提交的文件一起卷进 agent 的归因提交
    # （2026-09-22 审计实测：用户 `git add user_wip.txt` 后回滚一次，那个文件就跟着
    # agent 的 "rollback: 回滚到检查点 …（my-agent）" 一起被提交了）。
    # 上面那句"只提交本次触碰的路径，不卷入用户并行未提交工作"的注释，靠的就是这里。
    suffix = (["--allow-empty"] if allow_empty else []) + ["-m", message]
    if pathspec:
        # 归因模式：只提交本次触碰的路径。两道过滤都关键：
        #   · 带一个不在索引里的路径（刚删掉、且从未进过索引）会让
        #     `git commit -- <path>` 直接报 pathspec 错误，把整次提交带崩 ——
        #     回滚会因此"文件恢复了却没落库"（2026-09-22 验证时踩到）；
        #   · 过滤后为空时**绝不能**退化成"不带 pathspec 的 commit" —— 那提交的是
        #     整个真实索引，会把用户自己 `git add` 的文件卷进 agent 的提交。
        staged = set((_git(["diff", "--cached", "--name-only"], path).stdout or "").splitlines())
        only = [p for p in pathspec if p in staged]
        if not only:
            return True                     # 指定路径没有可提交的变化，不是失败
        suffix += ["--"] + only
    cmd = ["commit"] + suffix
    commit = _git(cmd, path, timeout=120)
    if commit.returncode == 0:
        return True
    # 身份未配置 → 兜底身份重试
    if "identity" in commit.stderr.lower() or "user.name" in commit.stderr.lower():
        commit = _git(
            ["-c", f"user.name={SNAPSHOT_AUTHOR_NAME}",
             "-c", f"user.email={SNAPSHOT_AUTHOR_EMAIL}",
             "commit"] + suffix,
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


#: 逐操作快照的 ref 前缀（不属于任何分支，push 不会带上）
SNAPSHOT_REF_PREFIX = "refs/snapshots"


def snapshot_ref(ts: str = None) -> str:
    """本次快照的 ref 名：refs/snapshots/<YYYYmmdd-HHMMSS-ffffff>。

    带微秒是必需的：同一秒内连续两次 checkpoint（真实场景很常见）若同名，
    后一次会把前一次的 ref 覆盖掉，前一个快照就再也回滚不了（实测踩到）。
    """
    stamp = ts or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return f"{SNAPSHOT_REF_PREFIX}/{stamp}"


def _commit_to_ref(path: str, ref: str, message: str, add_paths=None) -> str:
    """把改动做成提交并挂到 ref 上，**不动 HEAD / 暂存区 / 工作区**。

    用临时索引（GIT_INDEX_FILE）+ commit-tree 实现，所以主分支历史保持干净，
    但快照对象仍在（可 diff、可回滚）。无改动（树与 HEAD 相同）时返回 ""。
    """
    import tempfile
    fd, index_file = tempfile.mkstemp(prefix="my-agent-index-")
    os.close(fd)
    env = {"GIT_INDEX_FILE": index_file}
    try:
        head = _git(["rev-parse", "--verify", "HEAD"], path, env=env)
        parent_sha = head.stdout.strip() if head.returncode == 0 else ""
        parent_tree = ""
        if parent_sha:
            pt = _git(["rev-parse", "HEAD^{tree}"], path, env=env)
            parent_tree = pt.stdout.strip() if pt.returncode == 0 else ""
            base = _git(["read-tree", "HEAD"], path, env=env)
        else:
            base = _git(["read-tree", "--empty"], path, env=env)
        if base.returncode != 0:
            return ""
        if add_paths is None:
            staged = _git(["add", "-A"], path, env=env)
        elif add_paths:
            staged = _git(["add", "-A", "--"] + [str(x) for x in add_paths], path, env=env)
        else:
            staged = _git(["add", "-u"], path, env=env)
        if staged.returncode != 0:
            return ""
        tree = _git(["write-tree"], path, env=env)
        if tree.returncode != 0:
            return ""
        tree_sha = tree.stdout.strip()
        if parent_tree and tree_sha == parent_tree:
            return ""                      # 与 HEAD 树一致 → 无需快照
        cmd = ["commit-tree", tree_sha] + (["-p", parent_sha] if parent_sha else []) \
            + ["-m", message]
        commit = _git(cmd, path, timeout=120, env=env)
        if commit.returncode != 0:
            commit = _git(["-c", f"user.name={SNAPSHOT_AUTHOR_NAME}",
                           "-c", f"user.email={SNAPSHOT_AUTHOR_EMAIL}"] + cmd,
                          path, timeout=120, env=env)
        if commit.returncode != 0:
            return ""
        sha = commit.stdout.strip()
        if not sha:
            return ""
        # ref 唯一性兜底：万一撞名（同微秒/自定义 ts），追加序号而不是覆盖旧快照
        final_ref, n = ref, 1
        while _git(["rev-parse", "--verify", "--quiet", final_ref],
                   path).returncode == 0:
            final_ref = f"{ref}-{n}"
            n += 1
            if n > 50:
                return ""
        if _git(["update-ref", final_ref, sha], path).returncode != 0:
            return ""
        return sha
    finally:
        try:
            os.remove(index_file)
        except OSError:
            pass


def list_snapshots(path: str, limit: int = 20) -> list:
    """列出本地逐操作快照（新→旧）：[(ref, hash, subject), ...]。"""
    r = _git(["for-each-ref", "--sort=-refname",
              "--format=%(refname)%09%(objectname)%09%(subject)",
              SNAPSHOT_REF_PREFIX], path)
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return out[:limit]


def _newest_snapshot(path: str) -> str:
    """最新的逐操作快照 hash（ref 名带时间戳，倒序取第一条）。"""
    snaps = list_snapshots(path, limit=1)
    return snaps[0][1] if snaps else ""


def _is_snapshot_commit(path: str, commit: str) -> bool:
    """该 commit 是否挂在本仓库 refs/snapshots/* 上（非分支历史里的提交）。"""
    r = _git(["for-each-ref", "--contains", commit,
              "--format=%(refname)", SNAPSHOT_REF_PREFIX], path)
    return r.returncode == 0 and SNAPSHOT_REF_PREFIX in (r.stdout or "")


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
    # 快照挂 refs/snapshots/<时间戳>：可回滚，但不进 main 历史（push 不会带上）
    sha = _commit_to_ref(path, snapshot_ref(), message,
                         add_paths=[changed_file] if changed_file else None)
    if sha:
        return sha
    # 无改动（或快照失败）→ 返回当前 HEAD，语义与旧实现"无提交时返回 HEAD"一致
    head = _git(["rev-parse", "HEAD"], path)
    return head.stdout.strip() if head.returncode == 0 else ""


def rollback_to(path: str, commit: str) -> str:
    """
    回滚工作区到某个历史提交或**逐操作快照**的树状态。不重写历史：
    把目标与"当前状态"之间的差异按类型恢复/移除（新增的删除、修改/删除的
    恢复内容），再作为新提交落库——旧提交与旧快照全部保留。

    "当前状态"的选择很关键：分支提交（HEAD 祖先）用 HEAD；逐操作快照用
    **最新快照**——因为快照不再推进 HEAD（见 _commit_to_ref），HEAD 停在
    最后一次真实提交上，拿它当基准会算错差异。

    Args:
        path: 仓库路径
        commit: 目标提交 hash（HEAD 祖先，或 refs/snapshots/* 上的快照）

    Returns:
        成功返回回滚提交后的 HEAD hash（无差异时返回原 HEAD）；失败返回 ""。
    """
    if not is_git_repo(path):
        return ""
    commit = str(commit or "").strip()
    if not commit:
        return ""
    # 目标提交必须是 HEAD 祖先，或挂在 refs/snapshots/* 上的逐操作快照
    is_ancestor = _git(["merge-base", "--is-ancestor", commit, "HEAD"],
                       path).returncode == 0
    if not is_ancestor and not _is_snapshot_commit(path, commit):
        return ""
    # 与**当前工作区**比较（`git diff <commit>` 不带第二个 ref 就是这个语义）：
    # 不再拿"最新快照"或 HEAD 的**树**去比。原因是树对树比较会漏掉 agent 运行期的
    # 改动 —— checkpoint 只挂 refs/snapshots/*、从不推进 HEAD（这是本模块自己的
    # 设计），所以被改坏的文件全在工作区、不在任何一棵树里，diff 为空 → 一个文件都
    # 不恢复，函数却返回非空 HEAD，`dashboard/server.py` 据此报"回滚成功"
    # （2026-09-22 审计实测：改坏 a.py 后 rollback_to 返回非空，a.py 纹丝不动）。
    # 同理，目标快照之后**新增**的文件（在 commit 树里不存在）现在也会被正确判成
    # "A" 并删除，而不是像以前那样残留。
    diff = _git(["diff", "--name-status", "--no-renames", commit], path)
    if diff.returncode != 0:
        return ""
    entries: list = []
    # "A" 要区别对待：`git diff <commit>` 把"已暂存但未提交"的新文件也报成新增，
    # 而那多半是用户自己 `git add` 的东西 —— 回滚不该替他删掉（2026-09-22 验证时
    # 踩到：用户 add 了 user_wip.txt，回滚一次文件就没了）。
    # 判据用"该文件是否已进 HEAD 历史"：进了 = 是某次提交带进来的，该删；
    # 只在索引/工作区里 = 用户还在写的在制品，别动。
    head_files = set((_git(["ls-tree", "-r", "--name-only", "HEAD"], path).stdout or "").splitlines())
    for line in diff.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, rel = parts[0].strip(), parts[1].strip()
        if status == "A" and rel not in head_files:
            continue
        entries.append((status, rel))
    # 再并上"与最新快照比较"这一路：目标快照之后**被 checkpoint 过**的新文件只会
    # 出现在后续快照的树里（它们不在 HEAD 历史里，正好被上面的规则放过）——
    # 这一类是 agent 自己造的，可以放心移除；不并这一路，回滚就会把它们残留下来。
    seen = {rel for _, rel in entries}
    newest = _newest_snapshot(path)
    if newest and newest != commit:
        extra = _git(["diff", "--name-status", "--no-renames", commit, newest], path)
        if extra.returncode == 0:
            for line in extra.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[1].strip() not in seen:
                    seen.add(parts[1].strip())
                    entries.append((parts[0].strip(), parts[1].strip()))

    touched: list = []
    for status, rel in entries:
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
    # 恢复后的内容恰好与 HEAD 一致时，`git commit` 会以 "nothing to commit" 失败 ——
    # 那不是回滚失败：文件已经回到目标状态了，而调用方（dashboard 的
    # `POST /api/rollback`）按"返回非空即成功"判定，返回 "" 会把成功显示成失败。
    # 所以再确认一次"工作区是否真的已与目标提交一致"，一致就算成功。
    verify = _git(["diff", "--quiet", commit, "--"] + touched, path)
    return _head(path) if verify.returncode == 0 else ""


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
