"""
Repo Map：任务开始时生成仓库结构摘要并注入上下文。

作用：让模型直接定位文件，而不是反复 file list / 乱读试探（省轮次、省 token）。

v1 实现（确定性、零外部依赖）：
- 目录树（跳过 .venv/.git/__pycache__/运行时目录）
- 每个文件的源码行数
- 总量截断保护（max_chars）
"""
import os
from typing import Optional

SKIP_DIRS = {
    ".venv", ".git", "__pycache__", ".pytest_cache", ".idea",
    "node_modules", "screenshots", "rollouts", "memory", ".vscode",
}
SKIP_FILES = {".env", ".gitignore"}

# 代码相关任务关键词：命中才注入 repo map（避免琐碎任务白白烧 token）
# 注意：不用"写/改/python/agent"这类宽泛词（会误命中"写一首诗""用python计算"）
CODE_KEYWORDS = [
    "代码", "项目", "文件", "修复", "升级", "测试", "优化", "bug",
    "函数", "模块", "自己", "仓库", "重构", "修改", "报错",
    "pytest", "开发", "源码", "源文件",
]


def looks_like_code_task(goal: str) -> bool:
    """判断任务是否与代码/项目相关（用于决定是否注入 repo map）。"""
    lowered = (goal or "").lower()
    return any(kw in lowered for kw in CODE_KEYWORDS)


def build_repo_map(
    project_dir: str,
    max_files: int = 150,
    max_chars: int = 3000,
    max_depth: int = 3,
) -> str:
    """
    生成仓库结构摘要。

    Returns:
        "项目结构（Repo Map）" 风格文本；目录为空/无权限时返回空串。
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        return ""

    lines: list = []
    file_count = 0

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = sorted(
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(".")
        )
        rel = os.path.relpath(root, project_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []   # 不再深入
            continue
        lines.append(("  " * depth) + f"{os.path.basename(root)}/")

        for f in sorted(files):
            if f in SKIP_FILES or f.endswith((".pyc", ".bak")):
                continue
            if file_count >= max_files:
                lines.append(("  " * (depth + 1)) + "…（文件过多，已截断）")
                text = "\n".join(lines)
                return text[:max_chars]
            full = os.path.join(root, f)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    line_count = sum(1 for _ in fh)
            except Exception:
                line_count = 0
            file_count += 1
            lines.append(("  " * (depth + 1)) + f"{f}（{line_count} 行）")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（Repo Map 过长，已截断）"
    return text


def get_repo_map_for_goal(goal: str, project_dir: Optional[str] = None) -> str:
    """按任务类型返回 repo map（代码任务才生成，否则空串）。"""
    if not looks_like_code_task(goal):
        return ""
    base = project_dir or os.getcwd()
    return build_repo_map(base)
