"""
Python 代码执行工具模块（v2：JSON Schema + 受限命名空间收紧）。
安全地在受限环境中执行 Python 代码片段并返回结果。

v2 变化：
- schema: {"code": string}
- 安全收紧（对齐同类实现的沙箱思路）：从受限全局命名空间移除 subprocess/sys，
  防止模型绕过 terminal 工具的审批门去执行任意系统命令；
  保留 os/pathlib 用于文件操作（与 Planner 提示词中 os.makedirs 用法兼容）。
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
    """受限 __import__：黑名单模块直接拒绝。"""
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

        # 捕获 stdout
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()

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
            "os": __import__("os"),
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
            return ToolResult(
                success=True,
                output=output.strip() if output.strip() else "代码执行完成（无输出）。",
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
