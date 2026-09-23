"""
Python 代码执行工具模块（v3：受限命名空间 + 诚实的边界说明）。

**定位说明（重要）**：这里的受限命名空间是**护栏，不是沙箱**。
它能挡住"顺手执行一条系统命令"这类无门槛操作，但 Python 层面没有真正的
隔离——`().__class__.__base__.__subclasses__()` 之类的手法依然能拿到
subprocess.Popen（实测可达）。真正的隔离依赖 OS 级沙箱
（SANDBOX_EXECUTION=appcontainer）与审批策略，不要把它当成安全边界。

历史沿革：
- v2：schema 收紧为 {"code": string}；从受限命名空间移除 subprocess/sys；
  保留 os/pathlib 用于文件操作。
- v3（2026-09-17 审计）：实测发现 `os.system('echo ...')` 可**直接执行命令**
  （属性调用绕过了 import 黑名单），且子进程输出直写真实 fd 1、连 stdout 捕获
  都绕过。现在 os 换成受限代理，封锁 system/popen/exec*/spawn*/kill 等入口，
  `import os` 也返回同一个代理。
"""
import sys
import io
import threading
import builtins as _builtins
import traceback
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# v2 安全收紧：即使有 __builtins__.__import__，也不允许导入这些模块
# （防止绕过 terminal 工具的审批门执行系统命令 / 网络 / 底层操作）
BLOCKED_IMPORTS = {
    "subprocess", "socket", "ctypes", "winreg", "win32api", "win32con",
    "shutil", "pty", "os.system", "multiprocessing", "pickle", "marshal",
    "importlib", "inspect", "sys", "gc", "traceback", "code", "compileall",
    # nt / posix 是 os 的底层实现模块——`os.system` 就是它们的 `system`，
    # 而 _winapi 直接给 CreateProcess。漏掉它们等于把 os 那层封锁整个让开：
    # `import nt; nt.system(...)` 既不经 os 代理，也不经任何审批门。
    "nt", "posix", "_winapi",
}

# 被禁模块的"出路"提示：模型撞上黑名单时最需要知道的是「那我该用什么」。
# 实测（2026-09-15 漫剧任务）：模型想复制 5 张图 → shutil 被拦 → 报错只说
# "代码执行出错" → 连续 3 轮瞎猜（换绝对路径、换相对路径、查 cwd）。
_BLOCKED_HINTS = {
    "shutil": "复制/移动文件请用 file 工具的 copy/move 操作，删除请用 terminal 工具",
    "subprocess": "执行系统命令请用 terminal 工具（走审批门）",
    "os.system": "执行系统命令请用 terminal 工具（走审批门）",
    "socket": "网络请求请用 terminal 工具或对应的 web/浏览器工具",
    "ctypes": "底层系统调用不受支持，请用 terminal 工具",
    "winreg": "注册表操作请用 terminal 工具（走审批门）",
    "win32api": "Windows API 调用不受支持，请用 terminal 工具",
    "win32con": "Windows API 调用不受支持，请用 terminal 工具",
    "multiprocessing": "本工具已带超时保护，长任务请用 terminal 的 background=true",
    "pty": "伪终端不受支持，请用 terminal 工具",
    "importlib": "动态导入不受支持，请直接 import 目标模块",
    "pickle": "序列化请改用 json",
    "marshal": "序列化请改用 json",
    "inspect": "源码反射不受支持",
    "sys": "sys 不可用；文件操作请用 os/pathlib，系统命令请用 terminal 工具",
    "gc": "gc 不可用",
    "traceback": "traceback 不可用；出错信息会自动回传",
    "code": "交互式解释器不受支持",
    "compileall": "批量编译不受支持",
    "nt": "执行系统命令请用 terminal 工具（走审批门）",
    "posix": "执行系统命令请用 terminal 工具（走审批门）",
    "_winapi": "底层 Windows API 调用不受支持，请用 terminal 工具",
}


class BlockedImportError(ImportError):
    """黑名单模块导入被拒。

    用独立异常类型而不是靠字符串匹配，让 execute() 能把它和其他 ImportError
    （比如用户写错模块名 ModuleNotFoundError）区分开，分别给出精准提示。
    """

# 服务器进程里禁用/无意义的内置项：input 会阻塞后端、eval/exec/compile 与
# globals/locals 会破坏受限命名空间（沙箱逃逸面）
_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint", "exit", "quit",
    "help", "copyright", "credits", "license", "globals", "locals",
}

#: 单次执行的 stdout 捕获上限（字符）。超时后仍在后台跑的代码写不进更多，
#: 避免 `while True: print(...)` 之类把内存吃光。
_CAPTURE_LIMIT = 200_000


def _timeout_seconds() -> float:
    """python 工具执行超时（秒）。默认 30，可用 PYTHON_TOOL_TIMEOUT 调整。

    此前该工具体**没有**任何超时（description 却声称 30 秒），模型一旦写下
    sleep 轮询/长循环就会挂到工具层 300s 硬超时才返回。这里兑现承诺。
    """
    try:
        from config import TOOL_CONFIG
        return float(TOOL_CONFIG.get("python_timeout", 30))
    except Exception:
        return 30.0


def _safe_import(name, *args, **kwargs):
    """受限 __import__：黑名单模块直接拒绝；os 只给受限代理。"""
    root = name.split(".")[0]
    if root in BLOCKED_IMPORTS or name in BLOCKED_IMPORTS:
        hint = _BLOCKED_HINTS.get(name) or _BLOCKED_HINTS.get(root, "")
        msg = (
            f"安全限制：python 工具不允许导入模块 '{name}'"
            "（该模块可绕过 terminal 工具的审批门执行系统级操作）。"
        )
        if hint:
            msg += f"替代方案：{hint}。"
        raise BlockedImportError(msg)
    if root == "os":
        # 关键：`import os` 也必须给受限代理，否则用户代码里再 import 一次
        # 就重新拿到真实的 os，绕过预导入那层的封锁。
        return _restricted_os(__import__(name, *args, **kwargs))
    return __import__(name, *args, **kwargs)


def _format_user_traceback(exc: BaseException, max_frames: int = 6) -> str:
    """只保留用户代码帧的 traceback。

    此前把 tools/python.py 的内部帧（raise box["err"] / exec(code, ...)）一起
    回传，模型看到的是本工具的实现细节，真正有用的「用户代码第几行出错」被埋在
    中间；日志里表现为模型抱怨「报错没有详情」。
    """
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename != __file__:
            frames.append(tb)
        tb = tb.tb_next

    lines = ["Traceback (most recent call last):"]
    for tb in frames[-max_frames:]:
        code = tb.tb_frame.f_code
        lines.append(f'  File "{code.co_filename}", line {tb.tb_lineno}, in {code.co_name}')
    lines.extend(s.rstrip("\n") for s in traceback.format_exception_only(type(exc), exc))
    return "\n".join(lines)


class _CappedBuffer(io.StringIO):
    """有上限的输出缓冲：超过上限就丢弃后续写入。

    超时后的代码仍在后台跑（Python 不能强杀线程），旧实现用无上限 StringIO，
    一个刷屏死循环就能把内存吃光；而且它会写进**下一次调用**的缓冲
    （sys.stdout 是进程级的）。这里截断并记录丢弃量，工具会告知模型。
    """

    def __init__(self, limit: int):
        super().__init__()
        self._limit = max(1000, int(limit))
        self.dropped = 0

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        room = self._limit - self.tell()
        if room <= 0:
            self.dropped += len(s)
            return len(s)
        if len(s) > room:
            self.dropped += len(s) - room
            s = s[:room]
        return super().write(s)


#: 允许执行外部程序 / 操作进程的 os 入口，一律拒绝
_OS_DENIED = frozenset({
    "system", "popen",
    "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "posix_spawn", "posix_spawnp",
    "fork", "forkpty", "startfile",
    "kill", "killpg", "abort", "_exit",
    "setuid", "setgid", "setsid", "putenv", "unsetenv",
})


def _restricted_os(real):
    """构造 os 的受限代理：拦掉"执行系统命令 / 结束进程"这一类入口。

    实测（2026-09-17）：真实 `os` 一旦暴露，`os.system('...')` 与 `os.popen(...)`
    可直接执行任意命令——它是属性调用而**不是 import**，所以 BLOCKED_IMPORTS
    那套钩子完全拦不住；终端工具的黑名单（格式化磁盘、del /s、git push -f…）
    在这里等于不存在。而且子进程的输出直接写到真实 fd 1，连 stdout 捕获都绕过。

    真实模块保存在**闭包**里、代理实例上不留任何可读引用。旧实现把它挂在实例属性
    `_real` 上，而 `__getattr__` 只在常规属性查找**失败**时才触发——`_real` 就在实例
    `__dict__` 里，查找成功，`__getattr__` 根本不会执行。于是
    `os._real.system('...')` / `vars(os)['_real'].popen(...)` 把整个 deny-list
    从后门绕了过去（2026-09-22 审计实测：命令真的执行了，且输出直连真实 fd 1）。

    现在代理是 `__slots__ = ()` 的空壳、没有任何实例属性，所有名字查找必然落到
    `__getattr__`；那里连前导下划线一并拒绝——`__dict__` / `_name` 之类同样可能
    是回到真实模块的跳板。

    注意定位：这是**护栏，不是沙箱**。``().__class__.__base__.__subclasses__()``
    仍能拿到 Popen 之类的类（实测可达），任何想要逃逸的代码都逃得掉。真正的隔离
    要靠 OS 级沙箱（SANDBOX_EXECUTION=appcontainer）与审批策略，这里只是让
    "顺手跑个命令"不再是一条无门槛的捷径。
    """
    denied = _OS_DENIED

    class _RestrictedOS:
        __slots__ = ()

        def __getattr__(self, name):
            # 前导下划线一律不给：`_real` 之外，`__dict__` 之类也可能成为跳板。
            if name in denied or name.startswith("_"):
                raise AttributeError(
                    f"安全限制：os.{name} 已被禁用（它可绕过终端的审批门执行系统命令）。"
                    "需要执行命令请用 terminal 工具（受审批策略管理）；"
                    "复制/移动文件请用 file 工具；不需要外部命令的话请改用纯 Python 实现。"
                )
            return getattr(real, name)

        def __setattr__(self, name, value):
            raise AttributeError("安全限制：受限命名空间里不允许修改 os 模块属性。")

        def __delattr__(self, name):
            raise AttributeError("安全限制：受限命名空间里不允许修改 os 模块属性。")

        def __dir__(self):
            """反射时表现得像真的 os（但隐藏被禁用的入口）。

            否则 `dir(os)` / `hasattr(os, 'makedirs')` 一类正常写法会给出错误答案，
            让模型以为环境不可用而放弃本来能做的事。
            """
            try:
                names = set(dir(real))
            except Exception:
                names = set()
            return sorted(n for n in names - denied if not n.startswith("_"))

        def __repr__(self):
            return "<受限 os 模块（护栏，非沙箱）>"

    return _RestrictedOS()


def build_safe_builtins() -> dict:
    """构造受限命名空间的内置项。

    以真实 builtins 为基底（而不是手挑几十个），再扣掉危险项、把 __import__
    换成黑名单版。这样 class 定义（依赖 __build_class__）、all/any/super/
    format/repr/各类异常等常规语法都可用——此前手写的内置表缺这些，导致
    Agent 写的普通代码（定义个类、用 all()）就报 NameError。
    """
    safe = {
        k: v for k, v in vars(_builtins).items()
        if k not in _UNSAFE_BUILTINS and not k.startswith("__")
    }
    safe["__import__"] = _safe_import
    # 类定义必须的内建（exec 时缺失会导致 class 语句 NameError）
    safe["__build_class__"] = getattr(_builtins, "__build_class__", None)
    if safe.get("__build_class__") is None:
        safe.pop("__build_class__")
    # exec 代码里常见的模块级变量（defaultdict/类定义/文档字符串都可能引用）
    safe["__name__"] = "__main__"
    safe["__doc__"] = None
    safe["__package__"] = None
    safe["__spec__"] = None
    return safe


class PythonTool(BaseTool):
    """Python 代码执行工具。"""

    risk_level: str = "medium"
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"

    @property
    def name(self) -> str:
        return "python"

    @property
    def description(self) -> str:
        return (
            "Python 代码执行工具。可以运行 Python 代码片段并获取输出。"
            "适合数据处理、计算、生成文件（Excel/CSV/文档）等场景。"
            "注意：受限环境不提供 subprocess（不能执行系统命令，请用 terminal 工具），"
            "os.system/os.popen 等也已禁用（需要跑命令请用 terminal，"
            "复制文件请用 file 的 copy）；"
            "可用 os/pathlib 读写文件、json/math/re/datetime 等常用模块；"
            "执行超时时间为 %d 秒（超时会直接失败）。"
            "**不要在本工具里用 sleep 轮询等待外部任务**——长任务（测试/构建/下载）"
            "请改用 terminal 的 background=true 启动，再用 bg output 查看输出。"
            "安全黑名单（导入即报错，别试）：subprocess/socket/shutil/sys/ctypes/"
            "winreg/multiprocessing/pickle/inspect；"
            "**复制/移动文件请直接用 file 工具的 copy/move，不要用 shutil**。"
            % int(_timeout_seconds())
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码",
                }
            },
            "required": ["code"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        return self.execute(str(arguments.get("code", "")))

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest
        code = str(arguments.get("code", "")).strip()
        # 代码中出现系统级操作痕迹视为高风险
        risky = any(kw in code for kw in (
            "subprocess", "socket", "ctypes", "winreg",
            "os.system", "os.popen", "os.spawn", "os.kill",
        ))
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"python 代码片段（{len(code)} 字符）",
            risk_level="high" if risky else "medium",
            min_sandbox_mode=self.min_sandbox_mode,
        )

    def execute(self, input_str: str) -> ToolResult:
        code = input_str.strip()
        if not code:
            return ToolResult(success=False, output="", error="代码为空。")

        # 捕获 stdout：用**有上限**的缓冲。超时的代码不会被杀死（Python 无法强杀
        # 线程），一个 `while True: print(...)` 会continue往缓冲里写；无上限的
        # StringIO 会一直涨，而 sys.stdout 是进程级的，下一次调用换了新缓冲后
        # 孤儿线程还会写进**新**缓冲，污染下一次的输出。
        old_stdout = sys.stdout
        sys.stdout = captured = _CappedBuffer(_CAPTURE_LIMIT)

        # 受限的全局命名空间
        # 安全收紧：__import__ 换成黑名单过滤版，禁止导入 subprocess 等系统级模块，
        # 防止绕过 terminal 审批门；其余内置项以真实 builtins 为基底（见 build_safe_builtins）。
        safe_globals = {
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

        try:
            # 超时保护：exec 无法被中断（sleep/死循环会一直挂），因此放到
            # 守护线程执行 + join 超时。超时后线程仍在后台（Python 无法强杀
            # 线程），但工具立即返回明确错误，不再拖到工具层 300s 硬超时。
            _timeout = _timeout_seconds()
            box: dict = {}

            def _run() -> None:
                try:
                    exec(code, safe_globals)
                    box["ok"] = True
                except BaseException as e:   # noqa: BLE001
                    box["err"] = e

            t = threading.Thread(target=_run, daemon=True, name="py-tool")
            t.start()
            t.join(_timeout)

            if t.is_alive():
                # 超时：尽力回传已产生的输出，便于判断卡在哪一步
                partial = captured.getvalue()
                return ToolResult(
                    success=False,
                    output=partial.strip()[:1000],
                    error=(f"python 代码执行超时（>{_timeout:.0f}s）。"
                           "不要在本工具里轮询等待外部任务；长任务请用 terminal 的 "
                           "background=true 启动、再用 bg output 查看输出。"),
                )

            if "err" in box:
                raise box["err"]

            output = captured.getvalue()
            dropped = getattr(captured, "dropped", 0)
            text = output.strip()
            if dropped:
                text += f"\n[输出过长，已丢弃 {dropped} 字符]"
            return ToolResult(
                success=True,
                output=text if text else "代码执行完成（无输出）。",
            )
        except BlockedImportError as e:
            # 黑名单命中：把「为什么 + 改用哪个工具」同时放进 output 和 error，
            # 模型无论读哪个字段都能立刻换路，不必反复试探。
            partial = captured.getvalue().strip()
            msg = str(e)
            return ToolResult(
                success=False,
                output=f"{partial}\n{msg}".strip() if partial else msg,
                error=msg,
            )
        except Exception as e:  # noqa: BLE001
            error_msg = _format_user_traceback(e)
            output = captured.getvalue()
            full_output = output + "\n" + error_msg if output else error_msg
            return ToolResult(
                success=False,
                output=full_output.strip(),
                # error 字段此前恒为「代码执行出错。」——模型据此认为报错无详情。
                # 现在带上异常类型与消息，一眼可见根因。
                error=f"{type(e).__name__}: {e}"[:400],
            )
        finally:
            sys.stdout = old_stdout
