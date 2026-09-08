"""
工具基类模块（v2：JSON Schema + 审批元数据，借鉴同类框架工具设计）。

设计要点（与主流开源 agent 框架实现对齐）：
1. 每个工具通过 ``schema`` 暴露 JSON Schema（OpenAI function calling 格式），
   LLM 以结构化参数调用，而不是解析自由文本。
2. ``ToolResult`` 支持截断标记（truncated），超大输出只回喂摘要，节省 token。
3. 每个工具声明审批元数据：
   - risk_level: low / medium / high / blocked（风险等级，供 Guardian 与审批门使用）
   - approval: auto / on-request（auto = 由审批策略统一决定；on-request = 工具主动要求批准）
   - min_sandbox_mode: 该工具要求的最低沙箱等级（read-only / workspace-write / danger-full-access）

向后兼容：所有旧工具仍然实现 ``execute(input_str)``（字符串接口）；
新接口 ``execute_json(arguments)`` 默认把结构化参数转为字符串调用 ``execute``。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# 沙箱等级（与上游宿主框架文件策略命名一致）
SANDBOX_LEVELS = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}

# 风险等级
RISK_LEVELS = {"low": 0, "medium": 1, "high": 2, "blocked": 3}


@dataclass
class ToolResult:
    """工具执行结果（v2：支持截断标记）。"""

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
    """
    工具基类（v2）。
    所有工具（Terminal、File、Python、Browser 等）都需要继承此类。

    子类必须实现：
    - name / description
    - execute(input_str)（旧字符串接口，保留兼容）
    可选覆盖：
    - schema（JSON Schema；默认基于 description 的占位 schema）
    - execute_json(arguments)（结构化入口，默认转字符串）
    - risk_level / approval / min_sandbox_mode（审批元数据）
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
        """
        执行工具（旧字符串接口，保留兼容）。

        Args:
            input_str: 工具输入参数（自然语言或结构化字符串）。

        Returns:
            ToolResult 对象。
        """
        ...

    # ================================================================
    # v2：JSON Schema 与结构化调用
    # ================================================================

    @property
    def schema(self) -> dict:
        """
        工具的 JSON Schema（OpenAI function calling 的 parameters 字段）。
        子类应覆盖此属性提供精确 schema。

        默认实现：接受一个可选的 "input" 字符串参数（向后兼容）。
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
        """
        结构化入口：接收 LLM 传入的 JSON 参数，转为字符串调用 execute。

        默认实现：把 arguments 序列化为字符串（或取 "input" 字段）。
        子类应覆盖以生成精确的命令字符串。
        """
        if "input" in arguments:
            input_str = str(arguments["input"])
        elif arguments:
            import json as _json
            input_str = _json.dumps(arguments, ensure_ascii=False)
        else:
            input_str = ""
        return self.execute(input_str)

    def to_openai_schema(self) -> dict:
        """生成 OpenAI function calling 所需的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024],
                "parameters": self.schema,
            },
        }

    def build_approval_request(self, arguments: Dict[str, Any]) -> ApprovalRequest:
        """根据结构化参数生成审批请求（子类可覆盖以细化风险/描述）。"""
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=self._describe_call(arguments),
            risk_level=self.risk_level,
            min_sandbox_mode=self.min_sandbox_mode,
        )

    def cancel(self) -> None:
        """
        Interrupt the currently running call (e.g. terminate a stuck
        subprocess when the user presses "Stop").

        Called by Executor from another thread once stop is requested;
        default is a no-op. Subclasses that spawn external processes or
        block for a long time should override this to free resources
        promptly. Must be thread-safe: cancel() may enter concurrently.
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
    """
    截断工具输出，返回 (截断后文本, 是否截断, 原始长度)。

    Args:
        output: 原始输出文本
        max_chars: 最大字符数

    Returns:
        (text, truncated, original_length)
    """
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
