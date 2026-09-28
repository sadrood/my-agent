"""工具基类模块：JSON Schema + 审批元数据。
每个工具用 schema 暴露 function calling 参数，并声明 risk_level / approval / min_sandbox_mode。
旧接口 execute(input_str) 保留；execute_json(arguments) 默认把结构化参数转成字符串。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict


# 沙箱等级（与上游宿主框架文件策略命名一致）
SANDBOX_LEVELS = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}

# 风险等级
RISK_LEVELS = {"low": 0, "medium": 1, "high": 2, "blocked": 3}


@dataclass
class ToolResult:
    """工具执行结果：支持截断标记。"""

    success: bool
    output: str
    error: str = ""
    truncated: bool = False          # output 是否被截断过
    original_length: int = 0         # 截断前长度
    metadata: Dict[str, Any] = field(default_factory=dict)  # 附加信息（如截图等）

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "truncated": self.truncated,
            "original_length": self.original_length,
            "metadata": self.metadata,
        }


@dataclass
class ApprovalRequest:
    """一次工具调用前的审批请求。"""

    tool_name: str
    arguments: Dict[str, Any]        # 结构化参数（可能为空）
    command: str = ""                # 人类可读的调用描述（用于展示与日志）
    risk_level: str = "low"          # low / medium / high / blocked
    reason: str = ""                 # 为什么需要审批（策略自动生成）
    min_sandbox_mode: str = "read-only"
    approval: str = "auto"           # auto / on-request（工具主动要求批准）


class BaseTool(ABC):
    """工具基类：所有工具都继承它。
    子类必须实现 name / description / execute(input_str)；schema、execute_json、审批元数据可选覆盖。
    """

    # ---- 审批元数据（子类按需覆盖） ----
    risk_level: str = "medium"          # low / medium / high / blocked
    approval: str = "auto"              # auto / on-request
    min_sandbox_mode: str = "workspace-write"  # read-only / workspace-write / danger-full-access

    # ---- 并行安全元数据（子类按需覆盖） ----
    parallel_safe: bool = False         # 类级默认：是否可与其他工具并行执行

    # ---- 增量输出回调（可选）：长任务工具执行中把部分输出推给上层 ----
    _output_callback = None             # cb(text: str) | None

    def set_output_callback(self, callback) -> None:
        """绑定增量输出回调 cb(text)：长任务工具（terminal 等）执行过程中
        逐段推送输出，供 executor 转发 dashboard 实时流；传 None 解除。"""
        self._output_callback = callback

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        """本轮参数下能否与其他工具并行执行（默认看类属性；子类可按参数细化）。"""
        return self.parallel_safe

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，如 "terminal"、"file"、"python"。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，用于告诉 LLM 该工具的能力和用法。"""
        ...

    @abstractmethod
    def execute(self, input_str: str) -> ToolResult:
        """执行工具（旧字符串接口，保留兼容）。"""
        ...

    # ================================================================
    # JSON Schema 与结构化调用
    # ================================================================

    @property
    def schema(self) -> dict:
        """工具的 JSON Schema（function calling 的 parameters）；子类应覆盖。
        默认接受一个可选 "input" 字符串参数（向后兼容）。
        """
        return {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": self.description[:300],
                }
            },
            "required": ["input"],
        }

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        """结构化入口：把 LLM 传入的 JSON 参数转成字符串调用 execute；子类应覆盖。"""
        if "input" in arguments:
            input_str = str(arguments["input"])
        elif arguments:
            import json as _json
            input_str = _json.dumps(arguments, ensure_ascii=False)
        else:
            input_str = ""
        return self.execute(input_str)

    def to_openai_schema(self) -> dict:
        """生成 function calling 所需的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024],
                "parameters": self.schema,
            },
        }

    def build_approval_request(self, arguments: Dict[str, Any]) -> ApprovalRequest:
        """根据结构化参数生成审批请求；子类覆写时必须带上 approval 元数据。
        ApprovalPolicy.decide 判的是 request.approval == "on-request"，漏传等于该分支永不触发，
        approval="on-request" 的工具会被静默降级成零确认执行。"""
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=self._describe_call(arguments),
            risk_level=self.risk_level,
            min_sandbox_mode=self.min_sandbox_mode,
            approval=self.approval,
        )

    def cancel(self) -> None:
        """中断正在执行的调用（如用户按"停止"时终止卡住的子进程）。
        默认空实现；会起外部进程或长时间阻塞的子类应覆盖，且必须线程安全。
        """
        return None

    def _describe_call(self, arguments: Dict[str, Any]) -> str:
        """人类可读的调用描述，用于审批提示与日志。"""
        import json as _json
        try:
            args_str = _json.dumps(arguments, ensure_ascii=False)
        except TypeError:
            args_str = str(arguments)
        if len(args_str) > 300:
            args_str = args_str[:300] + "..."
        return f"{self.name}({args_str})"


def truncate_output(output: str, max_chars: int) -> tuple:
    """截断工具输出，返回 (截断后文本, 是否截断, 原始长度)。"""
    if output is None:
        return "", False, 0
    original_length = len(output)
    if original_length <= max_chars:
        return output, False, original_length
    # 头部保留 80%，尾部保留 20%（头部信息往往最重要）
    head_chars = int(max_chars * 0.8)
    tail_chars = max_chars - head_chars - 50
    if tail_chars < 100:
        tail_chars = 100
        head_chars = max_chars - tail_chars - 50
    truncated = (
        output[:head_chars]
        + f"\n\n...[输出过长，中间部分已省略，原始长度 {original_length} 字符]...\n\n"
        + output[-tail_chars:]
    )
    return truncated, True, original_length
