"""python 工具的独立进程执行器（由 PythonTool 以子进程方式拉起）。

为什么不在进程内 exec：受限命名空间挡不住对象内省与 `import builtins`
（同类方案被 CVE-2025-5120 证伪），而且线程杀不掉，死循环会一直挂在宿主进程里。
这里把执行放进子进程：进程本身可被硬杀，系统的审计钩子又给了第二层拦截。

协议：stdin 收用户代码；argv[1] 是用户 stdout 的落点（有上限，父进程超时后也能读到
已产生的部分）；本进程自己的 stdout 只回一行 JSON 状态。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.getcwd()):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)      # 与进程内 exec 一致：当前目录下的模块可导入

from tools.python import (      # noqa: E402
    BLOCKED_IMPORTS, _CAPTURE_LIMIT, BlockedImportError,
    _restricted_os, blocked_import_message, build_safe_builtins, format_user_traceback,
)

#: 审计钩子拦下的事件：这些能力一律该走 terminal / 浏览器工具（受审批门管理）。
#: 钩子装上就撤不掉，比"导入黑名单"更难绕过——内省拿到 subprocess 也会在这里被挡。
_DENIED_EVENTS = {
    "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
    "os.startfile", "subprocess.Popen", "pty.spawn",
    "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sethostname",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.call_function",
    "winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue",
    "urllib.Request", "http.client.connect", "ftplib.connect", "smtplib.connect",
}


class _CappedWriter:
    """用户 stdout → 文件，超过上限丢弃；每写一次就 flush，父进程超时能读到最后进度。"""

    def __init__(self, path: str, limit: int):
        self._fh = open(path, "w", encoding="utf-8", errors="replace")
        self._limit = max(1000, int(limit))
        self._written = 0
        self.dropped = 0

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        room = self._limit - self._written
        if room <= 0:
            self.dropped += len(s)
            return len(s)
        chunk = s[:room]
        self._fh.write(chunk)
        self._fh.flush()
        self._written += len(chunk)
        self.dropped += len(s) - len(chunk)
        return len(s)

    def flush(self):
        try:
            self._fh.flush()
        except Exception:       # noqa: BLE001
            pass

    def isatty(self) -> bool:
        return False

    def close(self):
        try:
            self._fh.close()
        except Exception:       # noqa: BLE001
            pass


def _install_audit_hook() -> None:
    def hook(event, args):
        if event in _DENIED_EVENTS:
            raise PermissionError(
                f"安全限制：禁止 {event}（执行系统命令请用 terminal 工具，"
                "网络请用对应的 web/浏览器工具）")
        if event == "import" and args:
            name = str(args[0])
            root = name.split(".")[0]
            if root in BLOCKED_IMPORTS or name in BLOCKED_IMPORTS:
                raise BlockedImportError(blocked_import_message(name))

    sys.addaudithook(hook)


def _safe_globals() -> dict:
    return {
        "__builtins__": build_safe_builtins(),
        # 预导入常用模块
        "json": __import__("json"),
        "math": __import__("math"),
        "datetime": __import__("datetime"),
        "re": __import__("re"),
        "random": __import__("random"),
        "itertools": __import__("itertools"),
        "collections": __import__("collections"),
        "os": _restricted_os(__import__("os")),   # 受限代理，非真实 os
        "pathlib": __import__("pathlib"),
    }


def _clean(text) -> str:
    """写回宿主前去掉孤立代理字符。

    用户代码可能产出 lone surrogate（读损坏文件、surrogateescape 等），它无法编码成
    UTF-8 —— 状态行一旦写不出去，整次执行就崩在管道上，连错误报告都拿不到。
    """
    return str(text).encode("utf-8", "replace").decode("utf-8")


def main() -> int:
    out_path = sys.argv[1]
    code = sys.stdin.read()
    writer = _CappedWriter(out_path, _CAPTURE_LIMIT)
    real_stdout = sys.stdout
    sys.stdout = writer
    status = {"ok": True}
    try:
        _install_audit_hook()
        exec(compile(code, "<python-tool>", "exec"), _safe_globals())
    except BlockedImportError as e:
        status = {"ok": False, "kind": "blocked", "message": str(e)}
    except BaseException as e:          # noqa: BLE001 — 用户代码任何异常都只是结果
        status = {
            "ok": False, "kind": "error",
            "error": f"{type(e).__name__}: {e}"[:400],
            "traceback": format_user_traceback(e),
        }
    finally:
        sys.stdout = real_stdout
        status["dropped"] = writer.dropped
        writer.close()
    for key in ("message", "error", "traceback"):
        if key in status:
            status[key] = _clean(status[key])
    real_stdout.write(json.dumps(status, ensure_ascii=False))
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
