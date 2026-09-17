"""
Toonflow 客户端（外部 AI 短剧工厂的 REST API 封装）。

Toonflow（https://github.com/HBAI-Ltd/Toonflow-app）本身是 Electron 桌面工具，
但它的后端是一个**独立的 Express 服务**（默认 127.0.0.1:10588），把整条流水线
暴露成 169 个 /api 路由 —— 所以 agent 可以完全不碰它的界面，直接编排它。

契约（读源码核实，2026-09-17）：
    登录: POST /api/login/login  {"username","password"}
          → {"data": {"token": "Bearer <jwt>", ...}}（有效期 180 天）
    其余: 带上 `Authorization: <token>`（token 字符串本身已含 "Bearer " 前缀）
    路径: 就是 router.ts 里 app.use("/api/xxx", ...) 的原样路径，无额外前缀
    校验: 各路由用 zod + validateFields，字段缺失/类型错时返回 400 与具体原因
          → 所以"先调一次看报错"是可行的发现手段（错误会原样回传）

⚠️ 它的 API 没有公开文档、路由是代码生成的（router.ts 顶部有 @routes-hash），
版本之间字段可能变。因此这里只做**薄封装**：登录/重试/错误翻译由本模块负责，
业务字段一律交给调用方，不猜。
"""
import json
import threading
from typing import Any, Dict, Optional

from config import TOONFLOW_CONFIG


class ToonflowError(RuntimeError):
    """Toonflow 调用失败（附可行动的说明）。"""


def _is_loopback(base_url: str) -> bool:
    """base_url 是否指向本机（决定是否绕过环境代理）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    except Exception:
        return False


class ToonflowClient:
    """Toonflow REST 客户端（登录态缓存 + 401 自动重登）。"""

    def __init__(
        self,
        base_url: str = None,
        username: str = None,
        password: str = None,
        timeout: float = None,
    ):
        cfg = TOONFLOW_CONFIG
        self.base_url = str(base_url or cfg.get("base_url", "http://127.0.0.1:10588")).rstrip("/")
        self.username = username or cfg.get("username", "admin")
        self.password = password or cfg.get("password", "admin123")
        self.timeout = float(timeout or cfg.get("timeout", 60))
        self.max_chars = int(cfg.get("max_chars", 6000))
        self._token: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------

    def login(self, force: bool = False) -> str:
        """登录并缓存 token（线程安全）。"""
        with self._lock:
            if self._token and not force:
                return self._token
            data = self._request_raw("POST", "/api/login/login",
                                     json_body={"username": self.username,
                                                "password": self.password},
                                     _retry_auth=False)
            payload = data.get("data") if isinstance(data, dict) else None
            token = (payload or {}).get("token") if isinstance(payload, dict) else None
            if not token:
                raise ToonflowError(
                    f"Toonflow 登录成功但未返回 token：{str(data)[:200]}")
            self._token = str(token)
            return self._token

    @property
    def token(self) -> Optional[str]:
        return self._token

    # ------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------

    def request(self, method: str, path: str, params: Dict[str, Any] = None,
                json_body: Any = None) -> Any:
        """发起一次带鉴权的请求；401 时自动重登并重试一次。"""
        self.login()
        return self._request_raw(method, path, params=params, json_body=json_body)

    def _request_raw(self, method: str, path: str, params: Dict[str, Any] = None,
                     json_body: Any = None, _retry_auth: bool = True) -> Any:
        import httpx

        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"
        headers = {}
        if self._token:
            headers["Authorization"] = self._token
        try:
            resp = httpx.request(method.upper(), url, params=params,
                                 json=json_body, headers=headers,
                                 timeout=self.timeout,
                                 # 回环地址**不走环境里的代理**：实测本机没启动
                                 # Toonflow 时，若让 httpx 读环境代理配置，会得到
                                 # 一个莫名其妙的 HTTP 502（本该是"连接被拒绝"），
                                 # 排查方向直接被带偏。远端部署仍允许走代理。
                                 trust_env=not _is_loopback(self.base_url))
        except httpx.HTTPError as e:
            raise ToonflowError(
                f"连不上 Toonflow（{self.base_url}）：{str(e)[:160]}。"
                "请确认 Toonflow 已启动（桌面端打开后其后端就在 10588；"
                "或用 yarn dev / Docker 起服务），必要时改 TOONFLOW_BASE_URL。"
            ) from e

        if resp.status_code == 401 and _retry_auth:
            # token 过期/被重置 → 重新登录一次再试
            self.login(force=True)
            return self._request_raw(method, path, params=params,
                                     json_body=json_body, _retry_auth=False)

        body = self._decode(resp)
        if resp.status_code >= 400:
            raise ToonflowError(self._explain(resp.status_code, path, body))
        return body

    @staticmethod
    def _decode(resp) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _explain(status: int, path: str, body: Any) -> str:
        """把 HTTP 错误翻成模型能照做的说明。"""
        detail = json.dumps(body, ensure_ascii=False)[:400] if not isinstance(body, str) \
            else body[:400]
        if status == 400:
            # zod 校验失败：字段名/类型原因都在 detail 里，直接照它补参数
            return (f"Toonflow 参数校验失败（400，{path}）：{detail}。"
                    "请按提示补齐/修正字段后重试（字段名与类型必须完全匹配）。")
        if status == 401:
            return (f"Toonflow 认证失败（401，{path}）：{detail}。"
                    "请检查 TOONFLOW_USERNAME / TOONFLOW_PASSWORD。")
        if status == 404:
            return (f"Toonflow 路径不存在（404，{path}）：{detail}。"
                    "该版本可能没有这个路由，用 tool 的 routes 命令查看已核实可用的路径。")
        if status == 500:
            return (f"Toonflow 服务端错误（500，{path}）：{detail}。"
                    "常见原因是它自己的模型供应商没配好（设置中心 → 模型服务）。")
        return f"Toonflow 调用失败（HTTP {status}，{path}）：{detail}"

    # ------------------------------------------------------------

    def health(self) -> dict:
        """连通性/账号自检（不触发任何模型调用，不花钱）。"""
        info = {"base_url": self.base_url, "username": self.username}
        try:
            self.login()
            info["login"] = "ok"
        except ToonflowError as e:
            info["login"] = f"failed: {str(e)[:200]}"
            return info
        try:
            info["version"] = self.request("GET", "/api/other/getVersion")
        except ToonflowError as e:
            info["version"] = f"failed: {str(e)[:160]}"
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


#: 已核实存在的主干路由（来自上游 router.ts），供模型按图索骥。
#: 只列主干，不是全部 169 个；未列出的可用 call 命令直接调。
KNOWN_ROUTES = {
    "登录": "POST /api/login/login  {username, password}",
    "版本": "GET  /api/other/getVersion",
    "模型列表": "GET  /api/modelSelect/getModelList",
    "画风列表": "GET  /api/artStyle/getArtStyle",
    "项目列表": "GET  /api/project/getProject",
    "新建项目": ("POST /api/project/addProject  "
             "{projectType, name, intro, type, artStyle, directorManual, "
             "videoRatio, imageModel, videoModel, imageQuality, mode} —— 全为字符串"),
    "导入原文": ("POST /api/novel/addNovel  "
             "{projectId: number, data: [{index, reel, chapter, chapterData}]}"),
    "原文列表": "GET  /api/novel/getNovel",
    "事件图谱": "GET  /api/novel/event/getEvent ｜ POST /api/novel/event/generateEvents",
    "剧本": "GET  /api/script/getScrptApi ｜ POST /api/script/addScript",
    "剧本Agent": "GET  /api/scriptAgent/getPlanData ｜ POST /api/scriptAgent/setPlanData",
    "分镜": "POST /api/production/storyboard/addStoryboard ｜ GET /api/production/storyboard/getStoryboardData",
    "分镜出图": "POST /api/production/storyboard/batchGenerateImage ｜ GET /api/production/storyboard/pollingImage",
    "素材出图": "POST /api/assetsGenerate/batchGenerateImageAssets ｜ GET /api/assets/pollingImageAssets",
    "出片": "POST /api/production/workbench/batchGenerateVideo ｜ GET /api/production/workbench/checkVideoStateList",
    "成片清单": "GET  /api/production/workbench/getVideoList ｜ GET /api/production/workbench/getFileUrl",
    "任务": "GET  /api/task/getTaskApi ｜ GET /api/task/taskDetails",
    "统计": "GET  /api/general/generalStatistics",
    "供应商配置": "GET  /api/setting/vendorConfig/getVendorList",
}
