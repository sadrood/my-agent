"""验收分镜表与 LRC 时间轴：结构、行数、时间递增、歌词覆盖。"""
import re
import sys
from pathlib import Path

LRC_LINE = re.compile(r"^\[(\d{1,2}):(\d{2})\.(\d{2})\](.*)$")


def _lyrics(workspace: Path) -> list:
    path = workspace / "song" / "lyrics.txt"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _find(workspace: Path, name: str):
    for candidate in (workspace / name, workspace / "song" / name):
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    workspace = Path(sys.argv[1])
    errors = []

    storyboard = _find(workspace, "storyboard.md")
    if storyboard is None:
        errors.append("缺少 storyboard.md")
    else:
        lines = storyboard.read_text(encoding="utf-8", errors="replace").splitlines()
        table = [line.strip() for line in lines if line.strip().startswith("|")]
        header = next((line for line in table if "---" not in line), "")
        for word in ("时间戳", "歌词", "画面", "转场"):
            if word not in header:
                errors.append(f"分镜表头缺少“{word}”列")
        rows = [line for line in table[1:] if "---" not in line]
        if len(rows) < 8:
            errors.append(f"分镜表只有 {len(rows)} 行数据（要求 >=8）")

    lrc = _find(workspace, "lyrics.lrc")
    if lrc is None:
        errors.append("缺少 lyrics.lrc")
    else:
        entries = []
        for line in lrc.read_text(encoding="utf-8", errors="replace").splitlines():
            match = LRC_LINE.match(line.strip())
            if match:
                minute, second, centi, text = match.groups()
                entries.append((int(minute) * 60 + int(second) + int(centi) / 100,
                                text.strip()))
        lyrics = _lyrics(workspace)
        if len(entries) < len(lyrics):
            errors.append(f"LRC 只有 {len(entries)} 行（歌词共 {len(lyrics)} 行）")
        if entries:
            times = [time for time, _ in entries]
            if any(later <= earlier for earlier, later in zip(times, times[1:])):
                errors.append("LRC 时间戳没有严格递增")
            if times[0] > 5:
                errors.append(f"第一句在 {times[0]:.2f}s 才开始（要求 <=5s）")
            if not 30 <= times[-1] <= 60:
                errors.append(f"总时长 {times[-1]:.2f}s 不在 30—60 秒内")
            joined = "".join(text for _, text in entries)
            missing = [line for line in lyrics if line not in joined]
            if missing:
                errors.append(f"LRC 漏掉歌词：{missing}")

    if errors:
        print("\n".join(errors))
        return 1
    print("ok：分镜表与 LRC 结构完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
