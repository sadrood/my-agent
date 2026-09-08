"""
任务模式自动路由（--auto-mode / AUTO_MODE=true）。

轻量关键词启发式：把目标分类为 research（深度研究）/ team（团队协作）/
single（默认单循环）。规则刻意保守：只有明确命中的措辞才改路由，
避免把普通任务误送进重模式。
"""
import re

# 深度研究：明确表达"调研/分析/综述/报告"类措辞
RESEARCH_KEYWORDS = [
    r"深度研究",
    r"调研",
    r"研究报告",
    r"行业分析",
    r"市场分析",
    r"竞品分析",
    r"文献综述",
    r"趋势分析",
    r"写.{0,6}(调研|研究|分析)报告",
    r"搜集.{0,12}资料.{0,6}(汇总|整理|报告)",
]

# 团队协作：明确表达"团队/并行/多子任务/同时多模块"类措辞
TEAM_KEYWORDS = [
    r"团队",
    r"并行.{0,8}(完成|执行|开发|实现|构建|处理)",
    r"多.{0,6}子任务",
    r"同时.{0,8}(开发|实现|构建|完成)",
    r"(前后端|多模块|多组件).{0,8}(一起|同时|并行)",
]


def route_goal(goal: str) -> str:
    """按关键词把目标路由到 "research" / "team" / "single"。"""
    text = (goal or "").strip()
    if not text:
        return "single"
    for pattern in RESEARCH_KEYWORDS:
        if re.search(pattern, text):
            return "research"
    for pattern in TEAM_KEYWORDS:
        if re.search(pattern, text):
            return "team"
    return "single"
