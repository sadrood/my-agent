"""LLM 模块：原生 function calling + JSON 模式 + 自动重试 + 流式输出；换模型只改 .env。
chat() 保留旧文本接口（Planner / 总结 / Team 仍在用），chat_with_tools() 走原生工具协议。
限流(429)/服务端/连接错误按指数退避重试——上游限流频繁，这一步不能省。"""
import re
import time
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

# 延迟导入 SDK（关键性能优化）： SDK 的 __init__ 会级联导入大量类型定义（types.beta / graders / eval 等），实测耗时 ~1.7s，占 CLI 启动总耗时的 84%。
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


def _api_error():
    """任何 SDK 层错误（含 404/403/401 这类硬拒绝）。"""
    from openai import APIError as _AE
    return _AE


def _bad_request_error():
    """BadRequestError 类（惰性导入，用于错误体解析分支）。"""
    from openai import BadRequestError as _BRE
    return _BRE


def unwrap_raw_arguments(arguments: dict, max_depth: int = 5) -> dict:
    """工具参数 `_raw` 解包（递归、有界）。
    仅当 arguments 是 dict 且含 `_raw` 时解一层；解不开就原样返回，交给"解析失败"路径。
    非 dict 值包成 {"input": ...}。"""
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

# 可重试的异常类型（指数退避）——改为惰性解析（见 _openai_errors()），避免模块导入期加载整个 SDK（约 1.7s）。保留名字供旧代码/文档引用。
RETRYABLE_ERRORS = ()


def _first_choice_message(response):
    """取第一个 choice 及其 message。
    网关会把上游错误包成 HTTP 200 + {"choices": []}，直接取下标只会抛晦涩的 IndexError。
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
    """是否为按分钟窗口重置的配额耗尽（TPM/RPM）。
    这类错误必须跨过分钟边界再试——退避 6s/12s 会全部撞在同一窗口内。
    """
    msg = str(e).lower()
    return any(k in msg for k in (
        "tpm", "rpm", "tokens per minute", "requests per minute",
        "per minute", "rate limit exceeded", "quota_exceeded",
    ))


def _is_quota_error(e: Exception) -> bool:
    """是否为配额/限流类错误（429）。
    有网关把限流包成 invalid_request_error，必须按错误文本识别，否则这类 429 会跳过重试。
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
    """流式响应事件（一次增量）：text_delta / reasoning_delta / tool_delta / done。"""
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

    @staticmethod
    def _provider_entries(config: dict = None) -> list:
        """解析备用链：`模型名` 用共享备用端点，`模型名@预设名` 用预设自己的端点与 key。"""
        cfg = LLM_CONFIG if config is None else config
        presets = cfg.get("fallback_presets") or {}
        # 留空 = 跟随主端点：最常见的一级兜底就是"同端点同 key，换个模型名"
        default_base = str(cfg.get("fallback_base_url") or cfg.get("base_url") or "")
        default_key = str(cfg.get("fallback_api_key") or cfg.get("api_key") or "")
        out = []
        for raw in cfg.get("fallback_models") or []:
            entry = str(raw).strip()
            if not entry:
                continue
            model, _, preset = entry.partition("@")
            preset = preset.strip().lower()
            hit = presets.get(preset, {}) if preset else {}
            base = str(hit.get("base_url") or default_base)
            key = str(hit.get("api_key") or default_key)
            if not (base and key and model.strip()):
                continue          # 端点/密钥缺失的条目直接跳过，别制造 401
            out.append({"model": model.strip(), "base_url": base, "api_key": key,
                        "label": f"{model.strip()}@{preset}" if preset else model.strip()})
        return out

    def _build_client(self, base_url: str, api_key: str):
        return _openai_client_class()(
            api_key=api_key,
            base_url=base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def _activate_provider(self, index: int, reason: str = "") -> None:
        """切到备用链上的第 index 个端点（0 = 主端点），并广播一次可见的切换说明。"""
        entry = self.providers[index]
        if entry.get("client") is None:
            entry["client"] = self._build_client(entry["base_url"], entry["api_key"])
        self.client = entry["client"]
        self.default_model = entry["model"]
        self.provider_index = index
        self.switched_at = time.time()
        self._notify_provider(entry, index, reason)

    def _active_meta(self) -> dict:
        """当前端点的标识信息（遥测用）。"""
        entry = self.providers[self.provider_index]
        return {"label": entry.get("label", ""), "model": entry.get("model", ""),
                "base_url": entry.get("base_url", "")}

    def _notify_health(self, event: dict) -> None:
        cb = getattr(self, "health_notifier", None)
        if cb is None:
            return
        try:
            cb(event)
        except Exception:      # noqa: BLE001 — 遥测失败不能影响主流程
            pass

    def _notify_provider(self, entry: dict, index: int, reason: str) -> None:
        cb = getattr(self, "provider_notifier", None)
        if cb is None:
            return
        try:
            cb({"label": entry.get("label", ""), "model": entry.get("model", ""),
                "base_url": entry.get("base_url", ""), "index": index,
                "primary": index == 0, "reason": reason})
        except Exception:      # noqa: BLE001 — 提示失败不能影响主流程
            pass

    def _maybe_probe_primary(self) -> None:
        """在备用端点待够 probe 秒后回试主端点：主端点只是限流，过一阵就该还给它。"""
        if self.provider_index == 0 or len(self.providers) < 2:
            return
        if time.time() - self.switched_at < self.probe_seconds:
            return
        self._activate_provider(0, "回试主端点")

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        """构造客户端；三个参数只覆盖 LLM_CONFIG 的密钥/地址/模型，不写 .env。"""
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
        # 固定温度：某些 thinking 模型只接受特定值（如必须为 1），设置后忽略所有传入 temperature（否则 400 invalid_request_error）
        self.fixed_temperature = LLM_CONFIG.get("fixed_temperature")
        # 该模型已知不支持的请求参数（400 后自动记录，后续请求自动移除/转换）
        self.param_blacklist: set[str] = set()

        # 自动重试配置（OpenRouter 等供应商限流时指数退避）
        self.max_retries = int(LLM_CONFIG.get("max_retries", 2))
        # 备用链：0 号是主端点；限流连续失败 fallback_after 次就切下一个
        self.providers = [{"model": self.default_model, "base_url": str(self.client.base_url),
                           "api_key": self.client.api_key, "label": "主端点",
                           "client": self.client}]
        for entry in self._provider_entries():
            self.providers.append(dict(entry, client=None))
        self.provider_index = 0
        self.quota_strikes = 0
        self.switched_at = time.time()
        self.fallback_after = max(1, int(LLM_CONFIG.get("fallback_after", 2)))
        self.probe_seconds = float(LLM_CONFIG.get("fallback_probe_seconds", 300))
        self.provider_notifier = None
        self.health_notifier = None      # 健康遥测回调（Agent 注入）
        # 401 突发保护：记住哪些端点本会话成功过；成功一次就重置退避次数
        self.auth_retry_max = max(0, int(LLM_CONFIG.get("auth_retry_max", 2)))
        self.auth_retry_delay = float(LLM_CONFIG.get("auth_retry_delay", 2.0))
        self.success_labels = set()
        self.auth_retry_left = self.auth_retry_max
        # : 限流等待回调（可选）：Agent 注入 cb(seconds, attempt)，用于把"正在等配额": 显示给用户并记进 rollout。见 _notify_rate_limit。
        self.retry_notifier = None
        self.retry_base_delay = float(LLM_CONFIG.get("retry_base_delay", 2.0))

        # 运行时统计（由 Agent 注入 RunMetrics，可留空）
        self.metrics = None

    def _model_fixed_temperature(self) -> float:
        """按模型名适配固定温度（.env 显式设置优先）。
        部分 thinking 模型只接受 temperature=1。
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
        """解析请求温度：固定温度优先。
        thinking 模型传入非 1 会 400 "only 1 is allowed for this model"。
        """
        fixed = self._model_fixed_temperature()
        if fixed is not None:
            return fixed
        return temperature if temperature is not None else self.default_temperature

    def _extract_bad_param(self, e: Exception) -> Optional[str]:
        """从 400 错误提取不受支持的参数名（None = 无法识别）：先读 param 字段，正则兜底。"""
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
        """从网关 /models 查模型的 context_length；查不到返回 None（调用方走兜底），按 (base, model) 缓存。"""
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
        """按已知参数限制清洗请求：黑名单参数移除；max_tokens 被禁时转 max_completion_tokens。"""
        for name in list(kwargs):
            if name not in self.param_blacklist:
                continue
            if name == "max_tokens" and "max_completion_tokens" not in kwargs:
                kwargs["max_completion_tokens"] = kwargs[name]
            kwargs.pop(name, None)

    def _create_with_retry(self, kwargs: dict):
        """带退避重试的 completions.create；限流重试耗尽且配了备用链时切下一个端点。"""
        self._sanitize_kwargs(kwargs)  # 发送前按已知限制清洗
        self._maybe_probe_primary()    # 备用端点待久了就回试主端点
        last_error = None
        attempt = 0
        switches = 0
        max_switches = max(0, len(self.providers) - 1)
        while attempt <= self.max_retries:
            t0 = time.time()
            try:
                resp = self.client.chat.completions.create(**kwargs)
                self.success_labels.add(self._active_meta()["label"])
                self.auth_retry_left = self.auth_retry_max
                self._notify_health(dict(self._active_meta(), event="call_ok",
                                         seconds=round(time.time() - t0, 3)))
                return resp
            except _bad_request_error() as e:
                if _is_quota_error(e):
                    # 429 变体（包装成 invalid_request_error 的配额错误）：提取不出参数名，直接 raise 会导致零重试——按限流处理
                    last_error = e
                    if attempt >= self.max_retries:
                        self._notify_health(dict(self._active_meta(), event="quota_limited",
                                                 wait=0.0, attempt=attempt + 1,
                                                 message=str(e)[:120]))
                        if switches < max_switches and self._failover(kwargs, "限流"):
                            switches += 1
                            attempt = 0        # 换了端点：重新给满重试预算
                            continue
                        raise
                    wait = self._quota_wait(e, attempt)
                    self._notify_health(dict(self._active_meta(), event="quota_limited",
                                             wait=round(wait, 1), attempt=attempt + 1,
                                             message=str(e)[:120]))
                    time.sleep(wait)
                    attempt += 1
                    continue
                last_error = e
                param = self._extract_bad_param(e)
                # temperature 特例：thinking 模型硬性要求 1（错误文本可能带"temperature" 字样也可能不带，统一按 param 识别）
                if param == "temperature" and "only 1 is allowed" in str(e).lower() and kwargs.get("temperature") != 1:
                    self.fixed_temperature = 1.0  # 记住该模型限制，后续请求直接生效
                    kwargs["temperature"] = 1
                    attempt += 1
                    continue
                if param:
                    self.param_blacklist.add(param)
                    self._sanitize_kwargs(kwargs)
                    attempt += 1
                    continue
                raise
            except _openai_errors() as e:
                last_error = e
                if attempt >= self.max_retries:
                    if _is_quota_error(e):
                        self._notify_health(dict(self._active_meta(), event="quota_limited",
                                                 wait=0.0, attempt=attempt + 1,
                                                 message=str(e)[:120]))
                    reason = "限流" if _is_quota_error(e) else "上游错误"
                    if switches < max_switches and self._failover(kwargs, reason):
                        switches += 1
                        attempt = 0            # 换了端点：重新给满重试预算
                        continue
                    raise
                if _is_quota_error(e):
                    wait = self._quota_wait(e, attempt)
                    self._notify_health(dict(self._active_meta(), event="quota_limited",
                                             wait=round(wait, 1), attempt=attempt + 1,
                                             message=str(e)[:120]))
                    time.sleep(wait)
                else:
                    time.sleep(self._retry_delay(attempt))
                attempt += 1
            except _api_error() as e:
                # 端点硬拒绝：404（模型不在该端点）/ 403（不在套餐内）换端点能救。
                # 401 要分情况——上游在大请求连发时用"无效的令牌"做突发保护，而本会话这个
                # 端点明明成功过；误判成 key 失效会把整条备用链白切一遍，所以先退避重试。
                last_error = e
                code = getattr(e, "status_code", "")
                meta = self._active_meta()
                if (code == 401 and meta["label"] in self.success_labels
                        and self.auth_retry_left > 0):
                    self.auth_retry_left -= 1
                    delay = self.auth_retry_delay * (self.auth_retry_max - self.auth_retry_left)
                    self._notify_health(dict(meta, event="auth_burst_retry", wait=round(delay, 1),
                                             message=f"401 突发保护，退避重试：{str(e)[:80]}"))
                    time.sleep(delay)
                    continue
                self._notify_health(dict(meta, event="call_error",
                                         message=f"{code} {str(e)[:100]}"))
                if switches < max_switches and self._failover(kwargs, f"上游 {code} 拒绝"):
                    switches += 1
                    attempt = 0            # 换了端点：重新给满重试预算
                    continue
                raise
        raise last_error

    def _failover(self, kwargs: dict, reason: str) -> bool:
        """限流连续失败达阈值时切到备用端点（True=已切换）；同时换成本次调用的模型名。"""
        if len(self.providers) < 2:
            return False
        self.quota_strikes += 1
        if self.quota_strikes < self.fallback_after:
            return False
        self.quota_strikes = 0
        target = min(self.provider_index + 1, len(self.providers) - 1)   # 逐级往下，不绕回主端点
        if target == self.provider_index:
            return False            # 已经在链尾
        self._activate_provider(target, reason)
        kwargs["model"] = self.providers[target]["model"]
        self._notify_health(dict(self._active_meta(), event="switch",
                                 primary=target == 0, reason=reason))
        return True

    def _retry_delay(self, attempt: int, quota: bool = False) -> float:
        """退避延迟：限流类错误起点更长（配额按分钟窗口，2s 起步太密）。"""
        base = self.retry_base_delay * (3 if quota else 1)
        return base * (2 ** attempt)

    def _quota_wait(self, err, attempt: int) -> float:
        """429 等待：优先 retry-after，否则对齐到下一个分钟边界，单次上限 65s。
        分钟窗口配额下纯指数退避会一直在同一窗口内硬怼。
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
        # 长等待（分钟级窗口）给出可见提示：否则 CLI 静默等 1 分钟，看起来像卡死。走 stderr，不干扰富文本 stdout 渲染。
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
        """把"正在等配额"通知上层（可选回调；回调抛异常不影响重试）。
        stderr 提示在桌面端/dashboard 看不见，等待时长此前也不进 rollout——必须走这个回调。
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
        """发送对话消息返回文本（旧接口，Planner / 总结 / Team 仍在用）。"""
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
        """原生 function calling 调用；tools 传 BaseTool.to_openai_schema() 的结果。"""
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

        # 思考模式：捕获推理内容（需在下一轮回传，否则服务器 400）关键：字段存在（即使空串）也要标记，thinking 服务要求每条 assistant 消息回传该字段
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
                # 统一解包：合法的嵌套 _raw（模型把参数再包一层 JSON 字符串）也能还原出真正的命名参数；解析失败产生的 _raw 解不开、保持原样，由执行器按"参数解析失败"拦截。
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
        """流式 function calling：产出 text_delta / reasoning_delta / tool_delta / done 事件。
        流已开始后网络错误无法重试，只在建立连接阶段重试。
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

        # 建立流：连接阶段重试 + 备用链切换；不支持 stream_options 的供应商自动降级。
        # 限流必须走 _quota_wait（对齐分钟边界）并允许切换端点——主循环走的就是这条路，
        # 不给它备用链，等于限流时整轮白等重试完再判死。
        stream = None
        last_error = None
        attempt = 0
        switches = 0
        max_switches = max(0, len(self.providers) - 1)
        while attempt <= self.max_retries:
            try:
                # 计时必须从**发起请求前**开始。之前 t0 取在 create() 返回之后。
                t0 = time.time()
                stream = self.client.chat.completions.create(**kwargs)
                self.success_labels.add(self._active_meta()["label"])
                self.auth_retry_left = self.auth_retry_max
                self._notify_health(dict(self._active_meta(), event="call_ok",
                                         seconds=round(time.time() - t0, 3)))
                break
            except _openai_errors() as e:
                last_error = e
                if _is_quota_error(e):
                    if attempt >= self.max_retries:
                        self._notify_health(dict(self._active_meta(), event="quota_limited",
                                                 wait=0.0, attempt=attempt + 1,
                                                 message=str(e)[:120]))
                        if switches < max_switches and self._failover(kwargs, "限流"):
                            switches += 1
                            attempt = 0
                            continue
                        raise
                    wait = self._quota_wait(e, attempt)
                    self._notify_health(dict(self._active_meta(), event="quota_limited",
                                             wait=round(wait, 1), attempt=attempt + 1,
                                             message=str(e)[:120]))
                    time.sleep(wait)
                    attempt += 1
                    continue
                if attempt >= self.max_retries:
                    raise
                time.sleep(self._retry_delay(attempt))
                attempt += 1
            except _bad_request_error() as e:
                last_error = e
                if _is_quota_error(e):
                    # 429 变体（包装成 invalid_request_error）：按限流重试，同样能切备用链
                    if attempt >= self.max_retries:
                        self._notify_health(dict(self._active_meta(), event="quota_limited",
                                                 wait=0.0, attempt=attempt + 1,
                                                 message=str(e)[:120]))
                        if switches < max_switches and self._failover(kwargs, "限流"):
                            switches += 1
                            attempt = 0
                            continue
                        raise
                    wait = self._quota_wait(e, attempt)
                    self._notify_health(dict(self._active_meta(), event="quota_limited",
                                             wait=round(wait, 1), attempt=attempt + 1,
                                             message=str(e)[:120]))
                    time.sleep(wait)
                    attempt += 1
                    continue
                if "stream_options" in str(e) and "stream_options" in kwargs:
                    kwargs.pop("stream_options", None)
                    attempt += 1
                    continue
                param = self._extract_bad_param(e)
                if param == "temperature" and "only 1 is allowed" in str(e).lower() and kwargs.get("temperature") != 1:
                    self.fixed_temperature = 1.0
                    kwargs["temperature"] = 1
                    attempt += 1
                    continue
                if param:
                    self.param_blacklist.add(param)
                    self._sanitize_kwargs(kwargs)
                    attempt += 1
                    continue
                raise
            except Exception as e:
                if "stream_options" in str(e) and "stream_options" in kwargs:
                    kwargs.pop("stream_options", None)
                    attempt += 1
                    continue
                code = getattr(e, "status_code", "")
                meta = self._active_meta()
                if (code == 401 and meta["label"] in self.success_labels
                        and self.auth_retry_left > 0):
                    self.auth_retry_left -= 1
                    delay = self.auth_retry_delay * (self.auth_retry_max - self.auth_retry_left)
                    self._notify_health(dict(meta, event="auth_burst_retry", wait=round(delay, 1),
                                             message=f"401 突发保护，退避重试：{str(e)[:80]}"))
                    time.sleep(delay)
                    continue
                if code in (401, 403, 404):
                    self._notify_health(dict(meta, event="call_error",
                                             message=f"{code} {str(e)[:100]}"))
                    if switches < max_switches and self._failover(kwargs, f"上游 {code} 拒绝"):
                        switches += 1
                        attempt = 0
                        continue
                raise
        if stream is None:
            raise last_error

        first_token_ts = None
        final_usage = None

        for chunk in stream:
            # usage 可能附在最后一块（部分网关会连同 choices 一起给）——每块都检查，不能只在无 choices 的块里读（否则流式 usage 永远丢失）
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

            # 推理增量（思考模式 / o 系列）。关键：字段存在即产出事件（text 可为空），thinking 服务要求下一轮请求回传该字段（空串也要带上），否则 400。
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
        异常必须按类型区分：把任何异常都吞成 False 会让网络抖动/429 被读成"不支持"，
        进而静默降级到经典计划模式。执行器实际用 _looks_like_unsupported()，本方法供外部探测。"""
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
