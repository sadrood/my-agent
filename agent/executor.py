"""
执行器模块（v3：原生 function calling + 审批门 + Guardian + Rollout）。

v3 核心变化（对齐主流 agent 执行协议）：
1. 主路径使用原生 function calling（LLM.chat_with_tools），
   模型以 JSON Schema 参数调用工具，不再正则解析自由文本 JSON。
2. 每个计划步骤内部自动多轮工具调用（模型收到工具结果后继续决策），
   直到模型输出文字总结 → 该步骤完成。
3. 每次工具调用经过三层把关（参考同类开源实现）：
   - ApprovalPolicy 审批策略（沙箱等级 + 风险分级 + 人工确认）
   - Guardian 安全审校（中/高风险调用由审校模型二次把关）
   - 工具自身黑名单（如终端危险命令）
4. Rollout 事件流：所有模型/工具/审批事件写入 JSONL 追踪文件，
   步骤内消息超过 token 阈值时自动压缩（compaction）。
5. 向后兼容：若供应商不支持 function calling，自动回退到旧文本 JSON 协议
   （execute_step_legacy），返回结构保持兼容。
"""
import json
import logging
import os
import re
import time
import base64
import threading
from typing import Callable, Optional

from models.llm import LLM, LLMToolResponse, ToolCall, unwrap_raw_arguments
from models.prompts import (
    EXECUTOR_SYSTEM_PROMPT,
    EXECUTOR_BROWSER_SYSTEM_PROMPT,
    EXECUTOR_VISION_BROWSER_PROMPT,
    EXECUTOR_USER_PROMPT_TEMPLATE,
    EXECUTOR_FC_SYSTEM_PROMPT,
    EXECUTOR_FC_USER_PROMPT_TEMPLATE,
    APPROVAL_NOTICE_TEMPLATE,
    VISION_PAGE_ANALYSIS_QUESTION,
    VISION_STEP_RESULT_QUESTION,
)
from tools.base import ToolResult
from tools.tool_manager import ToolManager
from config import VISION_CONFIG, TOOL_CONFIG, APPROVAL_CONFIG, COMPACT_CONFIG

logger = logging.getLogger(__name__)

# think 伪工具的 schema（不注册进 ToolManager，由 Executor 拦截处理）
THINK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "think",
        "description": (
            "记录你的推理过程。在想清楚下一步该做什么、分析工具结果、"
            "或制定小计划时调用。这个工具不产生副作用，输出不会用于后续决策。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "你的完整推理内容"}
            },
            "required": ["thought"],
        },
    },
}


class Executor:
    """任务执行器（v3：function calling + 安全把关）。"""

    BROWSER_KEYWORDS = [
        "浏览器", "网页", "打开", "访问", "搜索", "点击",
        "截图", "登录", "填写", "提交", "表单", "导航",
        "browser", "url", "http", "www", "网站", "页面",
        "上网", "浏览", "查看", "下载", "上传", "链接",
        "百度", "谷歌", "google", "github",
    ]

    def __init__(
        self,
        tool_manager: Optional[ToolManager] = None,
        llm: Optional[LLM] = None,
        enable_vision: bool = None,
        approval_policy=None,
        guardian=None,
        rollout=None,
        instructions_text: str = "",
        max_step_ops: int = None,
        llm_retry_delay: float = 1.5,
        snapshot_checkpoints: bool = False,   # 逐操作式：edit/write 前 git 检查点
        snapshot_dir: str = "",               # 检查点的 git 工作目录
    ):
        self.tool_manager = tool_manager or ToolManager()
        self.llm = llm or LLM()
        self.enable_vision = (
            enable_vision
            if enable_vision is not None
            else VISION_CONFIG.get("enabled", True)
        )
        self.approval = approval_policy          # ApprovalPolicy | None（None=全部放行）
        self.guardian = guardian                 # Guardian | None
        self.rollout = rollout                   # Rollout | None
        self._event_sink = None                  # execute_goal_loop 期间的事件回调（→ dashboard）
        self.instructions_text = instructions_text
        self.max_step_ops = max_step_ops or TOOL_CONFIG.get("max_step_ops", 12)
        self.llm_retry_delay = llm_retry_delay   # 轮级重试退避（秒）
        self.snapshot_checkpoints = snapshot_checkpoints
        self.snapshot_dir = snapshot_dir or os.getcwd()

        self._vision_model = None                # 延迟加载
        self._fc_supported: Optional[bool] = None  # None=未探测, True/False=已知
        self.metrics = None                      # RunMetrics（由 Agent 注入）
        self._hooks = None                       # HookManager（首次工具调用时延迟获取）

    @property
    def vision_model(self):
        """延迟加载视觉模型。"""
        if self._vision_model is None and self.enable_vision:
            try:
                from models.vision import VisionModel
                self._vision_model = VisionModel()
            except Exception:
                self._vision_model = None
        return self._vision_model

    # ================================================================
    # 步骤执行（v3 主入口）
    # ================================================================

    def execute_step(
        self,
        goal: str,
        current_step: str,
        history_summary: str,
        step_context: str = "",
        page_screenshot_base64: str = "",
        vision_feedback: str = "",
        failure_warnings: str = "",
    ) -> dict:
        """
        执行单个计划步骤。

        优先使用 function calling 协议；若供应商不支持（或首次调用报错），
        自动回退到旧文本 JSON 协议。

        Returns:
            结果字典，兼容旧字段（step/action/tool/tool_input/success/output/error/status）
            新增字段: tools_used(list), tool_calls(list[dict])
        """
        if self._fc_supported is not False:
            try:
                result = self._execute_step_fc(
                    goal=goal,
                    current_step=current_step,
                    history_summary=history_summary,
                    step_context=step_context,
                    vision_feedback=vision_feedback,
                    failure_warnings=failure_warnings,
                )
                self._fc_supported = True
                return result
            except Exception as e:
                # 首次失败：可能是不支持 tools 的供应商 → 回退旧协议
                if self._fc_supported is None and self._looks_like_unsupported(e):
                    self._fc_supported = False
                    return self.execute_step_legacy(
                        goal=goal,
                        current_step=current_step,
                        history_summary=history_summary,
                        step_context=step_context,
                        vision_feedback=vision_feedback,
                        failure_warnings=failure_warnings,
                    )
                # 其它异常：按失败返回
                return {
                    "step": current_step,
                    "action": "use_tool",
                    "success": False,
                    "output": "",
                    "status": "failed",
                    "error": f"执行失败: {e}",
                    "tools_used": [],
                    "tool_calls": [],
                }
        return self.execute_step_legacy(
            goal=goal,
            current_step=current_step,
            history_summary=history_summary,
            step_context=step_context,
            vision_feedback=vision_feedback,
            failure_warnings=failure_warnings,
        )

    def _looks_like_unsupported(self, error: Exception) -> bool:
        """
        判断异常是否为供应商不支持 tools 所致。

        注意（教训）：不要匹配宽泛的 "tools"/"function"/"invalid_request_error"——
        思考模式的 reasoning 回传 400 也带 invalid_request_error，误判会
        把"上游协议错误"当成"不支持 function calling"而错误回退计划模式。
        """
        msg = str(error).lower()
        return any(k in msg for k in (
            "not supported", "unsupported", "does not support",
            "doesn't support", "tool support is not",
        ))

    # ================================================================
    # v3 主路径：function calling
    # ================================================================

    def _execute_step_fc(
        self,
        goal: str,
        current_step: str,
        history_summary: str,
        step_context: str = "",
        vision_feedback: str = "",
        failure_warnings: str = "",
    ) -> dict:
        """function calling 主循环：模型持续调用工具直到输出文字总结。"""
        tools = self.tool_manager.list_openai_schemas()
        tools.append(THINK_TOOL_SCHEMA)

        # 系统提示：基础 + 审批策略提示 + AGENTS.md 指令
        system_prompt = EXECUTOR_FC_SYSTEM_PROMPT
        if self.approval is not None:
            system_prompt += APPROVAL_NOTICE_TEMPLATE.format(
                approval_policy=self.approval.mode,
                sandbox_mode=self.approval.sandbox_mode,
            )
        if self.instructions_text:
            system_prompt += f"\n\n## 项目指令（必须遵守）\n{self.instructions_text}"

        context_parts = []
        if step_context:
            context_parts.append(f"\n当前步骤内已执行的操作:\n{step_context}\n")
        if vision_feedback:
            context_parts.append(f"\n【视觉分析结果】\n{vision_feedback}\n请基于以上分析决定下一步。\n")
        if failure_warnings:
            context_parts.append(f"\n【警告：已知失败模式】\n{failure_warnings}\n请避开以上错误模式。\n")

        user_prompt = EXECUTOR_FC_USER_PROMPT_TEMPLATE.format(
            goal=goal,
            current_step=current_step,
            history=history_summary or "（无）",
            step_context="".join(context_parts[:1]) or "",
            vision_feedback="".join(context_parts[1:2]) or "",
            failure_warnings="".join(context_parts[2:3]) or "",
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        tool_calls_log = []   # 步骤内全部工具调用记录
        errors: list = []
        final_text = ""
        thinking_mode_seen = False   # 本对话是否出现过推理字段（thinking 服务需逐条回传）

        self._emit("step_start", {"step": current_step})

        for _ in range(self.max_step_ops):
            # 上下文压缩（阈值与循环模式同一来源：窗口比例制）
            if self.rollout is not None:
                messages = self.rollout.maybe_compact(
                    messages, goal, max_tokens=self._compact_threshold())

            response = self.llm.chat_with_tools(messages, tools)
            # 思考模式会话：出现过推理字段后，后续每条 assistant 消息都要回传该字段
            if getattr(response, "reasoning_present", False) or getattr(response, "reasoning", ""):
                thinking_mode_seen = True
            self._emit("model_turn", {
                "content": response.content[:300],
                "tool_calls": [tc.name for tc in response.tool_calls],
                "finish_reason": response.finish_reason,
                # 诊断埋点：思考模式的 reasoning 是否被捕获（未捕获会导致下一轮 400）
                "reasoning_len": len(getattr(response, "reasoning", "") or ""),
            })

            # 模型输出文字且没有工具调用 → 步骤完成
            if not response.tool_calls:
                final_text = response.content.strip()
                break

            # 处理本轮的每个工具调用
            assistant_msg = {
                "role": "assistant",
                "content": response.content or "",
            }
            # 思考模式：reasoning 必须在下一轮回传，否则服务器 400
            if response.reasoning:
                assistant_msg[response.reasoning_field or "reasoning_content"] = response.reasoning
            elif thinking_mode_seen:
                # 本回合跳过思考（无推理增量）也要带空字段：thinking 服务仍要求回传
                assistant_msg[response.reasoning_field or "reasoning_content"] = ""
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{len(tool_calls_log)}",
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                call_id = tc.id or f"call_{len(tool_calls_log)}"
                result, blocked_reason = self._dispatch_tool_call(tc.name, tc.arguments, goal)

                tool_calls_log.append({
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "success": result.success,
                    "output": (result.output or result.error)[:300],
                    "blocked_reason": blocked_reason,
                })
                if not result.success and not blocked_reason:
                    errors.append(result.error)

                result_text = result.output
                if not result.success:
                    result_text = f"执行失败: {result.error}" if result.error else "执行失败（无错误信息）"
                if blocked_reason:
                    result_text = f"被拦截: {blocked_reason}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_text,
                })
        else:
            # 达到步骤内最大操作数
            final_text = final_text or "达到该步骤最大操作次数。"

        # 组装结果
        last_call = tool_calls_log[-1] if tool_calls_log else None
        success = len(errors) == 0
        output = final_text or (last_call["output"] if last_call else "")

        self._emit("step_end", {
            "step": current_step,
            "success": success,
            "tool_call_count": len(tool_calls_log),
            "final_text": final_text[:300],
        })

        return {
            "step": current_step,
            "action": "use_tool" if tool_calls_log else "think",
            "tool": last_call["name"] if last_call else "",
            "tool_input": (
                json.dumps(last_call["arguments"], ensure_ascii=False)[:200]
                if last_call else ""
            ),
            "success": success,
            "output": output,
            "error": "；".join(errors[:3]) if errors else "",
            "status": "completed" if success else "failed",
            "reasoning": final_text,
            "tools_used": list(dict.fromkeys(c["name"] for c in tool_calls_log)),
            "tool_calls": tool_calls_log,
        }

    # ================================================================
    # v3.1：单循环执行（主循环式：一次持续对话完成整个目标）
    # ================================================================

    def execute_goal_loop(
        self,
        goal: str,
        system_prompt: str,
        context_text: str = "",
        max_ops: int = None,
        event_sink: Optional[Callable[[str, dict], None]] = None,
        temperature: float = None,
        top_p: float = None,
        max_tokens: int = None,
        stream: bool = True,
        on_turn_start: Optional[Callable[[], None]] = None,
        on_text_delta: Optional[Callable[[str, str], None]] = None,
        stop_event=None,   # threading.Event | None：置位后在下个检查点优雅停止
    ) -> dict:
        """
        单循环执行：一轮持续对话完成整个目标。

        模型可以自由地"思考 → 调用工具 → 看结果 → 继续"，直到输出
        纯文本最终回答为止。不再有独立规划/逐步执行/总结三层调用。

        Args:
            goal: 用户目标
            system_prompt: 系统提示（含人格、规则、审批提示、AGENTS.md）
            context_text: 附加上下文（对话历史、经验、失败模式警告等）
            max_ops: 整次任务最大工具操作轮数（默认取配置的 2 倍）
            event_sink: 事件回调 (event_type, data) → dashboard/打印
            temperature: 覆盖默认温度
            top_p: 覆盖默认核采样（None=不显式设置）
            max_tokens: 覆盖默认最大输出 token
            stream: 是否流式输出（逐字渲染）
            on_turn_start: 每轮模型调用开始前的回调（显示思考中状态）
            on_text_delta: 流式增量回调 (kind, text)，kind ∈ {"reasoning", "text"}

        Returns:
            {
              "success": bool,
              "output": 最终回答文本,
              "tool_calls": [{"name","arguments","success","output","blocked_reason"}],
              "errors": [str],
              "ops": 实际模型轮数,
            }
        """
        tools = self.tool_manager.list_openai_schemas()
        tools.append(THINK_TOOL_SCHEMA)

        # 整个执行期间保存事件回调：工具实时输出流 / checkpoint 转发 dashboard 用
        self._event_sink = event_sink

        user_content = goal if not context_text else f"{goal}\n\n{context_text}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_calls_log: list = []
        errors: list = []
        last_success_idx = -1   # 最后一次成功工具调用的序号
        last_failure_idx = -1   # 最后一次失败工具调用的序号
        consecutive_empty = 0   # 连续空回复次数（上游偶发只回 reasoning 就 stop）
        no_tools_fallback_used = False   # 本轮是否已尝试过纯文本回退
        thinking_mode_seen = False   # 本对话是否出现过推理字段（thinking 服务需逐条回传）
        max_ops = max_ops or int(TOOL_CONFIG.get("max_loop_ops", 40))

        self._emit("run_loop_start", {"goal": goal})

        # 停止信号注入工具层：terminal 等工具在执行期间轮询该信号，
        # 用户点"停止"时正在运行的子进程能被立即终止，而不是干等到命令结束
        if stop_event is not None:
            try:
                self.tool_manager.bind_stop_event(stop_event)
            except Exception:
                pass

        warned_near_limit = False
        for turn in range(max_ops):
            # 停止检查点：每轮开始前，用户点"停止"后优雅退出
            if stop_event is not None and stop_event.is_set():
                try:
                    self.tool_manager.cancel_active_tools()
                except Exception:
                    pass
                self._emit("run_loop_end", {"success": False, "stopped": True})
                return {
                    "success": False,
                    "output": "已按要求停止执行。",
                    "tool_calls": tool_calls_log,
                    "errors": errors,
                    "ops": turn,
                    "stopped": True,
                }
            # 接近上限预警（80% 时提醒一次）
            if not warned_near_limit and turn >= int(max_ops * 0.8):
                self._emit("ops_warning", {
                    "turn": turn + 1, "max_ops": max_ops,
                    "message": f"已用 {turn + 1}/{max_ops} 轮，接近上限。",
                })
                warned_near_limit = True

            # 统计：模型轮数
            if self.metrics is not None:
                self.metrics.turns += 1

            # 常驻状态栏锚点：每轮模型调用前发 turn_start
            # （event_sink 由 Agent 层渲染 token 计数/沙箱/策略并转发 rollout）
            if event_sink is not None:
                try:
                    event_sink("turn_start", {"turn": turn + 1, "max_ops": max_ops})
                except Exception:
                    pass

            # 上下文压缩：token 压力超阈值时把旧历史总结成摘要
            # （阈值 = 窗口比例制：_compact_threshold → llm.compact_threshold_tokens）
            if COMPACT_CONFIG.get("enabled", True):
                messages = self._maybe_compact(messages)

            # 流式优先；供应商不支持时回退一次性调用。
            # 轮级重试：普通临时错误最多 3 次；429 限流最多 6 次 + 指数退避（借鉴同类实现）。
            response = None
            llm_error = None
            rate_limited = False
            for attempt in range(6):
                # 非限流错误最多重试 3 次（0/1/2）即放弃；限流走满 6 次
                if llm_error is not None and not rate_limited and attempt >= 3:
                    break
                if attempt > 0:
                    if rate_limited:
                        delay = min(2 ** (attempt - 1), 30)   # 2/4/8/16/30 指数退避
                    else:
                        delay = self.llm_retry_delay * attempt
                    self._emit("llm_retry", {
                        "turn": turn + 1, "attempt": attempt,
                        "reason": "rate_limit" if rate_limited else "retry",
                        "error": str(llm_error)[:200],
                    })
                    time.sleep(delay)
                try:
                    if on_turn_start is not None:
                        try:
                            on_turn_start()
                        except Exception:
                            pass
                    if stream and hasattr(self.llm, "chat_with_tools_stream"):
                        response = self._stream_turn(messages, tools, on_text_delta, temperature, top_p, max_tokens)
                    else:
                        response = self.llm.chat_with_tools(
                            messages, tools, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
                        )
                    llm_error = None
                    break
                except Exception as e:
                    llm_error = e
                    self._emit("llm_error", {
                        "turn": turn + 1, "attempt": attempt + 1,
                        "error": str(e)[:300],
                    })
                    if self._looks_like_unsupported(e):
                        raise   # 交由 Agent 回退经典计划模式
                    if self._is_rate_limit(e):
                        rate_limited = True   # 429 限流：走满 6 次指数退避重试

            if llm_error is not None:
                # 三次尝试全部失败：记录并优雅收尾（保留已完成的工具成果）
                self._emit("run_loop_end", {"success": False, "error": str(llm_error)[:200]})
                partial = self._render_partial_progress(tool_calls_log, errors)
                err_text = str(llm_error)
                # 常见错误的诊断提示
                hint = ""
                if "401" in err_text:
                    hint = (
                        "\n（401 = 认证失败：检查 API key 是否有效、被停用，或网关是否有 IP 白名单限制。"
                        "可运行 my-agent --doctor 验证连通，/config 查看当前 key）"
                    )
                elif "429" in err_text:
                    hint = ("\n（429 = 限流：已按指数退避多次重试仍失败。"
                            "目标已保存，稍后重发即可续跑——上下文已保留）")
                return {
                    "success": False,
                    "output": (partial or f"模型调用连续失败：{err_text}") + hint,
                    "tool_calls": tool_calls_log,
                    "errors": errors + [err_text],
                    "ops": turn + 1,
                    "llm_error": err_text[:300],
                }

            # 思考模式会话：出现过推理字段后，后续每条 assistant 消息都要回传该字段
            if getattr(response, "reasoning_present", False) or getattr(response, "reasoning", ""):
                thinking_mode_seen = True

            self._emit("model_turn", {
                "content": response.content[:300],
                "tool_calls": [tc.name for tc in response.tool_calls],
                "finish_reason": response.finish_reason,
                # 诊断埋点：思考模式的 reasoning 是否被捕获（未捕获会导致下一轮 400）
                "reasoning_len": len(getattr(response, "reasoning", "") or ""),
            })

            # 模型输出文字且不再调用工具 → 这就是最终回答
            if not response.tool_calls:
                final = response.content.strip()
                if not final and turn < max_ops - 1 and consecutive_empty < 2:
                    # 防御：上游偶发只输出 reasoning 就 stop（空回复），
                    # 推一条提示让模型继续，最多重试 2 次
                    consecutive_empty += 1
                    self._emit("empty_turn", {"turn": turn + 1, "retry": consecutive_empty})
                    if thinking_mode_seen:
                        # 思考模式：空回复的 assistant 消息也要带推理字段（可为空串）
                        empty_assistant = {"role": "assistant", "content": ""}
                        empty_assistant[getattr(response, "reasoning_field", "reasoning_content") or "reasoning_content"] = \
                            getattr(response, "reasoning", "") or ""
                        messages.append(empty_assistant)
                    messages.append({
                        "role": "user",
                        "content": "（你的上一条回复为空。请继续完成任务：直接输出答案，或调用工具。）",
                    })
                    continue
                if not final and consecutive_empty >= 2 and not no_tools_fallback_used:
                    # 上游 tools 服务降级（带 tools 的请求只吐 reasoning 不吐正文）：
                    # 回退纯文本问一次，至少给用户一个文字回答
                    no_tools_fallback_used = True
                    self._emit("no_tools_fallback", {"turn": turn + 1})
                    messages.append({
                        "role": "user",
                        "content": "（工具调用当前不可用。请直接用文字回答，或说明无法完成的原因。）",
                    })
                    try:
                        if hasattr(self.llm, "chat"):
                            text = self.llm.chat(messages)
                            if text and text.strip():
                                self._emit("run_loop_end", {
                                    "success": False, "output": text[:300],
                                    "fallback": "no-tools",
                                })
                                return {
                                    # 降级回退：保守记为未成功（无法验证任务是否真正达成），
                                    # 避免"没搞成"被记成成功污染经验库
                                    "success": False,
                                    "output": text.strip(),
                                    "tool_calls": tool_calls_log,
                                    "errors": errors + ["上游工具调用降级，已回退纯文本回答"],
                                    "ops": turn + 1,
                                    "no_tools_fallback": True,
                                }
                    except Exception:
                        pass
                    final = ""
                consecutive_empty = 0
                # 成功 = 有最终回答 且 没有未补救的失败
                # （空回复不算成功——上游降级时"无文字总结"绝不能记成成功）
                success = bool(final) and last_success_idx >= last_failure_idx
                self._emit("run_loop_end", {"success": success, "output": final[:300]})
                return {
                    "success": success,
                    "output": final,
                    "tool_calls": tool_calls_log,
                    "errors": errors,
                    "ops": turn + 1,
                }
            consecutive_empty = 0

            # 本轮的工具调用
            assistant_msg = {
                "role": "assistant",
                "content": response.content or "",
            }
            # 思考模式：reasoning 必须在下一轮回传，否则服务器 400
            if response.reasoning:
                assistant_msg[response.reasoning_field or "reasoning_content"] = response.reasoning
            elif thinking_mode_seen:
                # 本回合跳过思考（无推理增量）也要带空字段：thinking 服务仍要求回传
                assistant_msg[response.reasoning_field or "reasoning_content"] = ""
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id or f"call_{len(tool_calls_log)}",
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
            messages.append(assistant_msg)

            # 本轮工具调用：批量执行。整批并行安全且审批无需交互时并发
            # （读文件/只读浏览器/低风险终端/生图等互不依赖的调用并行跑），
            # 其余保持串行；事件与结果回喂始终按模型给出的顺序。
            # 停止检查点：发起新一轮工具调用前再确认一次（避免批处理中途不可停）
            if stop_event is not None and stop_event.is_set():
                try:
                    self.tool_manager.cancel_active_tools()
                except Exception:
                    pass
                self._emit("run_loop_end", {"success": False, "stopped": True})
                return {
                    "success": False,
                    "output": "已按要求停止执行。",
                    "tool_calls": tool_calls_log,
                    "errors": errors,
                    "ops": turn + 1,
                    "stopped": True,
                }
            batch = list(response.tool_calls)
            # 先解包嵌套 _raw（模型/供应商把参数再包一层 JSON 字符串）：
            # 解开的还原为真正命名参数正常派发；解不开的（真截断/坏 JSON）
            # 保持 _raw，走下方"参数解析失败"拦截。
            for tc in batch:
                tc.arguments = unwrap_raw_arguments(tc.arguments)
            # 参数解析失败拦截：模型输出的 arguments JSON 不合法（典型是输出
            # 上限截断——finish_reason=length 时大文件写入参数被拦腰切断）。
            # 不下发给工具（会得到"未知操作"这类无引导错误让模型盲试重试），
            # 而是就地生成可行动的失败结果回喂给模型。
            bad = {
                id(tc): self._unparseable_error(tc, response)
                for tc in batch if "_raw" in tc.arguments
            }
            dispatch = [tc for tc in batch if id(tc) not in bad]
            for tc in batch:
                if event_sink is not None:
                    try:
                        event_sink("tool_call", {"tool": tc.name, "arguments": tc.arguments})
                    except Exception:
                        pass

            if dispatch and self._batch_parallelizable(dispatch):
                executed = self._run_tool_calls_parallel(dispatch, goal)
            else:
                # 串行执行；批处理中途响应停止：后续未执行的调用标记为取消，
                # 避免"停了但批里剩下的工具还在跑"
                executed = []
                for i, tc in enumerate(dispatch):
                    if stop_event is not None and stop_event.is_set() and i > 0:
                        for _ in dispatch[i:]:
                            executed.append((
                                ToolResult(success=False, output="",
                                           error="已按要求停止：该调用未执行。"),
                                "已按要求停止", 0.0,
                            ))
                        break
                    executed.append(self._execute_one_tool_call(tc, goal))
            # 把参数解析失败的调用按原顺序插回 executed，保证 zip 一一对应
            if bad:
                merged = []
                for tc in batch:
                    if id(tc) in bad:
                        merged.append((ToolResult(success=False, output="",
                                                  error=bad[id(tc)]), bad[id(tc)], 0.0))
                    else:
                        merged.append(executed.pop(0))
                executed = merged

            for tc, (result, blocked_reason, seconds) in zip(batch, executed):
                if self.metrics is not None:
                    self.metrics.tool_seconds += seconds
                    # think 是伪工具（无副作用），不计入"步数"，避免状态栏步数虚高
                    if tc.name != "think":
                        self.metrics.steps += 1
                    if result.success:
                        changed = self._changed_file(tc.name, tc.arguments)
                        if changed:
                            self.metrics.add_file_change(changed)
                idx = len(tool_calls_log)
                call_id = tc.id or f"call_{idx}"

                tool_calls_log.append({
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "success": result.success,
                    "output": (result.output or result.error)[:300],
                    "blocked_reason": blocked_reason,
                })
                if not result.success and not blocked_reason:
                    errors.append(result.error)
                    last_failure_idx = idx
                elif result.success:
                    last_success_idx = idx

                result_text = result.output
                if not result.success:
                    result_text = f"执行失败: {result.error}" if result.error else "执行失败（无错误信息）"
                if blocked_reason:
                    result_text = f"被拦截: {blocked_reason}"

                if event_sink is not None:
                    try:
                        # UI 事件输出走完整上限（与工具层 output_max_chars 一致）：
                        # 300 字符截断会让前端"概要 == 详情"（没有可展开的细节）
                        ui_cap = int(TOOL_CONFIG.get("output_max_chars", 8000))
                        event_sink("tool_result", {
                            "tool": tc.name,
                            "arguments": tc.arguments,
                            "success": result.success,
                            "output": (result.output or result.error or blocked_reason or "")[:ui_cap],
                            "metadata": getattr(result, "metadata", {}) or {},
                        })
                    except Exception:
                        pass

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_text,
                })

        # 达到最大操作轮数
        self._emit("run_loop_end", {"success": False, "output": ""})
        return {
            "success": False,
            "output": (
                f"已达到任务最大操作轮数（{max_ops}）。任务可能比预期复杂，"
                f"已完成的操作已记录：可用 --max-ops 提高上限后重试。"
            ),
            "tool_calls": tool_calls_log,
            "errors": errors,
            "ops": max_ops,
        }

    def _stream_turn(
        self,
        messages: list,
        tools: list,
        on_text_delta: Optional[Callable[[str, str], None]] = None,
        temperature: float = None,
        top_p: float = None,
        max_tokens: int = None,
    ):
        """
        流式执行一轮模型调用：增量回调 + 累积为 LLMToolResponse。
        """
        content_parts: list = []
        reasoning_parts: list = []
        reasoning_field = "reasoning_content"
        reasoning_present = False
        tool_map: dict = {}   # index → {"id", "name", "args"}
        finish_reason = ""

        for ev in self.llm.chat_with_tools_stream(
            messages, tools, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
        ):
            if ev.type == "text_delta":
                content_parts.append(ev.text)
                if on_text_delta is not None:
                    on_text_delta("text", ev.text)
            elif ev.type == "reasoning_delta":
                # 字段存在即标记（即使为空串），thinking 服务要求下一轮回传
                reasoning_present = True
                if ev.text:
                    reasoning_parts.append(ev.text)
                    if on_text_delta is not None:
                        on_text_delta("reasoning", ev.text)
                if ev.field:
                    reasoning_field = ev.field
            elif ev.type == "tool_delta":
                tc = tool_map.setdefault(ev.tool_index, {"id": "", "name": "", "args": ""})
                if ev.tool_name:
                    tc["name"] = ev.tool_name
                tc["args"] += ev.tool_args_delta
            elif ev.type == "done":
                finish_reason = ev.finish_reason

        tool_calls = []
        for idx in sorted(tool_map):
            tc = tool_map[idx]
            try:
                arguments = json.loads(tc["args"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": tc["args"]}
            if not isinstance(arguments, dict):
                arguments = {"input": str(arguments)}
            # 统一解包嵌套 _raw（模型/供应商把参数再包一层 JSON 字符串）：
            # 解不开的（真截断/坏 JSON）保持原样，走下方"参数解析失败"拦截
            arguments = unwrap_raw_arguments(arguments)
            tool_calls.append(ToolCall(
                id=tc["id"] or f"call_{idx}",
                name=tc["name"],
                arguments=arguments,
            ))

        return LLMToolResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning="".join(reasoning_parts),
            reasoning_field=reasoning_field,
            reasoning_present=reasoning_present,
        )

    def _unparseable_error(self, tc, response) -> str:
        """参数 JSON 解析失败时的可行动错误信息（不再让模型对着'未知操作'盲试）。"""
        raw = str(tc.arguments.get("_raw", ""))
        reason = getattr(response, "finish_reason", "") or ""
        if reason == "length":
            # 输出上限截断：写大文件/长文本时参数被拦腰切断
            return (
                f"参数不完整：模型输出被截断（finish_reason=length，写入内容过长）。"
                f"请把 '{tc.name}' 操作拆小：如需写大文件，先写入较短的部分、"
                f"再用多次 append 追加；一次调用的内容控制在 4K 字符以内。"
                f"（收到 {len(raw)} 字符的残缺参数）"
            )
        return (
            f"参数格式错误：工具 '{tc.name}' 的 arguments 不是合法 JSON，"
            f"无法解析（开头: {raw[:60]!r}）。请检查参数为合法 JSON 对象后重试。"
        )

    @staticmethod
    def _changed_file(tool_name: str, arguments: dict) -> str:
        """判断一次工具调用是否修改了文件，返回文件路径（未修改返回空串）。"""
        if tool_name == "edit":
            return str(arguments.get("file_path", "") or "")
        if tool_name == "file":
            if str(arguments.get("operation", "") or "").lower() == "write":
                return str(arguments.get("path", "") or "")
        return ""

    @staticmethod
    def _is_rate_limit(e: Exception) -> bool:
        """判断是否为限流/配额类错误（429 / tpm/rpm exhausted / quota）。"""
        s = str(e).lower()
        return "429" in s or "rate limit" in s or "tpm exhausted" in s \
            or "rpm exhausted" in s or "quota" in s or "限流" in s

    @staticmethod
    def _render_partial_progress(tool_calls_log: list, errors: list) -> str:
        """LLM 连续失败时，把已完成的工具成果渲染成部分进展说明。

        只统计真实的工具调用（排除 think 伪工具），并区分成功/失败/被拦截，
        避免"已完成 N 次"把 think、被拦截、被取消的调用也算进总次数里误导人。
        """
        ops = [c for c in tool_calls_log if c.get("name") != "think"]
        if not ops:
            return ""
        success = [c for c in ops if c.get("success")]
        failed = [c for c in ops if not c.get("success") and not c.get("blocked_reason")]
        blocked = [c for c in ops if c.get("blocked_reason")]
        sub = []
        if success:
            sub.append(f"成功 {len(success)} 次")
        if failed:
            sub.append(f"失败 {len(failed)} 次")
        if blocked:
            sub.append(f"被拦截 {len(blocked)} 次")
        head = f"任务在模型服务中断前已完成 {len(ops)} 次工具调用"
        lines = [(head + (f"（{'，'.join(sub)}）" if sub else "") + "。")]
        if success:
            lines.append("\n已完成的操作:")
            for c in success[-8:]:
                snippet = (c.get("output") or "")[:120].replace("\n", " ")
                lines.append(f"- {c.get('name')}: {snippet}")
        if errors:
            lines.append(f"\n遇到的错误: {'; '.join(errors[-3:])[:300]}")
        return "\n".join(lines)

    # ================================================================
    # 上下文压缩（借鉴同类实现的 compaction）
    # ================================================================

    @staticmethod
    def _estimate_tokens(messages: list) -> int:
        """粗略估算消息 token 数（CJK 混合：字符数 / 3）。"""
        total = 0
        for m in messages:
            total += len(str(m.get("content", "")) or "")
        return int(total / 3)

    def _compact_threshold(self) -> int:
        """压缩触发阈值：模块级 COMPACT_CONFIG.token_threshold 显式 >0 优先
        （含测试注入）；否则委托 LLM（按网关报告窗口 × 比例）；窗口不可知兜底。"""
        try:
            explicit = int(COMPACT_CONFIG.get("token_threshold") or 0)
            if explicit > 0:
                return explicit
            llm = getattr(self, "llm", None)
            if llm is not None and hasattr(llm, "compact_threshold_tokens"):
                return llm.compact_threshold_tokens()
        except Exception:
            pass
        return 240_000

    def _maybe_compact(self, messages: list) -> list:
        """上下文压缩：token 超阈值时，把旧历史交给 LLM 总结成摘要替换。

        保留最近 keep_last 条，更早的压缩成一段 system 摘要。
        失败安全：总结失败/未超阈值则原样返回，不破坏对话。
        """
        threshold = self._compact_threshold()
        keep = int(COMPACT_CONFIG.get("keep_last", 20))
        if len(messages) <= keep + 2 or self._estimate_tokens(messages) < threshold:
            return messages
        old = messages[:-keep]
        recent = messages[-keep:]
        try:
            summary = self._summarize_old(old)
        except Exception as e:
            self._emit("compaction", {"ok": False, "error": str(e)[:200]})
            return messages
        compacted = [{"role": "system", "content": summary}] + recent
        self._emit("compaction", {"ok": True, "summarized": len(old), "kept": len(recent)})
        return compacted

    def _summarize_old(self, old_messages: list) -> str:
        """把旧历史交给 LLM 压缩成摘要。"""
        text = "\n".join(
            f"[{m.get('role', 'user')}] {str(m.get('content', ''))[:500]}"
            for m in old_messages
        )
        prompt = (
            "你是对话历史压缩器。把下面这段 AI 助手与用户的对话历史压缩成一段简洁摘要，"
            "务必保留：已确认的事实、已做的决定、未完成的任务、关键约束、用户偏好、"
            "相关文件路径与已执行的操作结果。输出 200-500 字。\n\n"
            f"{text}"
        )
        resp = self.llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=800, temperature=0.2,
        )
        return (resp or "").strip() or "(历史已压缩)"

    def _dispatch_tool_call(self, tool_name: str, arguments: dict, goal: str,
                            checkpoint: bool = True):
        """
        执行一次工具调用（含审批 + Guardian 把关）。

        Args:
            checkpoint: 是否在修改成功后做 git checkpoint
                        （并行线程内关闭，防止 index 锁竞争与乱序提交）。

        Returns:
            (ToolResult, blocked_reason: str)  blocked_reason 非空表示被安全机制拦截
        """
        # 兜底解包：任何入口进来的参数都先还原嵌套 _raw（审批/Guardian/工具拿到的
        # 都必须是真正的命名参数，而不是 {"_raw": ...} 字符串）
        if isinstance(arguments, dict):
            arguments = unwrap_raw_arguments(arguments)

        # think 伪工具：无副作用，直接成功
        if tool_name == "think":
            thought = str(arguments.get("thought", ""))[:2000]
            self._emit("tool_call", {"tool": "think", "args": {"thought_len": len(thought)}})
            return ToolResult(success=True, output="思考已记录。"), ""

        # 1. 审批门
        if self.approval is not None:
            request = self.tool_manager.build_approval_request(tool_name, arguments)
            if request is not None:
                decision = self.approval.decide(request)
                self._emit("approval", {
                    "tool": tool_name,
                    "decision": "allow" if decision.allowed else "deny",
                    "reason": decision.reason,
                })
                if not decision.allowed:
                    return ToolResult(success=False, output="", error=decision.reason), decision.reason

        # 2. Guardian 审校
        if self.guardian is not None:
            request = self.tool_manager.build_approval_request(tool_name, arguments)
            if request is not None and self.guardian.should_review(request.risk_level):
                verdict = self.guardian.review(request, goal)
                self._emit("guardian", {"tool": tool_name, "verdict": verdict.verdict, "reason": verdict.reason})
                if verdict.verdict == "block":
                    return ToolResult(success=False, output="", error=f"Guardian 拦截: {verdict.reason}"), f"Guardian 拦截: {verdict.reason}"

        # 3. 执行（带硬超时：任何工具卡死都不冻结 Agent）
        self._emit("tool_call", {"tool": tool_name, "args": arguments})

        # 3.1 工具执行前钩子（fail-open：任何异常只警告，绝不阻断执行）
        try:
            if self._hooks is None:
                from agent.hooks import get_hook_manager
                self._hooks = get_hook_manager()
            self._hooks.on_pre_tool_use(tool_name, arguments)
        except Exception as e:
            logger.warning("pre 钩子调用异常（fail-open 跳过）: %s", e)

        timeout = TOOL_CONFIG.get("browser_timeout", 60) if tool_name == "browser" \
            else TOOL_CONFIG.get("tool_timeout", 300)

        # 实时输出流：支持增量回调的工具（terminal）执行期间把输出逐段转发 dashboard
        stream_tool = None
        if self._event_sink is not None and tool_name == "terminal":
            try:
                stream_tool = self.tool_manager.get_tool(tool_name)
            except Exception:
                stream_tool = None
        if stream_tool is not None and hasattr(stream_tool, "set_output_callback"):
            def _on_output(text: str, _sink=self._event_sink, _name=tool_name):
                try:
                    _sink("tool_output", {"tool": _name, "text": text})
                except Exception:
                    pass
            stream_tool.set_output_callback(_on_output)
        try:
            result = self._run_tool_with_timeout(
                lambda: self.tool_manager.execute_json(tool_name, arguments), timeout)
        finally:
            if stream_tool is not None and hasattr(stream_tool, "set_output_callback"):
                stream_tool.set_output_callback(None)

        if result is None:
            # 超时：重置该工具实例（丢弃卡死的 playwright 连接/子进程引用），
            # 让后续调用从干净状态重新开始，避免"一次卡死、次次卡死"。
            try:
                self.tool_manager.reset_tool(tool_name)
            except Exception:
                pass
            msg = (
                f"工具执行超时（>{timeout:.0f}s）：{tool_name} 无响应，"
                f"已重置该工具状态。请重试或改用其他方式。"
            )
            self._emit("tool_result", {
                "tool": tool_name,
                "success": False,
                "output": msg[:300],
                "truncated": False,
            })
            return ToolResult(success=False, output="", error=msg), msg

        # 逐操作式检查点：修改成功后立即 git 提交，
        # 每次操作都有独立提交（git revert HEAD 即可回滚上一步）；
        # 只提交本次修改的文件（Agent 归因提交），不卷入并行未提交改动
        if self.snapshot_checkpoints and checkpoint and result.success:
            changed = self._changed_file(tool_name, arguments)
            if changed:
                try:
                    from agent.snapshot import checkpoint
                    commit_hash = checkpoint(self.snapshot_dir, f"{tool_name} {changed}",
                                             changed_file=changed)
                    # commit hash 供前端「回滚到此处」按钮调用 /api/rollback。
                    # 注意必须走 event_sink（→hub→WebSocket）：self._emit 只进 rollout 日志
                    self._emit("checkpoint", {"tool": tool_name, "file": changed, "commit": commit_hash})
                    if self._event_sink is not None:
                        try:
                            self._event_sink("checkpoint", {"tool": tool_name, "file": changed, "commit": commit_hash})
                        except Exception:
                            pass
                except Exception:
                    pass

        self._emit("tool_result", {
            "tool": tool_name,
            "success": result.success,
            "output": (result.output or result.error)[:300],
            "truncated": result.truncated,
        })

        # 3.2 工具执行后钩子（fail-open：任何异常只警告，绝不阻断主流程）
        try:
            if self._hooks is None:
                from agent.hooks import get_hook_manager
                self._hooks = get_hook_manager()
            self._hooks.on_post_tool_use(tool_name, result)
        except Exception as e:
            logger.warning("post 钩子调用异常（fail-open 跳过）: %s", e)

        return result, ""

    # ================================================================
    # 并行工具调用（批量执行）
    # ================================================================

    def _batch_parallelizable(self, tcs: list) -> bool:
        """整批是否并发执行：≥2 个调用、全部并行安全、且审批无需交互。"""
        if len(tcs) < 2:
            return False
        if self.approval is not None:
            try:
                if self.approval.mode != "never" and getattr(self.approval, "interactive", True):
                    # 交互式审批（untrusted / on-request 等）不并发：避免多线程抢 input()
                    return False
            except Exception:
                return False
        for tc in tcs:
            if tc.name == "think":
                continue   # 无副作用伪工具，可并行
            if not self.tool_manager.is_parallel_safe(tc.name, tc.arguments):
                return False
        return True

    @staticmethod
    def _run_tool_with_timeout(fn, timeout: float):
        """
        在守护线程中执行工具调用，超时返回 None（线程继续在后台，进程退出不阻塞）。

        工具实现里的底层库（如 Playwright 同步 API）可能在 CDP 连接半死时
        永久挂起且无超时保护；这里做最后的硬性兜底，保证 Agent 主循环
        不会被单个卡死的工具冻结。
        """
        import threading

        box: dict = {}

        def _target():
            try:
                box["result"] = fn()
            except BaseException as e:   # noqa: BLE001 - 含 KeyboardInterrupt 等
                box["error"] = e

        t = threading.Thread(target=_target, daemon=True, name="tool-timeout")
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None   # 超时：线程仍在后台运行，调用方应重置工具实例
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _execute_one_tool_call(self, tc, goal: str, checkpoint: bool = True):
        """执行一次工具调用并计时；日志/指标/事件回喂由主线程统一按序处理。"""
        _t0 = time.time()
        result, blocked_reason = self._dispatch_tool_call(
            tc.name, tc.arguments, goal, checkpoint=checkpoint)
        return result, blocked_reason, time.time() - _t0

    def _run_tool_calls_parallel(self, tcs: list, goal: str):
        """
        并发执行一批工具调用，返回与输入同序的结果列表。

        用 daemon 线程并行（不用 ThreadPoolExecutor：其 with 退出会等待
        卡死的 worker，导致 Agent 冻结）；每个工具调用内部已有
        _run_tool_with_timeout 硬超时兜底，join 最多等待超时上限。
        """
        import threading

        results: dict = {}

        def _run(idx, tc):
            try:
                results[idx] = self._execute_one_tool_call(tc, goal, False)
            except BaseException as e:   # noqa: BLE001
                results[idx] = (ToolResult(success=False, output="", error=str(e)), str(e), 0.0)

        threads = []
        for i, tc in enumerate(tcs):
            t = threading.Thread(target=_run, args=(i, tc), daemon=True,
                                 name=f"tool-parallel-{i}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return [results[i] for i in range(len(tcs))]

    def _emit(self, event_type: str, data: dict):
        if self.rollout is not None:
            self.rollout.emit(event_type, data)

    # ================================================================
    # 旧协议回退（legacy：文本 JSON 决策）
    # ================================================================

    def execute_step_legacy(
        self,
        goal: str,
        current_step: str,
        history_summary: str,
        step_context: str = "",
        page_screenshot_base64: str = "",
        vision_feedback: str = "",
        failure_warnings: str = "",
    ) -> dict:
        """旧协议：LLM 输出 JSON 决策（保留兼容不支持 tools 的供应商）。"""
        decision = self._decide_action(
            goal=goal,
            current_step=current_step,
            history_summary=history_summary,
            step_context=step_context,
            vision_feedback=vision_feedback,
            failure_warnings=failure_warnings,
        )

        action = decision.get("action", "think")
        result = {
            "step": current_step,
            "action": action,
            "reasoning": decision.get("reasoning", ""),
        }

        if action == "use_tool":
            tool_name = decision.get("tool", "")
            tool_input = decision.get("tool_input", "")

            if tool_name == "browser" and tool_input == "screenshot_base64":
                screenshot_result = self._take_screenshot_for_vision()
                if screenshot_result:
                    result.update(screenshot_result)
                    return result

            tool_result = self.tool_manager.execute(tool_name, tool_input)
            result["tool"] = tool_name
            result["tool_input"] = tool_input
            result["success"] = tool_result.success
            result["output"] = tool_result.output
            result["error"] = tool_result.error
            result["status"] = "completed" if tool_result.success else "failed"
            result["tools_used"] = [tool_name] if tool_name else []

        elif action == "see":
            vision_result = self._perform_vision_analysis(
                goal=goal,
                current_step=current_step,
                step_context=step_context,
            )
            result.update(vision_result)

        elif action == "continue":
            result["output"] = decision.get("reasoning", "继续执行当前步骤。")
            result["success"] = True
            result["status"] = "continue"

        elif action == "think":
            result["output"] = decision.get("reasoning", current_step)
            result["success"] = True
            result["status"] = "completed"

        elif action == "finish":
            result["output"] = decision.get("result", "任务完成。")
            result["success"] = True
            result["status"] = "completed"

        else:
            result["output"] = f"未知操作: {action}"
            result["success"] = False
            result["status"] = "failed"

        return result

    def execute_tool_directly(self, tool_name: str, tool_input: str) -> dict:
        """直接调用工具（绕过 LLM）。"""
        tool_result = self.tool_manager.execute(tool_name, tool_input)
        return {
            "step": f"自动操作: {tool_name} {tool_input}",
            "action": "use_tool",
            "tool": tool_name,
            "tool_input": tool_input,
            "success": tool_result.success,
            "output": tool_result.output,
            "error": tool_result.error,
            "status": "completed" if tool_result.success else "failed",
            "reasoning": "自动执行",
        }

    # ================================================================
    # 视觉分析
    # ================================================================

    def _take_screenshot_for_vision(self) -> Optional[dict]:
        """执行浏览器截图并提取 base64。"""
        browser = self.tool_manager.get_tool("browser")
        if browser is None:
            return None
        try:
            result = browser.execute("screenshot_base64")
            if not result.success:
                return {
                    "step": "截图失败",
                    "action": "see",
                    "success": False,
                    "output": "",
                    "status": "failed",
                    "error": result.error,
                    "screenshot_base64": "",
                    "vision_analysis": "",
                }
            # 提取 base64
            output = result.output
            match = re.search(r'\[FULL_BASE64\](.*?)\[/FULL_BASE64\]', output, re.DOTALL)
            if match:
                return {
                    "step": "页面截图",
                    "action": "see",
                    "success": True,
                    "output": output.split("[FULL_BASE64]")[0].strip(),
                    "status": "completed",
                    "screenshot_base64": match.group(1),
                    "vision_analysis": "",
                }
        except Exception:
            pass
        return None

    def _perform_vision_analysis(
        self,
        goal: str,
        current_step: str,
        step_context: str = "",
    ) -> dict:
        """
        执行视觉分析：截图 → 视觉模型分析 → 返回结果。
        """
        if self.vision_model is None:
            return {
                "step": current_step,
                "action": "see",
                "success": False,
                "output": "",
                "status": "failed",
                "error": "视觉模型不可用。请确保配置了支持多模态的 LLM（如 GPT-4o）。",
                "screenshot_base64": "",
                "vision_analysis": "",
                "reasoning": "视觉模型不可用",
            }

        # 1. 截图
        screenshot_result = self._take_screenshot_for_vision()
        if screenshot_result is None or not screenshot_result.get("success"):
            return screenshot_result or {
                "step": current_step,
                "action": "see",
                "success": False,
                "output": "",
                "status": "failed",
                "error": "无法截图。浏览器可能未启动。",
                "screenshot_base64": "",
                "vision_analysis": "",
            }

        # 2. 视觉分析
        base64_data = screenshot_result.get("screenshot_base64", "")
        try:
            if step_context:
                question = VISION_STEP_RESULT_QUESTION.format(
                    expected_result=current_step,
                    last_action=step_context[-200:],
                )
            else:
                question = VISION_PAGE_ANALYSIS_QUESTION

            analysis = self.vision_model.analyze(base64_data, question, max_tokens=1500)

            screenshot_result["vision_analysis"] = analysis
            screenshot_result["output"] = (
                f"【视觉分析结果】\n{analysis}\n\n"
                f"请基于以上分析，决定下一步操作。"
            )
            screenshot_result["status"] = "continue"  # 视觉分析后需要继续决策
            return screenshot_result

        except Exception as e:
            screenshot_result["status"] = "failed"
            screenshot_result["error"] = f"视觉分析失败: {str(e)}"
            return screenshot_result

    def analyze_page_visually(self, goal: str = "") -> str:
        """
        对当前页面执行视觉分析。

        Returns:
            视觉分析结果文本，失败返回空字符串。
        """
        if self.vision_model is None:
            return ""
        result = self._perform_vision_analysis(goal=goal, current_step="查看页面内容")
        return result.get("vision_analysis", "")

    # ================================================================
    # 帧对比与异常检测（第四阶段新增）
    # ================================================================

    def compare_before_after(
        self,
        before_base64: str,
        after_base64: str,
        expected_action: str = "",
    ) -> str:
        """
        对比操作前后截图，验证操作效果。
        """
        if self.vision_model is None:
            return "视觉模型不可用，无法对比。"
        try:
            from models.video_analyzer import VideoFrameAnalyzer
            analyzer = VideoFrameAnalyzer(self.vision_model)
            return analyzer.compare_frames(before_base64, after_base64, expected_action)
        except Exception as e:
            return f"帧对比失败: {str(e)}"

    def detect_page_anomaly(self, screenshot_base64: str, context: str = "") -> dict:
        """
        检测页面异常（弹窗、错误、加载失败等）。

        Returns:
            {"has_anomaly": bool, "type": str, "description": str}
        """
        if self.vision_model is None:
            return {"has_anomaly": False, "type": "", "description": "视觉模型不可用"}
        try:
            from models.video_analyzer import VideoFrameAnalyzer
            analyzer = VideoFrameAnalyzer(self.vision_model)
            return analyzer.detect_anomaly(screenshot_base64, context)
        except Exception as e:
            return {"has_anomaly": False, "type": "", "description": str(e)}

    def validate_progress_visually(self, screenshot_base64: str, expected_state: str) -> str:
        """验证当前页面是否符合预期进度。"""
        if self.vision_model is None:
            return "视觉模型不可用。"
        try:
            from models.video_analyzer import VideoFrameAnalyzer
            analyzer = VideoFrameAnalyzer(self.vision_model)
            return analyzer.validate_progress(screenshot_base64, expected_state)
        except Exception as e:
            return f"进度验证失败: {str(e)}"

    # ================================================================
    # LLM 决策（旧协议）
    # ================================================================

    def _decide_action(
        self,
        goal: str,
        current_step: str,
        history_summary: str,
        step_context: str = "",
        vision_feedback: str = "",
        failure_warnings: str = "",
    ) -> dict:
        """调用 LLM 决定下一步操作（旧文本 JSON 协议）。"""
        user_prompt = EXECUTOR_USER_PROMPT_TEMPLATE.format(
            goal=goal,
            current_step=current_step,
            history=history_summary,
            tools_description=self.tool_manager.get_tools_description(),
        )

        if step_context:
            user_prompt += f"\n\n当前步骤内已执行的操作:\n{step_context}\n"

        if vision_feedback:
            user_prompt += f"\n\n【视觉分析结果】\n{vision_feedback}\n请基于以上分析决定下一步。\n"

        # 注入失败模式警告（自我进化）
        if failure_warnings:
            user_prompt += f"\n\n【警告：已知失败模式】\n{failure_warnings}\n请避开以上错误模式。\n"

        is_browser = self._is_browser_task(goal, current_step)
        if is_browser and self.vision_model is not None:
            system_prompt = EXECUTOR_VISION_BROWSER_PROMPT
        elif is_browser:
            system_prompt = EXECUTOR_BROWSER_SYSTEM_PROMPT
        else:
            system_prompt = EXECUTOR_SYSTEM_PROMPT

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = self.llm.chat(messages)
        return self._parse_decision(response)

    def _is_browser_task(self, goal: str, current_step: str) -> bool:
        """智能判断是否为浏览器任务。

        两级过滤：
        1. 关键词快速匹配（必须命中关键词才进入第二步）
        2. 负面模式过滤（排除抱怨、提问、反问等非任务场景）
        """
        combined = (goal + " " + current_step).lower()
        if not any(kw.lower() in combined for kw in self.BROWSER_KEYWORDS):
            return False

        # 负面模式：用户只是在讨论/抱怨浏览器，而非请求实际操作
        negative_patterns = [
            r"为什么.*浏览器", r"干嘛.*浏览器", r"怎么.*浏览器",
            r"啥.*浏览器", r"什么.*浏览器", r"咋.*浏览器",
            r"别开.*浏览器", r"不要.*浏览器", r"关了.*浏览器",
            r"不.*用.*浏览器", r"不需要.*浏览器", r"不用.*打开.*浏览",
            r"不用.*启动.*浏览", r"浏览器.*干嘛", r"浏览器.*干吗",
            r"打开.*浏览器.*干嘛", r"浏览器.*什么", r"刚才.*浏览器",
        ]
        for pattern in negative_patterns:
            if re.search(pattern, goal):
                return False

        return True

    @staticmethod
    def _parse_decision(raw_response: str) -> dict:
        json_match = re.search(r'\{[\s\S]*\}', raw_response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        return {"action": "think", "reasoning": raw_response.strip()}
