"""从 rollout 事件流恢复会话：进程被强杀时，会话文件可能什么都没留下。

rollout 是**增量写**的，所以即使进程被 kill，事件流也在；本模块把它还原成会话记录，
并检测"有 rollout 却没有 run_end"的孤儿运行（那种就是被强杀的）。
命令行：`python -m agent.recover --list` / `python -m agent.recover --run <run-id>`
"""
import argparse
import glob
import json
import os
import sys
import time
from typing import Optional

#: 小于这个事件数的 rollout 视为噪音，不做孤儿提示
MIN_ORPHAN_EVENTS = 30


def _root() -> str:
    from config import PROJECT_ROOT
    return PROJECT_ROOT


def rollout_dir() -> str:
    from config import ROLLOUT_CONFIG
    raw = str(ROLLOUT_CONFIG.get("dir") or "rollouts")
    return raw if os.path.isabs(raw) else os.path.join(_root(), raw)


def _events(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def scan_runs(hours: float = 24.0) -> list:
    """列出最近的运行及其收尾状态。orphan=True 表示没有 run_end（被强杀）。"""
    deadline = time.time() - hours * 3600
    runs = []
    for path in glob.glob(os.path.join(rollout_dir(), "run-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < deadline:
            continue
        events = _events(path)
        kinds = {str((e or {}).get("event") or "") for e in events}
        runs.append({
            "run_id": os.path.basename(path)[:-6],
            "path": path,
            "events": len(events),
            "mtime": mtime,
            "finished": bool(kinds & {"run_end", "run_loop_end"}),
            "orphan": not bool(kinds & {"run_end", "run_loop_end"}),
            "goal": next((str((e.get("data") or {}).get("goal") or "")
                          for e in events if (e or {}).get("event") == "run_start"), ""),
        })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def find_orphans(hours: float = 24.0, min_events: int = MIN_ORPHAN_EVENTS) -> list:
    """被强杀、且有实际内容的运行（启动时用它提示用户可恢复）。"""
    return [r for r in scan_runs(hours) if r["orphan"] and r["events"] >= min_events]


def resolve_run(run_id: str) -> Optional[str]:
    """按 run id（或文件名/前缀）找 rollout 路径。"""
    if not run_id:
        return None
    path = run_id if os.path.isfile(run_id) else os.path.join(rollout_dir(), run_id)
    if os.path.isfile(path):
        return path
    matches = glob.glob(os.path.join(rollout_dir(), f"{run_id}*.jsonl"))
    return matches[0] if matches else None


def build_messages(events: list) -> list:
    """把事件流还原成会话记录（user/assistant；工具记录单独成条，不挤占正文）。

    规则：run_start 的目标 → 首条 user；model_turn 的正文 → assistant；
    工具调用与结果攒成「执行记录」条（超过 3000 字符就先落一条，避免长任务把正文挤掉）。
    """
    messages, buffer = [], []

    def flush_buffer() -> None:
        if not buffer:
            return
        messages.append({"role": "assistant",
                         "content": ("【执行记录】\n" + "\n".join(buffer))[:4000]})
        buffer.clear()

    def push(line: str) -> None:
        buffer.append(line)
        if sum(len(x) for x in buffer) > 3000:
            flush_buffer()

    for event in events:
        kind = str((event or {}).get("event") or "")
        data = (event or {}).get("data") or {}
        if kind == "run_start":
            goal = str(data.get("goal") or "").strip()
            if goal:
                messages.append({"role": "user", "content": goal[:8000]})
        elif kind in ("goal", "user_message"):
            text = str(data.get("content") or data.get("goal") or "").strip()
            if text:
                flush_buffer()
                messages.append({"role": "user", "content": text[:8000]})
        elif kind == "tool_call":
            push(f"【工具 {data.get('tool')}】"
                 f"{json.dumps(data.get('args'), ensure_ascii=False)[:300]}")
        elif kind == "tool_result":
            got = str(data.get("output") or data.get("error") or "")
            push(f"【结果 {'成功' if data.get('success') else '失败'}】{got[:600]}")
        elif kind == "model_turn":
            text = str(data.get("content") or "")
            if text.strip():
                flush_buffer()
                messages.append({"role": "assistant", "content": text[:8000]})
    flush_buffer()
    return messages


def recover(run_id: str, conv_id: str = "", dry_run: bool = False) -> dict:
    """把一次运行恢复成会话记录；返回统计信息。"""
    path = resolve_run(run_id)
    if not path:
        return {"ok": False, "error": f"找不到运行: {run_id}"}
    events = _events(path)
    messages = build_messages(events)
    if not messages:
        return {"ok": False, "error": f"{os.path.basename(path)} 里没有可恢复的内容"}
    stem = os.path.basename(path)[:-6]
    target = conv_id or stem.replace("run-", "conv-run-")
    info = {"ok": True, "run": stem, "conv": target, "messages": len(messages),
            "path": path, "dry_run": dry_run}
    if dry_run:
        return info
    from agent.session import SessionStore
    store = SessionStore()
    existing = store.load_conversation(target) or {}
    if existing.get("messages"):
        info["appended"] = True
        store.append_messages(target, messages[1:] if messages[0]["role"] == "user" else messages,
                              title=existing.get("title") or "")
    else:
        store.save_conversation(target, messages=messages,
                                last_summary="（从 rollout 恢复：本次运行被中断，未走到结束保存）")
    return info


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从 rollout 恢复被中断的会话")
    parser.add_argument("--list", action="store_true", help="列出最近的运行与是否被中断")
    parser.add_argument("--orphans", action="store_true", help="只列出被强杀的运行")
    parser.add_argument("--run", default="", help="要恢复的 run id（可给前缀）")
    parser.add_argument("--conv", default="", help="写入的会话 id（默认按 run id 生成）")
    parser.add_argument("--dry-run", action="store_true", help="只看会恢复出多少条，不写盘")
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    if args.list or args.orphans:
        runs = find_orphans(args.hours) if args.orphans else scan_runs(args.hours)
        if not runs:
            print("没有找到运行记录。")
            return 0
        for run in runs:
            mark = "被中断" if run["orphan"] else "已收尾"
            when = time.strftime("%m-%d %H:%M", time.localtime(run["mtime"]))
            print(f"  [{mark}] {run['run_id']}  {run['events']} 事件  {when}  {run['goal'][:60]}")
        return 0

    if not args.run:
        parser.print_help()
        return 2
    info = recover(args.run, args.conv, args.dry_run)
    if not info.get("ok"):
        print(f"恢复失败：{info.get('error')}")
        return 1
    print(f"恢复完成：{info['run']} → {info['conv']}（{info['messages']} 条记录"
          f"{'，仅试运行' if info['dry_run'] else ''}）")
    return 0


if __name__ == "__main__":      # pragma: no cover - 命令行入口
    sys.exit(_main())
