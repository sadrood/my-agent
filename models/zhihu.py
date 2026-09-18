# -*- coding: utf-8 -*-
"""知乎数据开放平台客户端（https://developer.zhihu.com/docs）。

契约（2026-09-18 逐个接口核实）：
    鉴权: Authorization: Bearer <access_secret>
          X-Request-Timestamp: <秒级 Unix 时间戳>（与服务端相差不得超过 10 分钟）
    信封: {"Code": 0, "Message": "success", "Data": {...}}
          Code != 0 时 Data 通常为 null，原因在 Message 里（错误码表见 ERROR_HINTS）
    特例: 直答 /v1/chat/completions 是 **OpenAI 兼容**格式（没有 Code/Message/Data
          信封，错误是 {"error": {...}}），并且路径**不带 /api 前缀**。

两类容易踩的坑，本模块都已处理：
  1. 时钟偏差：X-Request-Timestamp 必须是"服务端认的现在"。本机时钟漂移超过
     10 分钟会直接 20001。这里会从响应的 Date 头学习偏差并自动重试一次。
  2. 参数名是 PascalCase（Query/Count/Limit/QuestionUrl/...），而工具侧用
     snake_case；映射统一放在本模块，工具层不拼 URL。

额度是按能力独立计的（每项每日限免），可用 quota() 实时查询。
"""
import json
import mimetypes
import os
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from config import ZHIHU_CONFIG


class ZhihuError(RuntimeError):
    """知乎开放平台调用失败（附可行动的说明）。"""


#: 业务错误码 → 可行动的说明（Code != 0 时使用）
ERROR_HINTS: Dict[int, str] = {
    10001: "请求参数错误（或内容不可用）：检查是否漏填必填项、URL 是否是本平台认可的知乎链接。",
    20001: "鉴权失败：核对 ZHIHU_ACCESS_SECRET 是否正确；若 Secret 无误，多为"
           "X-Request-Timestamp 与服务端相差超过 10 分钟（本模块会自动纠正一次）。",
    30001: "频率/并发/当日额度超限：稍后重试，或用 quota 命令查看剩余额度。",
    30002: "额外配置的累计成功次数额度已耗尽。",
    30003: "请求被风控拒绝：换个查询词或稍后再试。",
    40001: "幂等键与请求参数冲突（Idempotency-Key 相同但参数不同）。",
    40002: "文件不存在、已过期或不可访问：重新上传。",
    40003: "活跃任务数超限：等已有任务完成后再提交（PDF 解析/PPT 生成）。",
    40004: "知识库不存在：用 kb_list 确认 KnowledgeBaseID。",
    40005: "相同文件正在处理中：稍后再试。",
    40006: "文件解析失败：确认文件本身可正常打开、格式受支持。",
    50002: "知识库检索失败：稍后重试。",
    90001: "服务端内部错误：稍后重试。",
}

#: 命令 → (HTTP 方法, 路径)。路径里的 {kb_id} 由调用方替换。
ENDPOINTS: Dict[str, tuple] = {
    "quota": ("GET", "/api/v1/quota"),
    "search": ("GET", "/api/v1/content/zhihu_search"),
    "web_search": ("GET", "/api/v1/content/global_search"),
    "hot": ("GET", "/api/v1/content/hot_list"),
    "answers": ("GET", "/api/v1/content/question_answers"),
    "recommend": ("GET", "/api/v1/user/question_recommendations"),
    "my_contents": ("GET", "/api/v1/user/contents"),
    "content_detail": ("GET", "/api/v1/user/content_detail"),
    "content_comments": ("GET", "/api/v1/user/content_comments"),
    "account_stats": ("GET", "/api/v1/user/creator_account_stats"),
    "content_stats": ("GET", "/api/v1/user/creator_content_stats"),
    "followees": ("GET", "/api/v1/user/followees"),
    "collections": ("GET", "/api/v1/user/collections"),
    "favlists": ("GET", "/api/v1/user/favlists"),
    "favlist_items": ("GET", "/api/v1/user/favlist_contents"),
    "kb_list": ("GET", "/api/v1/knowledge/bases"),
    "kb_items": ("GET", "/api/v1/knowledge/bases/{kb_id}/items"),
    "kb_upload": ("POST", "/api/v1/knowledge/files"),
    "kb_search": ("POST", "/api/v1/knowledge/search"),
    "zhida": ("POST", "/v1/chat/completions"),
    "upload_file": ("POST", "/resources/v1/files"),
    "pdf_parse": ("POST", "/api/v1/pdf-parse/tasks"),
    "ppt": ("POST", "/api/v1/ppt-generation/tasks"),
}

#: 直答可用模型档位
ZHIDA_MODELS = ("zhida-fast-1p5", "zhida-thinking-1p5", "zhida-agent")

#: 知识库文件上传支持的扩展名（文档给定）
KB_FILE_TYPES = ("pdf", "md", "txt", "ppt", "pptx", "xlsx", "xls", "docx", "doc",
                 "webp", "png", "jpg", "mobi", "epub", "csv", "azw3")

#: 异步任务终态
TASK_DONE, TASK_FAILED = "succeeded", "failed"


def mask_secret(secret: str) -> str:
    """把密钥打码后再展示/落日志（任何输出都不得出现完整 Secret）。"""
    s = secret or ""
    if len(s) <= 10:
        return "*" * len(s)
    return f"{s[:6]}…{s[-4:]}（共 {len(s)} 位）"


class ZhihuClient:
    """知乎开放平台 REST 客户端（无状态：每次请求带 Bearer 头）。"""

    def __init__(self, access_secret: str = None, base_url: str = None,
                 timeout: float = None, max_chars: int = None,
                 task_timeout: float = None, poll_interval: float = None,
                 save_dir: str = None):
        cfg = ZHIHU_CONFIG
        self.access_secret = (access_secret if access_secret is not None
                              else cfg.get("access_secret", "")).strip()
        self.base_url = str(base_url or cfg.get("base_url",
                                               "https://developer.zhihu.com")).rstrip("/")
        self.timeout = float(timeout or cfg.get("timeout", 30))
        self.llm_timeout = float(cfg.get("llm_timeout", 180))
        self.upload_timeout = float(cfg.get("upload_timeout", 300))
        self.task_timeout = float(task_timeout if task_timeout is not None
                                  else cfg.get("task_timeout", 600))
        self.poll_interval = float(poll_interval if poll_interval is not None
                                   else cfg.get("poll_interval", 3))
        self.max_chars = int(max_chars or cfg.get("max_chars", 12000))
        self.save_dir = str(save_dir or cfg.get("save_dir", "output/zhihu"))
        # 本机时钟与服务端的偏差（秒），从响应 Date 头学习
        self._skew = 0.0

    # ------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.access_secret)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_secret}",
            "X-Request-Timestamp": str(int(time.time() + self._skew)),
            "Content-Type": "application/json",
        }

    def _learn_clock(self, resp) -> bool:
        """从响应 Date 头学习时钟偏差；返回是否真的修正了。"""
        raw = resp.headers.get("date")
        if not raw:
            return False
        try:
            server_ts = parsedate_to_datetime(raw).timestamp()
        except Exception:       # noqa: BLE001 — Date 头异常时不影响主流程
            return False
        new_skew = server_ts - time.time()
        # 只关心"是否值得重试"：偏差过半分钟就认为本机时钟不准
        changed = abs(new_skew - self._skew) > 0.5
        self._skew = new_skew
        return changed

    # ------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------

    def request(self, method: str, path: str, params: Dict[str, Any] = None,
                json_body: Any = None, files: Dict[str, Any] = None,
                data: Dict[str, Any] = None, timeout: float = None,
                envelope: bool = True, _retry_auth: bool = True) -> Any:
        """发一次请求并解信封，返回 Data 部分。

        Args:
            envelope: False 表示该端点不是 Code/Message/Data 信封（如直答）。
            _retry_auth: 内部用；鉴权失败时用学到的时钟偏差重试一次。
        """
        import httpx

        if not self.configured:
            raise ZhihuError(
                "未配置知乎 Access Secret。请在 .env 里设置 "
                "ZHIHU_ACCESS_SECRET=<在 https://developer.zhihu.com/profile 获取的 "
                "Access Secret>，然后重启 agent。")

        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = self._headers()
        if files is not None:
            # multipart 由 httpx 生成 boundary，不能自己带 Content-Type
            headers.pop("Content-Type", None)
        try:
            resp = httpx.request(
                method.upper(), url, params=params, json=json_body,
                files=files, data=data, headers=headers,
                timeout=timeout if timeout is not None else self.timeout,
                trust_env=True)
        except httpx.HTTPError as e:
            raise ZhihuError(
                f"连不上知乎开放平台（{self.base_url}）：{str(e)[:160]}。"
                "检查网络/代理，或改 ZHIHU_BASE_URL。") from e

        body = self._decode(resp)
        # 每次响应都顺手校准时钟（Date 头），这样偏差不会累积
        clock_moved = self._learn_clock(resp)

        if envelope and isinstance(body, dict) and body.get("Code") not in (0, None):
            code = body.get("Code")
            message = str(body.get("Message") or "")[:300]
            # 20001 常见原因是时间戳超窗：用服务端时间纠正后重试一次
            if code == 20001 and _retry_auth and clock_moved:
                return self.request(method, path, params=params, json_body=json_body,
                                    files=files, data=data, timeout=timeout,
                                    envelope=envelope, _retry_auth=False)
            raise ZhihuError(self._explain(code, message, path))

        if resp.status_code >= 400:
            raise ZhihuError(self._explain_http(resp.status_code, path, body))

        if not envelope:
            return body
        if isinstance(body, dict):
            return body.get("Data")
        return body

    @staticmethod
    def _decode(resp) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _explain(code: Any, message: str, path: str) -> str:
        hint = ERROR_HINTS.get(code, "")
        tail = f" {hint}" if hint else ""
        return f"知乎接口返回错误 {code}（{path}）：{message}。{tail}".strip()

    @staticmethod
    def _explain_http(status: int, path: str, body: Any) -> str:
        detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        detail = str(detail)[:400]
        if status in (401, 403):
            return (f"知乎鉴权被拒（HTTP {status}，{path}）：{detail}。"
                    "核对 ZHIHU_ACCESS_SECRET。")
        if status == 404:
            return (f"知乎接口路径不存在（HTTP 404，{path}）：{detail}。"
                    "用 routes 命令查看已核实可用的路径。")
        if status == 429:
            return (f"知乎限流（HTTP 429，{path}）：{detail}。稍后重试或用 quota 看额度。")
        return f"知乎调用失败（HTTP {status}，{path}）：{detail}"

    def get(self, path: str, params: Dict[str, Any] = None, **kw) -> Any:
        return self.request("GET", path, params=_drop_none(params), **kw)

    def post(self, path: str, json_body: Any = None, **kw) -> Any:
        return self.request("POST", path, json_body=json_body, **kw)

    # ------------------------------------------------------------
    # 高层接口（参数名 snake_case，内部映射成上游 PascalCase）
    # ------------------------------------------------------------

    def quota(self, api_ids: Optional[List[str]] = None) -> Any:
        params = {"APIIDs": ",".join(api_ids)} if api_ids else None
        return self.get(ENDPOINTS["quota"][1], params)

    def search(self, query: str, count: int = 10, sort_by: str = None) -> Any:
        """知乎站内搜索（Count 上限 10）。"""
        return self.get(ENDPOINTS["search"][1],
                        {"Query": query, "Count": _clamp(count, 1, 10),
                         "SortBy": sort_by})

    def web_search(self, query: str, count: int = 10, filter: str = None,
                   search_db: str = None) -> Any:
        """全网搜索（Count 上限 20；Filter 是高级语法，如 host=="x.com"）。"""
        return self.get(ENDPOINTS["web_search"][1],
                        {"Query": query, "Count": _clamp(count, 1, 20),
                         "Filter": filter, "SearchDB": search_db})

    def hot(self, limit: int = 30) -> Any:
        """知乎热榜（Limit 上限 30）。"""
        return self.get(ENDPOINTS["hot"][1], {"Limit": _clamp(limit, 1, 30)})

    def answers(self, question_url: str, offset: int = 0, limit: int = 20) -> Any:
        return self.get(ENDPOINTS["answers"][1],
                        {"QuestionUrl": question_url, "Offset": max(0, int(offset)),
                         "Limit": _clamp(limit, 1, 50)})

    def recommend(self, query: str = None, count: int = 5) -> Any:
        """问题推荐（不传 Query 时按账号画像推荐）。"""
        return self.get(ENDPOINTS["recommend"][1],
                        {"Query": query, "Count": _clamp(count, 1, 20)})

    def my_contents(self, content_type: str = "all", offset: int = 0, limit: int = 20,
                    sort_field: str = None, sort_order: str = None) -> Any:
        return self.get(ENDPOINTS["my_contents"][1],
                        {"ContentType": content_type or "all", "Offset": max(0, int(offset)),
                         "Limit": _clamp(limit, 1, 50), "SortField": sort_field,
                         "SortOrder": sort_order})

    def content_detail(self, content_url: str) -> Any:
        """本人创作全文（只支持当前账号自己的内容）。"""
        return self.get(ENDPOINTS["content_detail"][1], {"ContentUrl": content_url})

    def content_comments(self, content_url: str, offset: int = 0, limit: int = 20,
                         order: str = None) -> Any:
        return self.get(ENDPOINTS["content_comments"][1],
                        {"ContentUrl": content_url, "Offset": max(0, int(offset)),
                         "Limit": _clamp(limit, 1, 50), "Order": order})

    def account_stats(self, content_type: str = "all", start_date: str = None,
                      end_date: str = None) -> Any:
        return self.get(ENDPOINTS["account_stats"][1],
                        {"ContentType": content_type or "all",
                         "StartDate": start_date, "EndDate": end_date})

    def content_stats(self, content_url: str, start_date: str = None,
                      end_date: str = None) -> Any:
        return self.get(ENDPOINTS["content_stats"][1],
                        {"ContentUrl": content_url, "StartDate": start_date,
                         "EndDate": end_date})

    def followees(self, offset: int = 0, limit: int = 20) -> Any:
        return self.get(ENDPOINTS["followees"][1],
                        {"Offset": max(0, int(offset)), "Limit": _clamp(limit, 1, 50)})

    def collections(self, limit: int = 20) -> Any:
        return self.get(ENDPOINTS["collections"][1], {"Limit": max(1, int(limit))})

    def favlists(self, limit: int = 20) -> Any:
        return self.get(ENDPOINTS["favlists"][1], {"Limit": max(1, int(limit))})

    def favlist_items(self, favlist_token: Any, offset: int = 0, limit: int = 20) -> Any:
        return self.get(ENDPOINTS["favlist_items"][1],
                        {"FavlistUrlToken": favlist_token, "Offset": max(0, int(offset)),
                         "Limit": _clamp(limit, 1, 50)})

    # ---- 知识库 ----

    def kb_list(self, scope: str = "all") -> Any:
        return self.get(ENDPOINTS["kb_list"][1], {"Scope": scope or "all"})

    def kb_items(self, kb_id: str, cursor: str = None, limit: int = 20) -> Any:
        path = ENDPOINTS["kb_items"][1].format(kb_id=kb_id)
        return self.get(path, {"Cursor": cursor, "Limit": _clamp(limit, 1, 20)})

    def kb_search(self, query: str, kb_ids: Optional[List[str]] = None,
                  scopes: Optional[List[str]] = None, limit: int = 10) -> Any:
        body = {"Query": query, "Limit": _clamp(limit, 1, 10)}
        if kb_ids:
            body["KnowledgeBaseIDs"] = list(kb_ids)
        if scopes:
            body["RecallScopes"] = list(scopes)
        return self.post(ENDPOINTS["kb_search"][1], body)

    def kb_upload(self, path: str, kb_id: str = None) -> Any:
        """上传单个文件到知识库（不传 kb_id 时进默认知识库）。"""
        full = _require_file(path)
        ext = os.path.splitext(full)[1].lstrip(".").lower()
        if ext and ext not in KB_FILE_TYPES:
            raise ZhihuError(
                f"知识库不支持 .{ext} 格式。支持：{', '.join(KB_FILE_TYPES)}")
        with open(full, "rb") as f:
            content = f.read()
        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        files = {"File": (os.path.basename(full), content, mime)}
        data = {"KnowledgeBaseID": kb_id} if kb_id else None
        return self.request("POST", ENDPOINTS["kb_upload"][1], files=files, data=data,
                            timeout=self.upload_timeout)

    # ---- 直答（OpenAI 兼容，非信封格式）----

    def zhida(self, question: str, model: str = "zhida-thinking-1p5",
              history: Optional[List[Dict[str, str]]] = None,
              stream: bool = False) -> Dict[str, Any]:
        """知乎直答。返回 {content, reasoning, model, usage?}。"""
        if model not in ZHIDA_MODELS:
            raise ZhihuError(
                f"未知直答模型 {model!r}。可用：{', '.join(ZHIDA_MODELS)}")
        messages = list(history or [])
        messages.append({"role": "user", "content": question})
        body = {"model": model, "messages": messages, "stream": bool(stream)}
        if stream:
            return self._zhida_stream(body)
        data = self.request("POST", ENDPOINTS["zhida"][1], json_body=body,
                            timeout=self.llm_timeout, envelope=False)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise ZhihuError(f"直答失败：{err.get('message') or err} "
                             f"(type={err.get('type')}, code={err.get('code')})")
        choices = (data or {}).get("choices") or [{}]
        msg = choices[0].get("message") or {}
        return {"content": msg.get("content") or "",
                "reasoning": msg.get("reasoning_content") or "",
                "model": (data or {}).get("model") or model,
                "usage": (data or {}).get("usage")}

    def _zhida_stream(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """流式累积 SSE 片段（delta.content / delta.reasoning_content）。"""
        import httpx

        content: List[str] = []
        reasoning: List[str] = []
        model = body.get("model")
        with httpx.stream("POST", f"{self.base_url}{ENDPOINTS['zhida'][1]}",
                          json=body, headers=self._headers(),
                          timeout=self.llm_timeout, trust_env=True) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise ZhihuError(self._explain_http(resp.status_code,
                                                    ENDPOINTS["zhida"][1],
                                                    self._decode(resp)))
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise ZhihuError(f"直答流式返回错误：{chunk['error']}")
                model = chunk.get("model") or model
                for ch in chunk.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
        return {"content": "".join(content), "reasoning": "".join(reasoning),
                "model": model, "usage": None}

    # ---- 小工具：PDF 解析 / PPT 生成（异步任务）----

    def upload_file(self, path: str) -> str:
        """上传 PDF 等文件，返回 file_id（PDF 解析第一步）。"""
        full = _require_file(path)
        with open(full, "rb") as f:
            content = f.read()
        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        data = self.request("POST", ENDPOINTS["upload_file"][1],
                            files={"file": (os.path.basename(full), content, mime)},
                            timeout=self.upload_timeout)
        file_id = (data or {}).get("file_id") if isinstance(data, dict) else None
        if not file_id:
            raise ZhihuError(f"上传成功但未返回 file_id：{str(data)[:200]}")
        return str(file_id)

    def create_pdf_task(self, file_id: str) -> str:
        data = self.post(ENDPOINTS["pdf_parse"][1], {"file_id": file_id})
        return str((data or {}).get("task_id") or "")

    def create_ppt_task(self, resource_url: str, num_pages: int = 12) -> str:
        pages = int(num_pages)
        if not 6 <= pages <= 21:
            raise ZhihuError(f"num_pages 必须在 6~21 之间，收到 {pages}。")
        data = self.post(ENDPOINTS["ppt"][1],
                         {"resource_url": resource_url, "num_pages": pages})
        return str((data or {}).get("task_id") or "")

    def task_status(self, kind: str, task_id: str) -> Dict[str, Any]:
        """查询 PDF/PPT 任务状态。kind: pdf | ppt。"""
        if kind not in ("pdf", "ppt"):
            raise ZhihuError(f"kind 只能是 pdf 或 ppt，收到 {kind!r}")
        path = f"/api/v1/{'pdf-parse' if kind == 'pdf' else 'ppt-generation'}/tasks/{task_id}"
        data = self.get(path)
        return data if isinstance(data, dict) else {}

    def wait_task(self, kind: str, task_id: str) -> Dict[str, Any]:
        """轮询到终态；超时抛 ZhihuError（附最后一次进度）。"""
        deadline = time.time() + self.task_timeout
        last = {}
        while True:
            last = self.task_status(kind, task_id)
            status = last.get("task_status")
            if status in (TASK_DONE, TASK_FAILED):
                return last
            if time.time() >= deadline:
                raise ZhihuError(
                    f"{kind} 任务 {task_id} 等待超时（{self.task_timeout:.0f}s，"
                    f"最后状态={status}，进度={last.get('progress')}）。"
                    "可用 task 命令继续查询该任务。")
            time.sleep(self.poll_interval)

    def download(self, url: str, filename: str = None) -> str:
        """把任务产物下载到 save_dir，返回本地路径（结果链接会过期）。"""
        import httpx

        os.makedirs(self.save_dir, exist_ok=True)
        name = filename or os.path.basename(url.split("?")[0]) or "zhihu_download"
        name = _safe_name(name)
        dest = os.path.join(self.save_dir, name)
        try:
            with httpx.stream("GET", url, timeout=self.upload_timeout,
                              follow_redirects=True, trust_env=True) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(65536):
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise ZhihuError(f"下载产物失败：{str(e)[:160]}") from e
        return dest

    # ------------------------------------------------------------
    # 自检与输出
    # ------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """凭证/连通性自检（只查额度，不消耗业务额度）。"""
        info: Dict[str, Any] = {
            "base_url": self.base_url,
            "secret": mask_secret(self.access_secret),
            "configured": self.configured,
        }
        if not self.configured:
            info["quota"] = "未配置 ZHIHU_ACCESS_SECRET"
            return info
        try:
            info["quota"] = self.quota()
        except Exception as e:      # noqa: BLE001 — 自检不应抛异常
            info["quota"] = f"failed: {str(e)[:300]}"
        info["clock_skew"] = round(self._skew, 1)
        return info

    def summarize(self, data: Any) -> str:
        """把响应压成适合回给模型的文本（超长截断）。"""
        if isinstance(data, str):
            text = data
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > self.max_chars:
            text = text[:self.max_chars] + f"\n…（已截断，原文 {len(text)} 字符）"
        return text


# ------------------------------------------------------------
# 小工具函数
# ------------------------------------------------------------

def _drop_none(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """去掉值为 None 的参数（上游对空串与缺省的处理不同，只滤 None）。"""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = low
    return max(low, min(high, n))


def _require_file(path: str) -> str:
    full = os.path.abspath(os.path.expanduser(str(path or "")))
    if not path or not os.path.isfile(full):
        raise ZhihuError(f"文件不存在：{path}")
    return full


def _safe_name(name: str) -> str:
    keep = "-_.() 一二三四五六七八九十"
    cleaned = "".join(ch if (ch.isalnum() or ch in keep) else "_" for ch in name)
    return cleaned.strip() or "zhihu_download"
