"""进程树/进程组工具：让子进程自成一组，并能在超时/停止时整组杀掉。

此前 terminal / terminal_session / patch 三处各写了一份，容易随时间漂移。
"""
import os
import signal
import subprocess
from typing import Any, Dict


def new_process_group_kwargs() -> Dict[str, Any]:
    """Popen 参数：让子进程自成一个进程组/会话，超时时才能整组杀掉（shell=True 尤其需要）。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc, timeout: float = 15) -> None:
    """连**子孙进程**一起杀掉（Windows 用 taskkill /T，POSIX 杀整个进程组）；
    任何失败都降级为只杀父进程，绝不抛出。"""
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:                            # noqa: BLE001
        try:
            proc.kill()
        except Exception:                        # noqa: BLE001
            pass
