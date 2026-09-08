"""
云经验库工具（experience）：跨 Agent 的经验学习与沉淀。

- search <query>：在私有学习库 + 公共分享库（已配置时）的本地克隆里检索
  相关经验条目，返回有限预算的注入文本（防提示注入：条目仅作参考资料）。
- save {domain, topic, body, tags}：把一次复盘/经验写成结构化 md 条目，
  经密钥扫描与长度校验后 commit + push 到【私有经验仓】；
  公共分享库不直推（经 PR 人工合并，防投毒）。

仓库协议（约定）：
  库根可选 experiences/ 目录，条目为 Markdown，frontmatter 含
  title / domain / tags / date。缺目录时扫描库根 *.md（跳过 README/index）。
"""
import datetime as _dt
import os
import re
import subprocess
import time as _time
from typing import Any, Dict, List, Tuple

from tools.base import BaseTool, ToolResult

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
]


def _git(args: List[str], cwd: str, check: bool = True) -> Tuple[int, str]:
    """执行 git（禁交互提示；继承项目仓库 http.proxy 以便走本机代理）。"""
    cmd = ["git"]
    try:
        proxy = subprocess.run(["git", "config", "--get", "http.proxy"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        if proxy:
            cmd += ["-c", f"http.proxy={proxy}"]
    except Exception:
        pass
    cmd += args
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_NAME="my-agent", GIT_AUTHOR_EMAIL="my-agent@local",
               GIT_COMMITTER_NAME="my-agent", GIT_COMMITTER_EMAIL="my-agent@local")
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180, env=env)
    except Exception as e:
        return 1, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        raise RuntimeError(out.strip()[:600])
    return r.returncode, out.strip()


def _cfg() -> dict:
    from config import EXPERIENCE_CONFIG
    return EXPERIENCE_CONFIG


def _cache_path(kind: str) -> str:
    cfg = _cfg()
    url = str(cfg.get(f"{kind}_repo") or "").strip()
    # Windows 本地路径也按分隔符取仓库名（反斜杠归一），避免整条路径进目录名
    url_n = url.replace("\\", "/").rstrip("/")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-",
                  url_n.rsplit("/", 1)[-1].replace(".git", ""))
    base = os.path.abspath(str(cfg.get("cache_dir") or "./memory/experience_lib"))
    return os.path.join(base, f"{kind}-{name}")


class ExperienceTool(BaseTool):
    """云经验库：search 学习他人经验 / save 沉淀自己的复盘（私有仓）。"""

    name = "experience"
    description = (
        "云经验库操作：search <query> 在经验库（私有学习库+可选公共分享库）检索相关"
        "经验条目并返回精炼要点；save 用结构化复盘（domain/topic/body/tags）写入私有"
        "经验仓并推送。条目协议：Markdown + frontmatter。"
    )
    risk_level: str = "medium"
    min_sandbox_mode: str = "workspace-write"   # 缓存与 git 均在项目 memory/ 下
    parallel_safe: bool = False

    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "save"]},
            "query": {"type": "string", "description": "search：检索关键词/领域"},
            "domain": {"type": "string", "description": "save：领域，如 棋类/象棋"},
            "topic": {"type": "string", "description": "save：条目标题"},
            "body": {"type": "string", "description": "save：复盘正文（经过/教训/可复用步骤）"},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "save：标签"},
            "top_k": {"type": "integer", "description": "search：返回条数（默认配置）"},
        },
        "required": ["action"],
    }

    # ---------------- 入口 ----------------
    def execute(self, input_str: str) -> ToolResult:
        text = input_str.strip()
        if not text:
            return ToolResult(success=False, output="",
                              error="用法：experience search <关键词>；或 JSON {action:'save', ...}")
        if text.startswith("{"):
            try:
                import json
                return self.execute_json(json.loads(text))
            except Exception as e:
                return ToolResult(success=False, output="", error=f"参数解析失败: {str(e)[:120]}")
        if text.startswith("search "):
            return self.execute_json({"action": "search", "query": text[7:].strip()})
        if text.startswith("save "):
            return ToolResult(success=False, output="",
                              error="save 需要结构化字段，请用 JSON：{action:'save', domain, topic, body, tags}")
        return ToolResult(success=False, output="", error="未知动作：可用 search / save。")

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action == "search":
            return self._search(arguments)
        if action == "save":
            return self._save(arguments)
        return ToolResult(success=False, output="", error=f"未知 action: {action or '(空)'}")

    # ---------------- 学习端 search ----------------
    def _ensure_repo(self, kind: str) -> Tuple[str, str]:
        """确保本地克隆就绪（带 TTL 节流的 pull）。返回 (路径, 错误|'')。"""
        cfg = _cfg()
        url = str(cfg.get(f"{kind}_repo") or "").strip()
        if not url:
            return "", "未配置经验库"
        path = _cache_path(kind)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.isdir(os.path.join(path, ".git")):
                _git(["clone", "--depth", "1", "--single-branch", "--branch",
                      str(cfg.get("branch") or "main"), url, path], cwd=os.getcwd())
            else:
                marker = os.path.join(path, ".fetched")
                stale = True
                if os.path.isfile(marker):
                    try:
                        stale = (_time.time() - os.path.getmtime(marker)) > float(
                            cfg.get("pull_ttl_sec") or 600)
                    except OSError:
                        stale = True
                if stale:
                    _git(["pull", "--ff-only", "--depth", "1"], cwd=path)
                    try:
                        with open(marker, "w", encoding="utf-8") as f:
                            f.write(str(_time.time()))
                    except OSError:
                        pass
            return path, ""
        except Exception as e:
            return path, f"{kind} 库不可达: {str(e)[:200]}"

    def _entry_files(self, repo_path: str) -> List[str]:
        root = os.path.join(repo_path, "experiences")
        if not os.path.isdir(root):
            root = repo_path
        found: List[str] = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git"]
            if ".git" in dirpath:
                continue
            for fn in files:
                if fn.endswith(".md") and fn.lower() not in ("readme.md", "index.md"):
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(p) <= 60 * 1024:
                            found.append(p)
                    except OSError:
                        pass
        return found

    @staticmethod
    def _score(query: str, rel_path: str, text: str) -> float:
        q = query.lower()
        tokens = [t for t in re.split(r"[\s,，。、;；:：/\\|]+", q) if len(t) >= 2] or [q]
        body_l = text.lower()
        head = text[:800].lower()
        score = 0.0
        for t in tokens:
            score += body_l.count(t) * 1.0
            if t in head:
                score += 6.0
            if t in rel_path.lower():
                score += 4.0
        m = re.search(r"(?im)^tags?\s*:\s*(.+)$", text[:800])
        if m and any(t in m.group(1).lower() for t in tokens):
            score += 5.0
        return score

    def _search(self, args: Dict[str, Any]) -> ToolResult:
        cfg = _cfg()
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="search 需要 query。")
        top_k = max(1, min(int(args.get("top_k") or cfg.get("learn_max_entries", 3)), 6))
        max_chars = int(cfg.get("learn_max_chars", 2500))

        notes: List[str] = []
        hits: List[Tuple[str, str, str, float]] = []   # (kind, rel, full, score)
        for kind in ("private", "public"):
            if not _repo_url(kind):
                continue        # 公共库可选：未配置不打扰
            path, err = self._ensure_repo(kind)
            if err or not path:
                if err:
                    notes.append(err)
                continue
            for full in self._entry_files(path):
                try:
                    text = open(full, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                rel = os.path.relpath(full, path)
                score = self._score(query, rel, text)
                if score > 0:
                    hits.append((kind, rel, text, score))
        hits.sort(key=lambda x: -x[3])

        if not hits:
            tail = ("\n".join(notes)) if notes else "（.env 里配置 EXPERIENCE_PRIVATE_REPO / EXPERIENCE_PUBLIC_REPO 后可用）"
            return ToolResult(success=True, output=f"经验库无匹配条目。\n{tail}".rstrip())

        out: List[str] = [f"经验库命中 {len(hits)} 条（前 {min(top_k, len(hits))} 条）："]
        used = 0
        for kind, rel, text, score in hits[:top_k]:
            body = re.sub(r"^---.*?---", "", text, count=1, flags=re.S).strip()
            body = re.sub(r"\n{3,}", "\n\n", body)
            snippet = body[:700] + ("…" if len(body) > 700 else "")
            seg = f"\n{'─'*44}\n[{kind} · 相关度 {score:.0f}] {rel}\n{snippet}"
            if used + len(seg) > max_chars and out:
                break
            out.append(seg)
            used += len(seg)
        if notes:
            out.append("\n" + "；".join(notes))
        return ToolResult(success=True, output="\n".join(out))

    # ---------------- 沉淀端 save ----------------
    def _save(self, args: Dict[str, Any]) -> ToolResult:
        cfg = _cfg()
        url = str(cfg.get("private_repo") or "").strip()
        if not url:
            return ToolResult(
                success=False, output="",
                error="未配置私有经验仓：在 .env 填 EXPERIENCE_PRIVATE_REPO=<GitHub 经验库地址>"
                      "（先建一个空仓库）后重试。")
        domain = str(args.get("domain") or "").strip()
        topic = str(args.get("topic") or "").strip()
        body = str(args.get("body") or "").strip()
        tags = args.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，]", tags) if t.strip()]
        if not domain or not topic or not body:
            return ToolResult(success=False, output="",
                              error="save 需要 domain / topic / body（tags 可选）。")
        if len(body) > int(cfg.get("entry_max_chars", 8000)):
            return ToolResult(success=False, output="",
                              error=f"body 过长（>{cfg.get('entry_max_chars')} 字），请精简。")
        scan = body + "\n" + " ".join(tags)
        for pat in _SECRET_PATTERNS:
            if pat.search(scan):
                return ToolResult(success=False, output="",
                                  error="检测到疑似密钥/凭据内容，拒绝入库（经验里不要写真实 key/token）。")

        slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic)[:50].strip("-") or "entry"
        fname = f"{_dt.date.today().isoformat()}-{slug}.md"
        path = _cache_path("private")
        try:
            if not os.path.isdir(os.path.join(path, ".git")):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                _git(["clone", "--depth", "1", "--single-branch", "--branch",
                      str(cfg.get("branch") or "main"), url, path], cwd=os.getcwd())
            target_dir = os.path.join(path, "experiences", domain.replace("/", os.sep))
            os.makedirs(target_dir, exist_ok=True)
            tag_line = ", ".join(f'"{t}"' for t in tags)
            entry = ("---\n"
                     f"title: {topic}\n"
                     f"domain: {domain}\n"
                     f"date: {_dt.date.today().isoformat()}\n"
                     f"tags: [{tag_line}]\n"
                     "author: my_agent\n"
                     "---\n\n"
                     f"{body}\n")
            full = os.path.join(target_dir, fname)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(entry)
            rel_entry = os.path.relpath(full, path)
            _git(["add", "--", rel_entry], cwd=path)
            _git(["commit", "-m", f"experience: {domain} - {topic}"], cwd=path)
            sha = _git(["rev-parse", "--short", "HEAD"], cwd=path)[1]
            code, msg = _git(["push", "origin",
                              "HEAD:" + str(cfg.get("branch") or "main")], cwd=path, check=False)
            if code != 0:
                return ToolResult(
                    success=True,
                    output=f"经验已写入私有库并本地提交（commit {sha}），但推送失败：{msg[:200]}。"
                           f"可手动补推：cd {path} && git push",
                    metadata={"committed": True, "pushed": False, "file": full, "commit": sha},
                )
            return ToolResult(
                success=True,
                output=f"经验已上传私有库 ✓ commit {sha}\n文件：{os.path.relpath(full, path)}",
                metadata={"committed": True, "pushed": True, "file": full, "commit": sha},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"保存失败: {str(e)[:300]}")
