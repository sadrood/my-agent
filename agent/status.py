"""运行态自省（`main.py --ps`）：面板、QQ 桥、最近的运行与孤儿。

存在意义：同一个机器人 token 跑两个桥会互相抢消息，而"谁在跑"此前只能靠翻日志。
凭据/PID 文件都放在 memory/（已 gitignore），进程退出时清理。
"""
import json
import os
import time
from typing import Optional

#: QQ 桥的 PID 文件（桥启动时写、退出时删）
QQBOT_PID_FILE = os.path.join("memory", "qqbot.pid")


def _root() -> str:
    from config import PROJECT_ROOT
    return PROJECT_ROOT


def _abs(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(_root(), rel)


def pid_alive(pid) -> bool:
    """进程是否还活着。Windows 上不能用 os.kill(pid, 0)（会真的杀进程）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10,
                                 errors="ignore")
            return str(pid) in (out.stdout or "")
        except Exception:                       # noqa: BLE001
            return False
    return os.path.isdir(f"/proc/{pid}")


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:                           # noqa: BLE001
        return None


def collect(hours: float = 24.0) -> dict:
    """汇总当前运行态：面板 / QQ 桥 / 最近运行 / 孤儿。"""
    from dashboard.server import ipc_file_path
    from agent import recover

    dash = _read_json(ipc_file_path()) or {}
    if dash:
        dash["alive"] = pid_alive(dash.get("pid"))

    qq = _read_json(_abs(QQBOT_PID_FILE)) or {}
    if qq:
        qq["alive"] = pid_alive(qq.get("pid"))

    runs = recover.scan_runs(hours=hours)
    return {
        "dashboard": dash,
        "qqbot": qq,
        "runs": runs[:10],
        "orphans": [r for r in runs if r.get("orphan")],
        "hours": hours,
    }


def render(status: dict) -> str:
    """终端文本。每段都写清"有没有在跑"，避免"以为在跑其实早死了"。"""
    lines = ["my_agent 运行态", "─" * 40]

    dash = status.get("dashboard") or {}
    if dash:
        where = f"{dash.get('host')}:{dash.get('port')}" + (f" · UDS {dash['uds']}" if dash.get("uds") else "")
        state = "在跑" if dash.get("alive") else "已退出（凭据文件是残留）"
        lines.append(f"  [面板] {state} · {where} · pid {dash.get('pid')} · 起于 {dash.get('started_at', '')}")
    else:
        lines.append("  [面板] 未启动（MY_AGENT_WEB_UI=1 或 --dashboard 才会跑）")

    qq = status.get("qqbot") or {}
    if qq:
        state = "在跑" if qq.get("alive") else "已退出（PID 文件是残留）"
        lines.append(f"  [QQ 桥] {state} · pid {qq.get('pid')} · 起于 {qq.get('started_at', '')}")
    else:
        lines.append("  [QQ 桥] 未启动（python -m agent.qqbot）")

    runs = status.get("runs") or []
    if runs:
        lines.append(f"  最近运行（{len(runs)} 条，{status.get('hours')} 小时内）:")
        for r in runs[:5]:
            age = max(0, int(time.time() - float(r.get("mtime") or 0)))
            mark = "无收尾" if r.get("orphan") else "已收尾"
            goal = (r.get("goal") or "").replace("\n", " ")[:34]
            lines.append(f"    {r['run_id']}  {mark} · {r['events']} 事件 · {age}s 前 · {goal}")
    else:
        lines.append("  最近没有运行记录")

    orphans = status.get("orphans") or []
    if orphans:
        # "无 run_end" 既可能是被强杀，也可能是此刻正在跑——不能一律叫孤儿，否则误报吓人
        fresh = sum(1 for r in orphans if time.time() - float(r.get("mtime") or 0) < 120)
        lines.append(f"  ⚠ {len(orphans)} 个运行没有收尾事件（其中 {fresh} 个 2 分钟内还在写 = 可能正在跑）")
        lines.append("     确认已停的：python -m agent.recover --orphans 看，--run <run-id> 续跑")
    return "\n".join(lines)
