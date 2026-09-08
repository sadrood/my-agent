"""
delegate 工具：让内置 Agent 主动把子任务委托给外部 CLI 引擎执行。

设计意图（用户原始构想）：不是让用户手动挑引擎，而是内置 Agent 自己当编排者——
遇到适合外部专业 CLI 的子任务（大型重构/独立评审/脚本工程等）时，
用本工具把"自包含的子任务"委托出去，拿回完整输出后验收、汇总、继续主线。

- 子任务必须自包含：外部引擎看不到本会话上下文，goal 要写清背景/约束/交付物。
- 委托出去的引擎按它们自己的权限设置运行（独立权限，不经本会话审批门）。
- 嵌套上限：delegate 里再 delegate（引擎调 my-agent 再委托）最多 2 层，防失控循环。
"""
import json
import os
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

# 嵌套委托深度上限（env 由 delegate 自身维护：委托前 +1，结束后还原）
MAX_DEPTH = int(os.getenv("MY_AGENT_DELEGATE_MAX_DEPTH", "2"))
_DEPTH_ENV = "MY_AGENT_DELEGATE_DEPTH"

_EXTERNAL_ONLY = ("claude", "codex", "custom")
# claude-acp 需要独立 ACP 会话协议与网关模型配置，暂不作为工具委托目标
_EXTRA = ("claude-acp",)

_SUPPORT_HINT = (
    "委托给外部 CLI 引擎执行一个【自包含子任务】并返回它的完整输出。"
    "用法：想清楚子任务的背景/约束/验收标准写进 goal（外部引擎看不到本会话上下文），"
    "完成后验收其输出，继续主线并最终自己汇总作答。"
)


class DelegateTool(BaseTool):
    """把子任务交给外部 CLI 引擎（claude / codex / custom 命令模板）执行。"""

    name = "delegate"
    description = (
        _SUPPORT_HINT
        + " 支持引擎：claude（文本输出 CLI）、codex（JSON 事件流 CLI）、"
        "custom（自定义命令模板，需传 command_template 且含 {goal}）。"
        "引擎按各自权限独立运行；单次委托最长约 30 分钟。"
    )
    risk_level: str = "high"      # 引擎可独立执行代码/写盘（独立权限）→ 高风险面
    min_sandbox_mode: str = "workspace-write"
    parallel_safe: bool = False

    schema = {
        "type": "object",
        "properties": {
            "runtime": {
                "type": "string",
                "enum": list(_EXTERNAL_ONLY),
                "description": "目标引擎：claude / codex / custom（需已安装）",
            },
            "goal": {
                "type": "string",
                "description": "自包含子任务（背景/约束/验收标准/交付位置都要写清楚）",
            },
            "command_template": {
                "type": "string",
                "description": "仅 custom：命令模板，必须含 {goal}，如 python my_runner.py {goal}",
            },
            "cwd": {
                "type": "string",
                "description": "可选：子任务工作目录（默认当前工作区）",
            },
        },
        "required": ["runtime", "goal"],
    }

    def __init__(self):
        self._stop_event = None

    def set_stop_event(self, event) -> None:
        """执行器/上层注入停止信号：置位后子进程树会被终止。"""
        self._stop_event = event

    # ---------------------------------------------------------------
    # 兼容旧字符串接口：支持 JSON 参数或 "runtime goal" 两段式
    # ---------------------------------------------------------------
    def execute(self, input_str: str) -> ToolResult:
        text = input_str.strip()
        if not text:
            return ToolResult(success=False, output="", error="缺少参数：需要 runtime 与 goal。")
        if text.startswith("{"):
            try:
                return self.execute_json(json.loads(text))
            except Exception as e:
                return ToolResult(success=False, output="", error=f"参数解析失败: {str(e)[:120]}")
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return ToolResult(success=False, output="", error="格式：delegate <runtime> <goal>")
        return self.execute_json({"runtime": parts[0], "goal": parts[1]})

    # ---------------------------------------------------------------
    # 结构化入口
    # ---------------------------------------------------------------
    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        runtime = str(arguments.get("runtime") or "").strip()
        goal = str(arguments.get("goal") or "").strip()
        if runtime not in _EXTERNAL_ONLY:
            return ToolResult(
                success=False, output="",
                error=f"delegate 目标必须是 {list(_EXTERNAL_ONLY)}（已安装才可用）；收到: {runtime or '(空)'}",
            )
        if not goal:
            return ToolResult(success=False, output="", error="goal 不能为空（子任务要自包含）。")
        if len(goal) > 6000:
            return ToolResult(success=False, output="", error="goal 过长（>6000 字），请精简后重试。")

        # 嵌套深度防护：防止 引擎→my-agent→delegate→引擎 无限递归
        depth = int(os.getenv(_DEPTH_ENV, "0") or 0)
        if depth >= MAX_DEPTH:
            return ToolResult(
                success=False, output="",
                error=f"delegate 嵌套已达上限（{MAX_DEPTH} 层），拒绝继续委托——请在本层直接完成任务。",
            )

        # 可用性探测（custom 恒可用：模板由调用方给）
        if runtime != "custom":
            try:
                from agent.runtime import detect_available
                if not detect_available().get(runtime):
                    return ToolResult(
                        success=False, output="",
                        error=f"引擎 {runtime} 未安装（PATH 里找不到可执行文件）。"
                              "可用：delegate custom + command_template 交给自定义 CLI。",
                    )
            except Exception as e:
                return ToolResult(success=False, output="", error=f"引擎探测失败: {str(e)[:150]}")

        cfg = None
        if runtime == "custom":
            template = str(arguments.get("command_template") or "").strip()
            if "{goal}" not in template:
                return ToolResult(
                    success=False, output="",
                    error="custom 引擎需要 command_template 且其中含 {goal}，如 python my_runner.py {goal}",
                )
            cfg = {"command": template}

        cwd = str(arguments.get("cwd") or "").strip() or os.getcwd()
        if not os.path.isdir(cwd):
            return ToolResult(success=False, output="", error=f"cwd 不存在: {cwd}")

        # 组装事件收集器：stream_delta 逐行收进缓冲；answer/run_end 记录状态
        lines: list = []
        status = {"ok": False}

        def on_event(kind: str, data: dict):
            try:
                if kind == "stream_delta":
                    text = str(data.get("text") or "")
                    if text:
                        lines.append(text)
                        cb = self._output_callback
                        if cb is not None:
                            try:
                                cb(text)
                            except Exception:
                                pass
                elif kind == "answer":
                    text = str(data.get("output") or "")
                    if text:
                        lines.append(text)
                elif kind == "run_end":
                    status["ok"] = data.get("status") == "completed"
            except Exception:
                pass

        from agent.runtime import run_external

        os.environ[_DEPTH_ENV] = str(depth + 1)   # 子进程继承 → 链内再次 delegate 会被限层
        try:
            try:
                run_external(runtime, goal, cwd, on_event,
                             stop_event=self._stop_event, cfg=cfg)
            except Exception as e:
                return ToolResult(success=False, output="", error=f"delegate 执行异常: {str(e)[:200]}")
        finally:
            os.environ[_DEPTH_ENV] = str(depth)

        output = "".join(lines).strip() or "(引擎无输出)"
        return ToolResult(
            success=status["ok"],
            output=output,
            error="" if status["ok"] else "外部引擎执行失败（退出码非 0 或被停止），见上方输出。",
            metadata={"runtime": runtime, "delegated": True, "nested_depth": depth + 1},
        )
