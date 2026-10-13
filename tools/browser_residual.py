"""残留浏览器进程的发现与清理：按命令行里的 `--user-data-dir=<profile>` 定位并杀掉。

只依赖 profile 目录，不碰浏览器/页面对象——从 BrowserTool 里抽出，避免平台分支散落在主类中。
"""
import os
import time


def residual_pids_win(profile_dir: str) -> list:
    """命令行里带该 profile 的 chrome.exe 进程号（PowerShell 单引号转义）。"""
    import subprocess
    profile = str(profile_dir).replace("'", "''")
    cmd = (
        "powershell -NoProfile -Command "
        f"\"Get-CimInstance Win32_Process -Filter \\\"Name='chrome.exe'\\\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
        f"Select-Object -ExpandProperty ProcessId\""
    )
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=30, shell=True).stdout
    except Exception:
        return []
    return [p.strip() for p in out.split() if p.strip().isdigit()]


def residual_pids_posix(profile_dir: str) -> list:
    """POSIX 侧：`ps` 取完整命令行，realpath **全等**比对 profile 目录。
    全等而不是子串：避免 "profile" 前缀误伤 "profile2" 这类无关实例。"""
    import subprocess
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,args="],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30).stdout
    except Exception:
        return []
    want = os.path.realpath(profile_dir)
    pids = []
    for line in (out or "").splitlines():
        line = line.strip()
        if "--user-data-dir=" not in line:
            continue
        pid, _, args = line.partition(" ")
        if not pid.isdigit():
            continue
        value = args.split("--user-data-dir=", 1)[1].strip().split(" ", 1)[0]
        value = value.strip("\"'")                     # 有的启动方式会带引号
        if value and os.path.realpath(value) == want:
            pids.append(pid)
    return pids


def kill_residual_win(pid: str) -> bool:
    import subprocess
    try:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                       capture_output=True, text=True, timeout=30)
        return True
    except Exception:
        return False


def kill_residual_posix(pid: str) -> bool:
    """先 SIGTERM，给 0.5s 自行收尾（Chromium 会带走子进程），仍活着再 SIGKILL。"""
    import signal
    # SIGKILL 在 Windows 上不存在；POSIX 各平台都是 9（取默认值以便被单测覆盖）
    sigkill = getattr(signal, "SIGKILL", 9)
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        return True                                    # 已经没了，也算清掉
    except Exception:
        return False
    for _ in range(5):
        time.sleep(0.1)
        try:
            os.kill(int(pid), 0)                       # 探活，不真发信号
        except ProcessLookupError:
            return True
        except Exception:
            return True
    try:
        os.kill(int(pid), sigkill)
    except ProcessLookupError:
        return True                                    # 刚好在这一刻退出了
    except Exception:
        return False                                   # 没杀掉就是没杀掉，别报成功
    return True
