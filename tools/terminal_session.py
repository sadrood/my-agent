"""
持久终端会话（参照上游同类 `terminal`：跨工具调用保持 shell 状态）。

一个会话 = 一个常驻 shell 子进程（Windows cmd.exe / POSIX bash）。
cd / set / 激活环境等状态跨 tool 调用保留。会话按 key（会话 id）隔离，
由 TerminalTool 统一管理。

执行方式：写入命令 + 哨兵行（echo __DONE_xxx__），后台 reader 线程持续
读输出，直到哨兵出现返回该段输出。超时/会话退出则报错，不阻塞 Agent。
"""
import os
import platform
import subprocess
import threading
import time

_IS_WINDOWS = platform.system() == "Windows"


def _default_shell() -> str:
    if _IS_WINDOWS:
        return "cmd.exe"
    return os.environ.get("SHELL", "bash")


class TerminalSession:
    """常驻 shell 会话。"""

    def __init__(self, shell: str = None, cwd: str = None, env: dict = None):
        self._shell = shell or _default_shell()
        self._lock = threading.Lock()
        self._lines: list = []
        self._cond = threading.Condition(self._lock)
        self._closed = False
        # 跟踪 cwd：cd 命令时解析更新，避免依赖解析 cmd 输出里的提示符
        self._cwd = os.path.abspath(cwd or os.getcwd())

        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        if cwd:
            kwargs["cwd"] = cwd
        self._proc = subprocess.Popen([self._shell], **kwargs)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        # 清掉 cmd/bash 启动横幅：跑一个无副作用命令并丢弃其输出段
        try:
            self.run("echo.>nul" if _IS_WINDOWS else ":", timeout=15)
            with self._lock:
                self._lines.clear()
        except Exception:
            pass

    def _pump(self):
        try:
            for line in iter(self._proc.stdout.readline, ""):
                with self._cond:
                    self._lines.append(line)
                    self._cond.notify_all()
        except Exception:
            pass

    def alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    def run(self, cmd: str, timeout: float = 60.0) -> tuple:
        """在会话里执行命令，返回 (success, output)。输出已去掉哨兵与尾部提示符。"""
        if not self.alive():
            return False, "会话已关闭。请先 terminal session start。"
        token = f"__DONE_{time.time():.3f}__".replace(".", "")
        try:
            with self._lock:
                start = len(self._lines)
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.write(f"echo {token}\n")
                self._proc.stdin.flush()
        except Exception as e:
            return False, f"写入会话失败: {e}"
        self._maybe_update_cwd(cmd)

        # 等待哨兵出现（带超时）。精确匹配：命令回显行「echo __DONE_x__」虽含 token，
        # 但整行 != token，避免提前命中把真实哨兵输出留到下次。
        sentinel_idx = None
        deadline = time.time() + timeout
        with self._cond:
            while time.time() < deadline:
                for i in range(start, len(self._lines)):
                    if self._lines[i].strip() == token:
                        sentinel_idx = i
                        break
                if sentinel_idx is not None:
                    break
                if self._proc.poll() is not None:
                    break
                self._cond.wait(timeout=0.2)
        if sentinel_idx is None:
            return False, f"命令执行超时（{timeout:.0f}s）或会话无响应。"

        with self._lock:
            lines = list(self._lines[start:sentinel_idx])
        output = self._strip_prompt("".join(lines))
        return True, output.strip() or "(无输出)"

    @staticmethod
    def _strip_prompt(text: str) -> str:
        """去掉 cmd/bash 的提示符行与命令回显行，只留命令输出。"""
        import re as _re
        lines = text.splitlines()
        cleaned = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            # Windows：纯提示符（D:\path>）或 提示符+命令回显（D:\path>command）
            if _IS_WINDOWS and _re.match(r"^[A-Za-z]:\\[^>]*>(?:\S.*)?$", s):
                continue
            # POSIX：提示符行（user@host:~/dir$ 或 #）
            if not _IS_WINDOWS and _re.match(r"^[\w.\-]+@[\w.\-]+:[^\s]*[$#]\s*$", s):
                continue
            cleaned.append(ln)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned)

    def _maybe_update_cwd(self, cmd: str) -> None:
        """cd 命令时跟踪 cwd（不依赖解析 cmd 输出）。"""
        c = cmd.strip()
        if not c.lower().startswith("cd "):
            return
        target = c[3:].strip()
        if target.lower().startswith("/d "):   # cmd: cd /d D:\x
            target = target[3:].strip()
        target = target.strip('"').strip("'")
        if not target:
            return
        try:
            target = os.path.expandvars(target)
        except Exception:
            pass
        new = os.path.abspath(target) if os.path.isabs(target) else os.path.abspath(os.path.join(self._cwd, target))
        if os.path.isdir(new):
            self._cwd = new

    def state(self) -> dict:
        """返回当前 cwd 等状态（跟踪式，不依赖解析输出）。"""
        return {"alive": self.alive(), "cwd": self._cwd if self.alive() else ""}

    def close(self):
        self._closed = True
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                )
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except Exception:
                    self._proc.kill()
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        try:
            self._proc.stdin.close()
        except Exception:
            pass


class TerminalSessionManager:
    """按 key（会话 id）管理常驻终端会话。"""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> TerminalSession:
        with self._lock:
            s = self._sessions.get(key)
            if s is not None and not s.alive():
                self._sessions.pop(key, None)
                s = None
            return s

    def start(self, key: str) -> TerminalSession:
        with self._lock:
            s = self._sessions.get(key)
            if s is not None and s.alive():
                return s
            s = TerminalSession()
            self._sessions[key] = s
            return s

    def stop(self, key: str) -> bool:
        with self._lock:
            s = self._sessions.pop(key, None)
        if s is None:
            return False
        s.close()
        return True

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
