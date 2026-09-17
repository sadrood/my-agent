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
        # 生成类调用要等分钟级：Toonflow 内部会同步轮询到出图/出片才返回
        # （实测 5 秒视频片段 60s 超时不够，会误报"连不上"）。
        self.gen_timeout = float(cfg.get("gen_timeout", 900))
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
                json_body: Any = None, timeout: float = None) -> Any:
        """发起一次带鉴权的请求；401 时自动重登并重试一次。

        超时按路径自动选择：命中生成类路由用 gen_timeout，其余用普通 timeout。
        """
        self.login()
        return self._request_raw(method, path, params=params, json_body=json_body,
                                 timeout=timeout if timeout is not None
                                 else self._timeout_for(path))

    #: 会触发模型生成、需要长时间等待的路由片段
    _GEN_HINTS = ("generate", "Generate", "batchGenerate", "modelTest", "pollScriptAssets",
                  "pollingImage", "checkVideoState")

    def _timeout_for(self, path: str) -> float:
        return self.gen_timeout if any(h in path for h in self._GEN_HINTS) else self.timeout

    def _request_raw(self, method: str, path: str, params: Dict[str, Any] = None,
                     json_body: Any = None, _retry_auth: bool = True,
                     timeout: float = None) -> Any:
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
                                 timeout=timeout if timeout is not None else self.timeout,
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
                                     json_body=json_body, _retry_auth=False,
                                     timeout=timeout)

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
            # 上游的真实原因通常比状态码有用得多（例如 video_queue_full、无效的令牌）
            hint = ""
            if any(k in detail for k in ("令牌", "key", "API Key", "apiKey", "401", "403")):
                hint = "看提示像是**密钥/供应商没配好**（设置中心 → 模型服务）。"
            elif "queue" in detail.lower() or "队列" in detail:
                hint = "上游队列已满，属**瞬时**状态，稍后重试即可。"
            return f"Toonflow 返回 500（{path}）：{detail}。{hint}"
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

    @staticmethod
    def method_for(path: str) -> str:
        """该路由的正确 HTTP 方法（查权威表；表外按 POST 试）。

        1.1.8 版 169 条路由里 159 条是 POST——**读接口也是 POST + JSON body**，
        用 GET 会得到 404 "API 404 Not Found"，所以方法不能靠猜。
        """
        return method_for(path)

    def summarize(self, data: Any) -> str:
        """把响应压成适合回给模型的文本（超长截断）。"""
        if isinstance(data, str):
            text = data
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > self.max_chars:
            text = text[:self.max_chars] + f"\n…（已截断，原文 {len(text)} 字符）"
        return text


#: 全量路由 → HTTP 方法（从上游 src/router.ts 逐条提取，2026-09-17 核实；
#: 1.1.8 版共 169 条：159 个 POST / 10 个 GET）。读接口也是 POST + JSON body，
#: 用 GET 调它们会得到 404 "API 404 Not Found"——call 命令据此自动选方法。
ROUTE_METHODS: Dict[str, str] = {
    "/api/agents/clearMemory": "POST",
    "/api/agents/getMemory": "POST",
    "/api/artStyle/addArtStyle": "POST",
    "/api/artStyle/editArtStyle": "POST",
    "/api/artStyle/extractStylePrompt": "POST",
    "/api/artStyle/getArtStyle": "POST",
    "/api/assets/addAssets": "POST",
    "/api/assets/addAudioAssets": "POST",
    "/api/assets/batchDelete": "POST",
    "/api/assets/batchGenerationData": "POST",
    "/api/assets/delAssets": "POST",
    "/api/assets/delImage": "POST",
    "/api/assets/getAssetsApi": "POST",
    "/api/assets/getImage": "POST",
    "/api/assets/getMaterialData": "POST",
    "/api/assets/pollingImageAssets": "POST",
    "/api/assets/pollingPromptAssets": "POST",
    "/api/assets/saveAssets": "POST",
    "/api/assets/updateAssets": "POST",
    "/api/assets/updateAudioAssets": "POST",
    "/api/assets/uploadClip": "POST",
    "/api/assetsGenerate/batchGenerateImageAssets": "POST",
    "/api/assetsGenerate/batchPolishAssetsPrompt": "POST",
    "/api/assetsGenerate/cancelGenerate": "POST",
    "/api/assetsGenerate/generateAssets": "POST",
    "/api/assetsGenerate/polishAssetsPrompt": "POST",
    "/api/common/getBigImage": "POST",
    "/api/cornerScape/batchBindAudio": "POST",
    "/api/cornerScape/getAllAssets": "POST",
    "/api/cornerScape/pollingAudio": "POST",
    "/api/cornerScape/updateAssetsAudio": "POST",
    "/api/general/generalStatistics": "POST",
    "/api/general/getSingleProject": "POST",
    "/api/general/updateProject": "POST",
    "/api/login/login": "POST",
    "/api/modelSelect/getModelDetail": "POST",
    "/api/modelSelect/getModelList": "POST",
    "/api/novel/addNovel": "POST",
    "/api/novel/batchDeleteNovel": "POST",
    "/api/novel/delNovel": "POST",
    "/api/novel/event/batchDeleteEvent": "POST",
    "/api/novel/event/deletEvent": "POST",
    "/api/novel/event/generateEvents": "POST",
    "/api/novel/event/getEvent": "POST",
    "/api/novel/getNovel": "POST",
    "/api/novel/getNovelData": "POST",
    "/api/novel/getNovelEventState": "POST",
    "/api/novel/getNovelIndex": "POST",
    "/api/novel/updateNovel": "POST",
    "/api/other/deleteAllData": "POST",
    "/api/other/getVersion": "GET",
    "/api/production/assets/batchGenerateAssetsImage": "POST",
    "/api/production/assets/deleteAssetsDireve": "POST",
    "/api/production/assets/pollingImage": "POST",
    "/api/production/assets/updateAssetsUrl": "POST",
    "/api/production/editImage/generateFlowImage": "POST",
    "/api/production/editImage/getImageDefaultModle": "POST",
    "/api/production/editImage/getImageFlow": "POST",
    "/api/production/editImage/saveImageFlow": "POST",
    "/api/production/editImage/updateImageFlow": "POST",
    "/api/production/editImage/uploadImage": "POST",
    "/api/production/getFlowData": "POST",
    "/api/production/getStoryboardData": "POST",
    "/api/production/saveFlowData": "POST",
    "/api/production/storyboard/addStoryboard": "POST",
    "/api/production/storyboard/batchAddStoryboardInfo": "POST",
    "/api/production/storyboard/batchDelete": "POST",
    "/api/production/storyboard/batchGenerateImage": "POST",
    "/api/production/storyboard/downPreviewImage": "POST",
    "/api/production/storyboard/editStoryboardInfo": "POST",
    "/api/production/storyboard/getStoryboardData": "POST",
    "/api/production/storyboard/pollingImage": "POST",
    "/api/production/storyboard/previewImage": "POST",
    "/api/production/storyboard/removeFrame": "POST",
    "/api/production/storyboard/updateStoryboardUrl": "POST",
    "/api/production/workbench/addTrack": "POST",
    "/api/production/workbench/batchGeneratePrompt": "POST",
    "/api/production/workbench/batchGenerateVideo": "POST",
    "/api/production/workbench/checkVideoPrompt": "POST",
    "/api/production/workbench/checkVideoStateList": "POST",
    "/api/production/workbench/delVideo": "POST",
    "/api/production/workbench/deleteTrack": "POST",
    "/api/production/workbench/generateVideo": "POST",
    "/api/production/workbench/generateVideoPrompt": "POST",
    "/api/production/workbench/getAudioBindAssetsList": "POST",
    "/api/production/workbench/getFileUrl": "POST",
    "/api/production/workbench/getGenerateData": "POST",
    "/api/production/workbench/getVideoList": "POST",
    "/api/production/workbench/selectVideo": "POST",
    "/api/production/workbench/updateVideoDuration": "POST",
    "/api/production/workbench/updateVideoPrompt": "POST",
    "/api/project/addDirectorManual": "POST",
    "/api/project/addProject": "POST",
    "/api/project/addVisualManual": "POST",
    "/api/project/delProject": "POST",
    "/api/project/deleteDirectorManual": "POST",
    "/api/project/deleteVisualManual": "POST",
    "/api/project/editDirectorlManual": "POST",
    "/api/project/editProject": "POST",
    "/api/project/editVisualManual": "POST",
    "/api/project/getModelDetails": "POST",
    "/api/project/getProject": "POST",
    "/api/project/getVisualManual": "POST",
    "/api/project/queryDirectorManual": "POST",
    "/api/project/visualManual": "POST",
    "/api/script/addScript": "POST",
    "/api/script/batchAddScript": "POST",
    "/api/script/delScript": "POST",
    "/api/script/exportScript": "POST",
    "/api/script/extractAssets": "POST",
    "/api/script/getAiRegex": "POST",
    "/api/script/getScrptApi": "POST",
    "/api/script/pollScriptAssets": "POST",
    "/api/script/updateScript": "POST",
    "/api/scriptAgent/getPlanData": "POST",
    "/api/scriptAgent/setPlanData": "POST",
    "/api/scriptAgent/updateData": "POST",
    "/api/setting/about/checkUpdate": "POST",
    "/api/setting/about/downloadApp": "POST",
    "/api/setting/agentDeploy/agentSetKey": "POST",
    "/api/setting/agentDeploy/deployAgentModel": "POST",
    "/api/setting/agentDeploy/getAgentDeploy": "POST",
    "/api/setting/agentDeploy/getAgentUseMode": "GET",
    "/api/setting/agentDeploy/updateAgentModel": "POST",
    "/api/setting/agentDeploy/updateUseMode": "POST",
    "/api/setting/dbConfig/clearData": "GET",
    "/api/setting/dbConfig/clearTable": "POST",
    "/api/setting/dbConfig/dbInfo": "GET",
    "/api/setting/dbConfig/exportData": "GET",
    "/api/setting/dbConfig/importData": "POST",
    "/api/setting/dev/getSwitchAiDevTool": "GET",
    "/api/setting/dev/updateSwitchAiDevTool": "POST",
    "/api/setting/fileManagement/openFolder": "POST",
    "/api/setting/getTextModel": "POST",
    "/api/setting/loginConfig/getUser": "GET",
    "/api/setting/loginConfig/updateUserPwd": "POST",
    "/api/setting/memoryConfig/delAllMemory": "POST",
    "/api/setting/memoryConfig/getMemory": "GET",
    "/api/setting/memoryConfig/sureMemory": "POST",
    "/api/setting/modelMap/bindingPrompt": "POST",
    "/api/setting/modelMap/deletePrompt": "POST",
    "/api/setting/modelMap/getImageAndVideoModel": "POST",
    "/api/setting/modelMap/getPromptList": "GET",
    "/api/setting/modelMap/savePrompt": "POST",
    "/api/setting/modelMap/updatePrompt": "POST",
    "/api/setting/promptManage/getPrompt": "POST",
    "/api/setting/promptManage/updatePrompt": "POST",
    "/api/setting/skillManagement/getSkillContent": "POST",
    "/api/setting/skillManagement/getSkillList": "POST",
    "/api/setting/skillManagement/saveSkillContent": "POST",
    "/api/setting/vendorConfig/addVendor": "POST",
    "/api/setting/vendorConfig/addVendorModel": "POST",
    "/api/setting/vendorConfig/delVendorModel": "POST",
    "/api/setting/vendorConfig/deleteVendor": "POST",
    "/api/setting/vendorConfig/enableVendor": "POST",
    "/api/setting/vendorConfig/getCodeByLink": "POST",
    "/api/setting/vendorConfig/getVendorList": "POST",
    "/api/setting/vendorConfig/modelTest": "POST",
    "/api/setting/vendorConfig/modelTest/imageTest": "POST",
    "/api/setting/vendorConfig/modelTest/textTest": "POST",
    "/api/setting/vendorConfig/modelTest/videoTest": "POST",
    "/api/setting/vendorConfig/upVendorModel": "POST",
    "/api/setting/vendorConfig/updateCode": "POST",
    "/api/setting/vendorConfig/updateVendorInputs": "POST",
    "/api/task/getProject": "POST",
    "/api/task/getTaskApi": "POST",
    "/api/task/getTaskCategories": "POST",
    "/api/task/taskDetails": "POST",
    "/api/test/test": "GET",
}


def method_for(path: str) -> str:
    """按路由表给出正确方法；表里没有的路径按 POST 试（上游读接口也多是 POST）。"""
    p = "/" + str(path or "").lstrip("/")
    return ROUTE_METHODS.get(p, "POST")


#: 主干流水线路由（中文标签便于模型按图索骥；方法已按上游源码核实）
KNOWN_ROUTES = {
    "登录": "POST /api/login/login  {username, password}",
    "版本": "GET  /api/other/getVersion",
    "模型列表": "POST /api/modelSelect/getModelList  {type: all|text|image|video}",
    "画风列表": "POST /api/artStyle/getArtStyle  {}",
    "项目列表": "POST /api/project/getProject  {}",
    "新建项目": ("POST /api/project/addProject  {projectType, name, intro, type, "
             "artStyle, directorManual, videoRatio, imageModel, videoModel, "
             "imageQuality, mode}（全部字符串）"),
    "导入原文": "POST /api/novel/addNovel  {projectId, data:[{index,reel,chapter,chapterData}]}",
    "原文列表": "POST /api/novel/getNovel  {projectId, page, limit}",
    "事件图谱": "POST /api/novel/event/getEvent  {projectId, page, limit}",
    "生成事件": "POST /api/novel/event/generateEvents  {projectId}",
    "剧本列表": "POST /api/script/getScrptApi  {projectId, name?}",
    "剧本新增": "POST /api/script/addScript  {projectId, ...}",
    "剧本Agent": "POST /api/scriptAgent/getPlanData ｜ setPlanData",
    "分镜数据": "POST /api/production/storyboard/getStoryboardData  {scriptId, page, limit}",
    "分镜出图": ("POST /api/production/storyboard/batchGenerateImage  "
              "{storyboardIds:[number], projectId, scriptId, concurrentCount?, compulsory?}"),
    "出图轮询": "POST /api/production/storyboard/pollingImage  {ids:[number]}",
    "出片": ("POST /api/production/workbench/batchGenerateVideo  "
           "{projectId, scriptId, trackData:[...]}（trackData 结构见上游）"),
    "出片轮询": ("POST /api/production/workbench/checkVideoStateList  "
             "{projectId, scriptId, videoIds:[number]}"),
    "成片清单": "POST /api/production/workbench/getVideoList  {projectId, scriptId}",
    "取文件地址": "POST /api/production/workbench/getFileUrl  {projectId, scriptId, ...}",
    "任务列表": "POST /api/task/getTaskApi  {page, limit, state?, taskClass?, projectId?}",
    "任务详情": "POST /api/task/taskDetails  {id}",
    "统计": "POST /api/general/generalStatistics  {}",
    "供应商列表": "POST /api/setting/vendorConfig/getVendorList  {}",
}
