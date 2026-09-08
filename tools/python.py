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

# 服务器进程里禁用/无意义的内置项：input 会阻塞后端、eval/exec/compile 与
# globals/locals 会破坏受限命名空间（沙箱逃逸面）
_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint", "exit", "quit",
    "help", "copyright", "credits", "license", "globals", "locals",
}


def _safe_import(name, *args, **kwargs):
    """受限 __import__：黑名单模块直接拒绝。"""
    root = name.split(".")[0]
    if root in BLOCKED_IMPORTS or name in BLOCKED_IMPORTS:
        raise ImportError(
            f"安全限制：Python 工具不允许导入模块 '{name}'。"
            "如确需执行系统命令，请使用 terminal 工具（受审批策略管理）。"
        )
    return __import__(name, *args, **kwargs)


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
            "执行超时时间为 30 秒。"
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
            exec(code, safe_globals)
            output = captured.getvalue()
            return ToolResult(
                success=True,
                output=output.strip() if output.strip() else "代码执行完成（无输出）。",
            )
        except Exception:
            error_msg = traceback.format_exc()
            output = captured.getvalue()
            full_output = output + "\n" + error_msg if output else error_msg
            return ToolResult(success=False, output=full_output.strip(), error="代码执行出错。")
        finally:
            sys.stdout = old_stdout
