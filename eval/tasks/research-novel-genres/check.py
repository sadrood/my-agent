"""验收调研报告：类型数量、引用来源、风险与推荐结构。"""
import re
import sys
from pathlib import Path

GENRES = ("无限流", "规则怪谈", "系统文", "系统", "都市异能", "家庭伦理",
          "赘婿", "神医", "悬疑")
SOURCES = ("01-平台趋势.md", "02-ai批量产出.md", "03-风险与合规.md")


def main() -> int:
    workspace = Path(sys.argv[1])
    report = workspace / "report.md"
    if not report.exists():
        print("缺少 report.md")
        return 1
    text = report.read_text(encoding="utf-8", errors="replace")
    compact = re.sub(r"\s", "", text)
    errors = []
    if len(compact) < 800:
        errors.append(f"报告只有 {len(compact)} 字（要求 >=800）")

    hits = [genre for genre in GENRES if genre in text]
    if len(hits) < 5:
        errors.append(f"只写到 {len(hits)} 个类型（要求至少 5 个）：{hits}")
    cited = [name for name in SOURCES if name in text or name[:-3] in text]
    if len(cited) < 3:
        errors.append(f"只引用了 {len(cited)} 份资料（要求 3 份）：{cited}")
    if "风险" not in text:
        errors.append("缺少风险分析")
    if "推荐" not in text and "优先级" not in text:
        errors.append("缺少推荐/优先级结论")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"ok：{len(compact)} 字，{len(hits)} 个类型，引用 {len(cited)} 份资料")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
