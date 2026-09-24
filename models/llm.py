"""
LLM 模块（v2：原生 function calling + JSON 模式 + 自动重试 + 流式输出）。
封装兼容协议调用。
换模型只需改 .env 文件，这个文件不用动。

新增能力（借鉴开源 agent 框架的工具协议）：
- chat_with_tools(): 原生 function calling，返回结构化工具调用
- chat(): 保留旧文本接口（Planner / 总结 / Team 等仍在使用）
- json 模式: 通过 json_mode=True 强制模型输出 JSON（部分供应商支持）
- chat_with_tools_stream(): 流式 function calling（逐字渲染）
- 自动重试: 对限流(429)/服务端错误/连接错误指数退避重试
  （OpenRouter 的 stealth 模型上游限流频繁，此能力至关重要）
"""
import re
import time
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

# 延迟导入 SDK（关键性能优化）：
# SDK 的 __init__ 会级联导入大量类型定义（types.beta / graders / eval
# 等），实测耗时 ~1.7s，占 CLI 启动总耗时的 84%。改为首次真正需要时再导入
# （创建客户端 / 捕获重试异常），CLI 启动从 2.2s 降到 ~0.5s。
if TYPE_CHECKING:   # 仅类型检查期提供名字，运行时不导入
    from openai import OpenAI

from config import LLM_CONFIG


def _openai_errors() -> tuple:
    """可重试的 SDK 异常类型（惰性解析，避免启动时导入整个 SDK）。"""
    from openai import (
        RateLimitError,
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
    )
    return (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)


def _openai_client_class():
    """LLM 客户端类（惰性导入）。"""
    from openai import OpenAI as _OpenAI
    return _OpenAI


def _bad_request_error():
    """BadRequestError 类（惰性导入，用于错误体解析分支）。"""
    from openai import BadRequestError as _BRE
    return _BRE


def unwrap_raw_arguments(arguments: dict, max_depth: int = 5) -> dict:
    """工具参数 `_raw` 解包（递归，有界）。

    背景：部分模型/服务会把 arguments 再包一层 JSON 字符串
    （{"_raw": "{\\"code\\": ...}"}，甚至更深层嵌套），如果不解包，
    工具只能看到 {"_raw": ...} 而拿不到真正的命名参数；而拦截逻辑
    只认"解析失败产生的 _raw"，合法的嵌套 _raw 会漏过（loop 模式
    误报坏参数、FC 回退路径直接原样派发）。

    规则：当且仅当 arguments 是 dict 且含 `_raw` 键时，尝试把该字符串
    json.loads 解开并继续下一层；解不开（坏 JSON/截断/空）则原样返回，
    由调用方按既有"解析失败"路径处理。非 dict 值包成 {"input": ...}。
    """
    depth = 0
    while depth < max_depth and isinstance(arguments, dict) and "_raw" in arguments:
        raw = arguments.get("_raw")
        if not isinstance(raw, str) or not raw.strip():
            break
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            break
        if isinstance(parsed, dict):
            arguments = parsed
        elif parsed is None:
            arguments = {}
        else:
            arguments = {"input": parsed}
        depth += 1
    return arguments

# 可重试的异常类型（指数退避）——改为惰性解析（见 _openai_errors()），
# 避免模块导入期加载整个 SDK（约 1.7s）。保留名字供旧代码/文档引用。
RETRYABLE_ERRORS = ()


def _first_choice_message(response):
    """取第一个 choice 及其 message；空 choices / 缺 message 时给一句能看懂的报错。

    网关把上游错误包成 **HTTP 200 + `{"choices": []}`** 是常见形态（内容被安全策略
    拦截时也这样）。直接 `response.choices[0].message` 会抛
    `IndexError: list index out of range` 或 `AttributeError: 'NoneType'`，
    调用方（文章流水线、主循环）只能拿到一个与模型无关的晦涩异常，排查方向被带偏。
    `models/vision.py` 对同一问题专门加了保护，主 LLM 这两条非流式路径反而没有
    （2026-09-22 审计）。
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError(
            "上游返回了空的 choices（HTTP 200 但无候选）。常见原因：内容被安全策略"
            "拦截，或网关把上游错误包成了 200。请调整输入后重试。")
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise RuntimeError("上游返回的 choice 缺少 message 字段（响应不完整）。")
    return choice, message


def _is_minute_quota_error(e: Exception) -> bool:
    """是否为**按分钟窗口**重置的配额耗尽（TPM/RPM）。

    这类错误重试必须跨过分钟边界才有意义；否则退避几秒后仍在同一窗口内
    （实测日志：6s/12s 退避的两次重试全部撞在同一分钟窗口内失败）。
    """
    msg = str(e).lower()
    return any(k in msg for k in (
        "tpm", "rpm", "tokens per minute", "requests per minute",
        "per minute", "rate limit exceeded", "quota_exceeded",
    ))


def _is_quota_error(e: Exception) -> bool:
    """是否为配额/限流类错误（429）。

    有些网关把限流包装成 invalid_request_error（如 "inference tpm exhausted"，
    type=invalid_request_error），不会抛 RateLimitError——按错误文本识别，
    避免这类 429 变体完全跳过重试直接失败。
    """
    msg = str(e).lower()
    return (
        "429" in msg
        or "rate limit" in msg
        or "too many requests" in msg
        or "quota" in msg
        or "exhausted" in msg
    )


@dataclass
class StreamEvent:
    """
    流式响应事件（一次增量）。

    type 取值:
      text_delta        - 正文增量（最终回答文本）
      reasoning_delta   - 推理增量（thinking 模式的思考过程）
      tool_delta        - 工具调用增量（name / arguments 分片到达）
      done              - 本轮流式结束（携带 finish_reason）
    """
    type: str
    text: str = ""
    finish_reason: str = ""
    tool_index: int = 0
    tool_name: str = ""
    tool_args_delta: str = ""
    field: str = ""     # reasoning_delta 的来源字段名（reasoning / reasoning_content）


@dataclass
class ToolCall:
    """一次结构化工具调用。"""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMToolResponse:
    """带工具调用的 LLM 响应。"""

    content: str = ""                  # 文本内容（可能为空）
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    reasoning: str = ""                # 思考模式推理内容（需在下一轮回传）
    reasoning_field: str = "reasoning_content"  # 推理字段名（reasoning_content / reasoning）
    reasoning_present: bool = False    # 响应中是否存在推理字段（即使为空串）

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLM:
    """LLM 客户端（兼容 chat/completions 协议）。"""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        """
        Args:
            api_key: 覆盖 LLM_CONFIG 的密钥（如视觉/Guardian 专用端点）
            base_url: 覆盖 LLM_CONFIG 的 API 地址
            model: 覆盖默认模型（临时切换，不写入 .env）
        """
        # 超时保护：上游挂起时抛 ReadTimeout → 执行器轮级重试，而不是无限转圈
        self.timeout = float(LLM_CONFIG.get("timeout", 300))
        self.client = _openai_client_class()(
            api_key=api_key or LLM_CONFIG["api_key"],
            base_url=base_url or LLM_CONFIG["base_url"],
            timeout=self.timeout,
            max_retries=0,   # 重试由本模块的指数退避统一管理
        )

        # 保存默认值，chat() 方法里会用
        self.default_model = model or LLM_CONFIG["default_model"]
        self.default_temperature = LLM_CONFIG["default_temperature"]
        self.default_max_output_tokens = LLM_CONFIG["default_max_output_tokens"]

        # 网关模型元数据缓存：{(base_url, model) -> context_window | None}
        self._window_cache: dict = {}
        # 固定温度：某些 thinking 模型只接受特定值（如必须为 1），
        # 设置后忽略所有传入 temperature（否则 400 invalid_request_error）
        self.fixed_temperature = LLM_CONFIG.get("fixed_temperature")
        # 该模型已知不支持的请求参数（400 后自动记录，后续请求自动移除/转换）
        self.param_blacklist: set[str] = set()

        # 自动重试配置（OpenRouter 等供应商限流时指数退避）
        self.max_retries = int(LLM_CONFIG.get("max_retries", 2))
        #: 限流等待回调（可选）：Agent 注入 cb(seconds, attempt)，用于把"正在等配额"
        #: 显示给用户并记进 rollout。见 _notify_rate_limit。
        self.retry_notifier = None
        self.retry_base_delay = float(LLM_CONFIG.get("retry_base_delay", 2.0))

        # 运行时统计（由 Agent 注入 RunMetrics，可留空）
        self.metrics = None

    def _model_fixed_temperature(self) -> float:
        """按模型名自动适配固定温度（内置规则；.env 显式设置优先）。

        已知只接受 temperature=1 的模型：
        - 部分 thinking 模型硬性要求 temperature=1
        """
        name = (self.default_model or "").lower()
        if name.startswith("kimi-k3"):
            return 1.0
        return self.fixed_temperature if self.fixed_temperature is not None else None

    # ================================================================
    # 统计记录（状态行数据源）
    # ================================================================

    def _record_usage(self, usage, elapsed: float, first_token: float = None):
        """把一次调用的 usage 记入 RunMetrics。"""
        if self.metrics is None:
            return
        input_tokens = output_tokens = cached_tokens = 0
        if usage is not None:
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached_tokens = getattr(details, "cached_tokens", 0) or 0
        self.metrics.add_llm_call(
            elapsed=elapsed,
            first_token=first_token,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )

    # ================================================================
    # 自动重试
    # ================================================================

    def _resolve_temperature(self, temperature: float = None) -> float:
        """解析请求温度：固定温度优先（thinking 模型只接受特定值）。

        某些 thinking 模型硬性要求 temperature=1，传入其他值会
        400 "only 1 is allowed for this model"；自动按模型名适配 +
        支持 LLM_FIXED_TEMPERATURE 环境变量显式覆盖。
        """
        fixed = self._model_fixed_temperature()
        if fixed is not None:
            return fixed
        return temperature if temperature is not None else self.default_temperature

    def _extract_bad_param(self, e: Exception) -> Optional[str]:
        """从 400 错误中提取不受支持的参数名（None 表示无法识别）。

        优先读取供应商返回的 param 字段（标准错误体结构），
        否则用正则从错误信息文本兜底匹配。
        """
        message = str(e)
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                param = err.get("param")
                if isinstance(param, str) and param:
                    return param
                if err.get("message"):
                    message = str(err["message"])
            elif body.get("message"):
                message = str(body["message"])

        patterns = [
            # temperature 特例：错误文本里没有参数名，直接映射
            r"only\s+1\s+is\s+allowed\s+for\s+this\s+model",
            r"unexpected\s+field\s+[\"']?([a-z_][a-z0-9_]*)[\"']?",
            r"[\"']?([a-z_][a-z0-9_]*)[\"']?\s+is\s+not\s+supported",
            r"does\s+not\s+support\s+(?:param(?:eter)?\s+)?[\"']?([a-z_][a-z0-9_]*)",
            r"invalid\s+(?:request\s+)?param(?:eter)?\s*[\"':=]+\s*[\"']?([a-z_][a-z0-9_]*)",
            r"unknown\s+(?:parameter|param)\s+[\"']?([a-z_][a-z0-9_]*)",
        ]
        for pat in patterns:
            m = re.search(pat, message, re.IGNORECASE)
            if m:
                if pat.startswith(r"only\s+1"):
                    return "temperature"
                return m.group(1)
        return None

    def fetch_context_window(self, model: str = None) -> Optional[int]:
        """从网关 /models 查当前模型的 context_length（真实窗口）。

        查询失败/模型未列出返回 None（调用方走兜底）；结果按 (base, model) 缓存。
        """
        model = model or self.default_model
        key = (str(self.client.base_url), model)
        if key in self._window_cache:
            return self._window_cache[key]
        window = None
        try:
            import json as _json
            import urllib.request as _urlreq
            req = _urlreq.Request(
                str(self.client.base_url).rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {self.client.api_key}"},
            )
            with _urlreq.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            for m in data.get("data", []) or []:
                if m.get("id") == model and m.get("context_length"):
                    window = int(m["context_length"])
                    break
        except Exception:
            window = None
        self._window_cache[key] = window
        return window

    def compact_threshold_tokens(self, model: str = None) -> int:
        """压缩触发阈值：COMPACT_TOKEN_THRESHOLD 显式 >0 优先；
        否则按网关报告的模型窗口 × COMPACT_WINDOW_RATIO（默认 0.75）；
        窗口查不到时回退 240_000（1M 窗口约 24% 的保守点）。"""
        try:
            from config import COMPACT_CONFIG
            explicit = int(COMPACT_CONFIG.get("token_threshold") or 0)
            if explicit > 0:
                return explicit
            ratio = float(COMPACT_CONFIG.get("window_ratio") or 0.75)
            window = self.fetch_context_window(model or self.default_model)
            if window and window > 0:
                return max(10_000, int(window * ratio))
        except Exception:
            pass
        return 240_000

    def _sanitize_kwargs(self, kwargs: dict) -> None:
        """按已知参数限制清洗请求（400 后自动记录，此后每次请求生效）。

        - 黑名单参数直接移除；
        - max_tokens 被禁时自动转为 max_completion_tokens
          （o1 / gpt-5 系列等只认后者的模型）。
        """
        for name in list(kwargs):
            if name not in self.param_blacklist:
                continue
            if name == "max_tokens" and "max_completion_tokens" not in kwargs:
                kwargs["max_completion_tokens"] = kwargs[name]
            kwargs.pop(name, None)

    def _create_with_retry(self, kwargs: dict):
        """
        带指数退避重试的 completions.create。

        对限流(429)/服务端错误/网络错误按 2s → 4s → ... 重试，
        其他错误（如参数错误）直接抛出。

        400 参数自动降级：若服务端拒绝某个请求参数，自动把该参数
        记入本实例的 param_blacklist 并重试（保留参数名解析 + 移除/
        转换逻辑），一次 400 之后同一实例后续请求都会跳过该参数。
        特例：temperature "only 1 is allowed"（thinking 模型硬性要求
        temperature=1）直接把本实例温度固定为 1，不再依赖内置名单。
        """
        self._sanitize_kwargs(kwargs)  # 发送前按已知限制清洗
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.client.chat.completions.create(**kwargs)
            except _bad_request_error() as e:
                if _is_quota_error(e):
                    # 429 变体（包装成 invalid_request_error 的配额错误）：
                    # 提取不出参数名，直接 raise 会导致零重试——按限流处理
                    last_error = e
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(self._quota_wait(e, attempt))
                    continue
                last_error = e
                param = self._extract_bad_param(e)
                # temperature 特例：thinking 模型硬性要求 1（错误文本可能带
                # "temperature" 字样也可能不带，统一按 param 识别）
                if param == "temperature" and "only 1 is allowed" in str(e).lower() and kwargs.get("temperature") != 1:
                    self.fixed_temperature = 1.0  # 记住该模型限制，后续请求直接生效
                    kwargs["temperature"] = 1
                    continue
                if param:
                    self.param_blacklist.add(param)
                    self._sanitize_kwargs(kwargs)
                    continue
                raise
            except _openai_errors() as e:
                last_error = e
                if attempt >= self.max_retries:
                    raise
                if _is_quota_error(e):
                    time.sleep(self._quota_wait(e, attempt))
                else:
                    time.sleep(self._retry_delay(attempt))
        raise last_error

    def _retry_delay(self, attempt: int, quota: bool = False) -> float:
        """退避延迟：限流类错误起点更长（配额按分钟窗口，2s 起步太密）。"""
        base = self.retry_base_delay * (3 if quota else 1)
        return base * (2 ** attempt)

    def _quota_wait(self, err, attempt: int) -> float:
        """429 限流等待：优先读 retry-after；无则退避延迟；单次上限 65s。

        TPM/RPM 等**分钟窗口**配额：供应商不给 retry-after 时，单纯指数退避
        （6s/12s）会一直在同一分钟窗口内硬怼——实测三次重试全失败、任务中断。
        因此对齐到**下一个分钟边界**再试，保证跨过配额重置点。
        """
        delay = self._retry_delay(attempt, quota=True)
        try:
            headers = getattr(getattr(err, "response", None), "headers", None) or {}
            ra = headers.get("retry-after") or headers.get("Retry-After")
            if ra:
                val = float(str(ra))
                if val > 0:
                    # 供应商明确告知了等待时长：以它为准（上限 65s）
                    return max(delay, min(val, 65.0))
        except Exception:
            pass
        if _is_minute_quota_error(err):
            # 无 retry-after 的分钟级配额：等到下一个分钟边界（+1s 余量）
            now = time.time()
            to_next_minute = 60.0 - (now % 60.0) + 1.0
            delay = max(delay, min(to_next_minute, 65.0))
        # 长等待（分钟级窗口）给出可见提示：否则 CLI 静默等 1 分钟，
        # 看起来像卡死。走 stderr，不干扰富文本 stdout 渲染。
        if delay >= 5:
            try:
                import sys as _sys
                _sys.stderr.write(
                    "\n[限流] 上游配额已满，等待 %.0f 秒后重试（第 %d 次）…\n"
                    % (delay, attempt + 1)
                )
                _sys.stderr.flush()
            except Exception:
                pass
        self._notify_rate_limit(delay, attempt)
        return delay

    def _notify_rate_limit(self, seconds: float, attempt: int) -> None:
        """把"正在等配额"这件事通知给上层（可选回调）。

        为什么需要：上面的 stderr 提示只在纯 CLI 里看得见——桌面端 / 内嵌 UI /
        dashboard 渲染的是富文本 stdout，stderr 里的这行没人看得到；而且**等待时长
        没有被记录进 rollout**，事后复盘只知道"发生过 429"，不知道一共等了 10 秒
        还是 2 分钟（实测分析一次 run 时就卡在这里）。

        models 层不 import UI：只调一个由 Agent 注入的回调，没有回调就什么都不做。
        回调抛异常绝不影响重试本身。
        """
        cb = getattr(self, "retry_notifier", None)
        if cb is None:
            return
        try:
            cb(seconds, attempt)
        except Exception:      # noqa: BLE001 — 提示失败不能影响重试
            pass

    def chat(
        self,
        messages: list[dict],
        model: str = None,
        temperature: float = None,
        max_tokens: int = None,
        top_p: float = None,
        json_mode: bool = False,
    ) -> str:
        """
        发送对话消息，返回文本回复（旧接口，保持兼容）。

        Args:
            messages: 对话历史 [{"role": ..., "content": ...}]
            model: 模型名（不传用默认）
            temperature: 随机度（不传用默认）
            max_tokens: 最大输出 token（不传用默认）
            top_p: 核采样（不传则不显式设置）
            json_mode: 是否启用 JSON 输出模式（部分供应商支持 response_format）

        Returns:
            回复文本。
        """
        kwargs = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": self._resolve_temperature(temperature),
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_output_tokens,
        }
        if top_p is not None:
            kwargs["top_p"] = top_p
        if json_mode:
            try:
                kwargs["response_format"] = {"type": "json_object"}
            except Exception:
                pass

        self._sanitize_kwargs(kwargs)  # 按已知参数限制清洗（400 自动降级）

        t0 = time.time()
        response = self._create_with_retry(kwargs)
        self._record_usage(getattr(response, "usage", None), time.time() - t0)
        _choice, message = _first_choice_message(response)
        content = message.content
        return content if content is not None else ""

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: List[dict],
        model: str = None,
        temperature: float = None,
        max_tokens: int = None,
        top_p: float = None,
        tool_choice: str = "auto",
    ) -> LLMToolResponse:
        """
        原生 function calling 调用（v2 核心接口）。

        Args:
            messages: 对话历史
            tools: 标准格式的工具列表（BaseTool.to_openai_schema() 的结果）
            model / temperature / max_tokens / top_p: 同 chat()
            tool_choice: auto / required / none / {"type":"function","function":{"name":...}}

        Returns:
            LLMToolResponse（含文本与结构化工具调用）
        """
        t0 = time.time()
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": self._resolve_temperature(temperature),
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_output_tokens,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        self._sanitize_kwargs(payload)  # 按已知参数限制清洗（400 自动降级）
        response = self._create_with_retry(payload)
        self._record_usage(getattr(response, "usage", None), time.time() - t0)

        choice, message = _first_choice_message(response)

        # 思考模式：捕获推理内容（需在下一轮回传，否则服务器 400）
        # 关键：字段存在（即使空串）也要标记，thinking 服务要求每条 assistant 消息回传该字段
        reasoning = ""
        reasoning_field = "reasoning_content"
        reasoning_present = False
        rc = getattr(message, "reasoning_content", None)
        r2 = getattr(message, "reasoning", None)
        if rc is not None or r2 is not None:
            reasoning_present = True
            reasoning = rc if rc is not None else r2
            if rc is None:
                reasoning_field = "reasoning"
            reasoning = reasoning or ""

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": tc.function.arguments or ""}
                if not isinstance(arguments, dict):
                    arguments = {"input": str(arguments)}
                # 统一解包：合法的嵌套 _raw（模型把参数再包一层 JSON 字符串）也能
                # 还原出真正的命名参数；解析失败产生的 _raw 解不开、保持原样，
                # 由执行器按"参数解析失败"拦截。
                arguments = unwrap_raw_arguments(arguments)
                tool_calls.append(ToolCall(
                    id=tc.id or "",
                    name=tc.function.name,
                    arguments=arguments,
                ))

        return LLMToolResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "",
            reasoning=reasoning or "",
            reasoning_field=reasoning_field,
            reasoning_present=reasoning_present,
        )

    def chat_with_tools_stream(
        self,
        messages: list[dict],
        tools: List[dict],
        model: str = None,
        temperature: float = None,
        max_tokens: int = None,
        top_p: float = None,
    ) -> Iterator[StreamEvent]:
        """
        流式 function calling：逐 token 产出增量事件。

        产出 StreamEvent 序列：
        text_delta / reasoning_delta / tool_delta ... done

        注意：流式迭代过程中的网络错误无法重试（流已开始），
        仅在建立连接阶段做重试。
        """
        kwargs = {
            "model": model or self.default_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self._resolve_temperature(temperature),
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},   # 让供应商在最后一块返回 usage
        }
        if top_p is not None:
            kwargs["top_p"] = top_p
        self._sanitize_kwargs(kwargs)  # 按已知参数限制清洗（400 自动降级）

        # 建立流：连接阶段重试；不支持 stream_options 的供应商自动降级
        stream = None
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                # 计时必须从**发起请求前**开始。此前 t0 取在 create() 返回之后，
                # 而部分网关（实测）即使 stream=True 也会先把整段回复缓冲好、
                # 等生成结束才返回流对象：于是 elapsed 只量到本地排空缓冲的 0.1s、
                # 首 token 记成 0.02s，状态行的「LLM 耗时 / 首 token」双双失真。
                # 放在循环内 → 连接重试的退避等待不计入（那是空等，不是模型耗时）。
                t0 = time.time()
                stream = self.client.chat.completions.create(**kwargs)
                break
            except _openai_errors() as e:
                last_error = e
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._retry_delay(attempt, quota=_is_quota_error(e)))
            except _bad_request_error() as e:
                last_error = e
                if _is_quota_error(e):
                    # 429 变体（包装成 invalid_request_error）：按限流重试
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(self._retry_delay(attempt, quota=True))
                    continue
                if "stream_options" in str(e) and "stream_options" in kwargs:
                    kwargs.pop("stream_options", None)
                    continue
                param = self._extract_bad_param(e)
                if param == "temperature" and "only 1 is allowed" in str(e).lower() and kwargs.get("temperature") != 1:
                    self.fixed_temperature = 1.0
                    kwargs["temperature"] = 1
                    continue
                if param:
                    self.param_blacklist.add(param)
                    self._sanitize_kwargs(kwargs)
                    continue
                raise
            except Exception as e:
                if "stream_options" in str(e) and "stream_options" in kwargs:
                    kwargs.pop("stream_options", None)
                    continue
                raise
        if stream is None:
            raise last_error

        first_token_ts = None
        final_usage = None

        for chunk in stream:
            # usage 可能附在最后一块（部分网关会连同 choices 一起给）——
            # 每块都检查，不能只在无 choices 的块里读（否则流式 usage 永远丢失）
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                final_usage = chunk_usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue

            # 正文增量
            content = getattr(delta, "content", None)
            if content:
                if first_token_ts is None:
                    first_token_ts = time.time()
                yield StreamEvent(type="text_delta", text=content)

            # 推理增量（思考模式 / o 系列）。
            # 关键：字段存在即产出事件（text 可为空），thinking 服务要求
            # 下一轮请求回传该字段（空串也要带上），否则 400。
            rc = getattr(delta, "reasoning_content", None)
            r2 = getattr(delta, "reasoning", None)
            if rc is not None or r2 is not None:
                text = rc if rc is not None else r2
                if text:
                    if first_token_ts is None:
                        first_token_ts = time.time()
                yield StreamEvent(
                    type="reasoning_delta",
                    text=text or "",
                    field="reasoning_content" if rc is not None else "reasoning",
                )

            # 工具调用增量
            if delta.tool_calls:
                if first_token_ts is None:
                    first_token_ts = time.time()
                for tc in delta.tool_calls:
                    if tc.index is None:
                        continue
                    fn = tc.function
                    yield StreamEvent(
                        type="tool_delta",
                        tool_index=tc.index,
                        tool_name=getattr(fn, "name", "") or "",
                        tool_args_delta=getattr(fn, "arguments", "") or "",
                    )

            finish = choice.finish_reason
            if finish:
                yield StreamEvent(type="done", finish_reason=finish)

        elapsed = time.time() - t0
        self._record_usage(
            final_usage,
            elapsed,
            first_token=(first_token_ts - t0) if first_token_ts is not None else None,
        )

    def supports_tools(self) -> bool:
        """探测当前供应商是否支持 function calling（一次 0-token 探测）。

        注意：执行器实际用的是 `_looks_like_unsupported()`（只在报错时判定），
        本方法目前没有调用方，保留供外部/脚本探测用。

        异常处理必须区分类型：旧实现把**任何**异常都吞成 False——一次网络抖动
        或 429 会被读成"该模型不支持 function calling"，进而静默降级到经典
        计划模式（用户看到的是行为突变，而不是一个网络错误）。
        """
        try:
            self.client.chat.completions.create(
                model=self.default_model,
                messages=[{"role": "user", "content": "ping"}],
                tools=[{"type": "function", "function": {"name": "ping", "description": "noop", "parameters": {"type": "object", "properties": {}}}}],
                tool_choice="none",
                max_tokens=1,
            )
            return True
        except _bad_request_error() as e:
            # 明确是"参数/能力"类错误 → 确实不支持
            msg = str(e).lower()
            if any(k in msg for k in ("tool", "function", "unsupported", "not support")):
                return False
            raise
        # 网络类错误（超时/限流/连接失败）→ 不能拿它当"不支持"，交给调用方处理
        except _openai_errors() as e:
            raise RuntimeError(f"探测 function calling 失败（非能力问题）: {str(e)[:200]}") from e
