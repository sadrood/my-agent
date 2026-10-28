"""验收五章短篇：章节数、每章字数、主题词、占位符。"""
import re
import sys
from pathlib import Path


def main() -> int:
    workspace = Path(sys.argv[1])
    story = workspace / "story.md"
    errors = []
    if not story.exists():
        print("缺少 story.md")
        return 1
    text = story.read_text(encoding="utf-8", errors="replace")

    pattern = re.compile(r"^\s*(?:#+\s*)?第\s*([一二三四五1-5])\s*章[^\n]*$", re.M)
    matches = list(pattern.finditer(text))
    if len(matches) != 5:
        errors.append(f"章节数应为 5，实际 {len(matches)}")
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end]
        chars = len(re.sub(r"\s", "", body))
        if chars < 300:
            errors.append(f"第 {index + 1} 章正文只有 {chars} 字（要求 >=300）")

    total = len(re.sub(r"\s", "", text))
    if total < 1500:
        errors.append(f"全文只有 {total} 字（要求 >=1500）")
    for word in ("程序员", "海边"):
        if word not in text:
            errors.append(f"缺少主题词：{word}")
    for marker in ("TODO", "待补充", "此处省略"):
        if marker in text:
            errors.append(f"含占位符：{marker}")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"ok：5 章，共 {total} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
