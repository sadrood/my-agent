"""验收章节拆分：文件数、文件名、标题与正文是否完整。"""
import sys
from pathlib import Path

EXPECTED = [
    ("第01章.txt", "第一章 归港", "服务器风扇的声音不如浪声踏实"),
    ("第02章.txt", "第二章 旧灯塔", "海上的船需要知道岸在哪"),
    ("第03章.txt", "第三章 潮湿的代码", "屏幕上就会多出一行没人写过的注释"),
    ("第04章.txt", "第四章 信", "收信人全是同一个名字：林澈"),
    ("第05章.txt", "第五章 潮退", "用最后一点电量把代码提交"),
    ("第06章.txt", "第六章 岸", "岸一直在"),
]


def main() -> int:
    workspace = Path(sys.argv[1])
    chapters = workspace / "chapters"
    errors = []
    if not chapters.is_dir():
        print("缺少 chapters/ 目录")
        return 1
    names = sorted(path.name for path in chapters.glob("*.txt"))
    expected_names = [name for name, _, _ in EXPECTED]
    if names != expected_names:
        errors.append(f"文件名应为 {expected_names}，实际 {names}")

    for name, title, key in EXPECTED:
        path = chapters / name
        if not path.exists():
            errors.append(f"缺少 {name}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if title not in text:
            errors.append(f"{name} 缺少章节标题“{title}”")
        if key not in text:
            errors.append(f"{name} 正文不完整（缺少关键句）")

    if errors:
        print("\n".join(errors))
        return 1
    print("ok：6 章拆分完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
