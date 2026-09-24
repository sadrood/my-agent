"""
短剧工厂服务工具（短剧工具类）：让 agent 通过 REST API 驱动外部短剧工厂。

为什么是 API 而不是点界面：短剧工厂服务的后端是独立 Express 服务
（默认 127.0.0.1:10588），169 个 /api 路由覆盖
原文 → 事件图谱 → 剧本 → 分镜 → 出图 → 出片 全流程，
登录一次拿 JWT 即可无人值守编排；用 computer 工具点 GUI 既慢又脆。

⚠️ 它的 API 无公开文档、路由是代码生成的，版本间字段可能变。所以：
- 主干步骤给了具名命令（字段已按上游源码核实）；
- 其余一切走 `call` 命令直连任意路径（method/path/query/body）；
- 参数不全时上游 zod 校验会返回 400 + 具体字段，错误原样回传，照它补即可。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

#: 需要写/改数据的命令（审批元数据用）
_WRITE_COMMANDS = ("create_project", "add_novel", "call_write")


class ToonflowTool(BaseTool):
    """短剧工厂服务对接工具。"""

    risk_level: str = "medium"          # 能建项目/导入原文/触发付费生成
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"  # 本身只发 HTTP，不写本地文件
    parallel_safe: bool = False          # 登录态是共享资源，串行更稳

    def __init__(self, client=None):
        self._client = client

    @property
    def name(self) -> str:
        return "toonflow"

    @property
    def description(self) -> str:
        from config import TOONFLOW_CONFIG
        from models.toonflow import KNOWN_ROUTES
        base = TOONFLOW_CONFIG.get("base_url", "http://127.0.0.1:10588")
        routes = "\n".join(f"    {k}: {v}" for k, v in list(KNOWN_ROUTES.items())[:10])
        return (
            "Toonflow 短剧工厂工具（外部服务：" + base + "）。\n"
            "把小说/剧本变成动画短剧的完整流水线，用 REST API 无人值守驱动：\n"
            "  原文 → 章节事件图谱 → 剧本(含 ScriptAgent) → 分镜 → 出图 → 出片\n"
            "命令：\n"
            "  health         连通性与登录自检（不触发模型调用，不花钱）\n"
            "  models         可用模型列表（建项目时填 imageModel/videoModel 用）\n"
            "  styles         可用画风列表（建项目时填 artStyle 用）\n"
            "  projects       项目列表\n"
            "  create_project 新建项目（name 必填，其余可选，见 schema）\n"
            "  add_novel      导入原文（project_id + text 或 data）\n"
            "  events         章节事件图谱（project_id）\n"
            "  scripts        剧本列表（project_id）→ 拿 script_id 才能查分镜/出片\n"
            "  storyboard     分镜数据（script_id）\n"
            "  videos         成片清单（project_id + script_id）\n"
            "  generate_images 批量出图（storyboard_ids + project_id + script_id）\n"
            "  image_state    出图进度（ids）\n"
            "  generate_video 批量出片（project_id + script_id + track_data）\n"
            "  video_state    出片进度（project_id + script_id + video_ids）\n"
            "  tasks          任务队列（page/limit，可按 project_id 过滤）\n"
            "  vendor         供应商配置（密钥已掩码）\n"
            "  add_track      新建出片轨道（project_id + script_id）→ track_id\n"
            "  gen_prompts    为轨道生成视频提示词（track_data + model + mode）\n"
            "  gen_videos     批量出片（project_id + script_id + track_data + model + mode + resolution）\n"
            "  workbench      出片工作台数据（分镜/轨道/视频当前状态）\n"
            "  file_url       取素材/成片文件地址（items）\n"
            "  出片标准流程：add_track → gen_prompts → gen_videos → video_state → file_url\n"
            "  call           通用调用：任意 method/path/query/body（覆盖其余 100+ 路由）\n"
            "已核实的主干路由：\n" + routes + "\n"
            "提示：参数不全时上游会返回 400 并说明缺哪个字段，照提示补齐即可；\n"
            "首次使用先跑 health 确认它已启动（桌面端打开后其后端就在 10588）。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["health", "models", "styles", "projects", "novels",
                             "create_project", "add_novel", "events", "scripts",
                             "storyboard", "videos", "generate_images", "image_state",
                             "generate_video", "video_state", "tasks", "vendor",
                             "add_track", "gen_prompts", "workbench", "file_url",
                             "call"],
                    "description": "要执行的命令",
                },
                "project_id": {
                    "type": "number",
                    "description": "项目 ID（add_novel / novels / events / videos 用）",
                },
                "script_id": {
                    "type": "number",
                    "description": "剧本 ID（storyboard / videos 用；Storyboard 按剧本取，不是按项目）",
                },
                "type": {
                    "type": "string",
                    "enum": ["all", "text", "image", "video"],
                    "description": "models 命令的筛选：all/text/image/video",
                },
                "name": {"type": "string", "description": "项目名（create_project）"},
                "intro": {"type": "string", "description": "项目简介（create_project）"},
                "art_style": {"type": "string",
                              "description": "画风 ID（create_project；先用 styles 查）"},
                "image_model": {"type": "string",
                                "description": "图片模型 ID（create_project；先用 models 查）"},
                "video_model": {"type": "string",
                                "description": "视频模型 ID（create_project；先用 models 查）"},
                "video_ratio": {"type": "string",
                                "description": "画幅比例，如 9:16 / 16:9"},
                "text": {"type": "string",
                         "description": "原文全文（add_novel 便捷入口，自动切成章节）"},
                "data": {
                    "type": "array",
                    "description": "原文章节数组（add_novel 直连上游格式）",
                    "items": {"type": "object"},
                },
                "storyboard_ids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "generate_images 用：要出图的分镜 ID 列表",
                },
                "ids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "image_state 用：分镜 ID 列表",
                },
                "video_ids": {
                    "type": "array", "items": {"type": "number"},
                    "description": "video_state 用：出片任务 ID 列表",
                },
                "track_data": {
                    "type": "array", "items": {"type": "object"},
                    "description": "generate_video 用：上游 trackData 数组",
                },
                "page": {"type": "number", "description": "tasks 用：页码（默认 1）"},
                "limit": {"type": "number", "description": "tasks 用：每页条数（默认 20）"},
                "concurrent_count": {
                    "type": "number", "description": "generate_images 用：并发数（默认 5）",
                },
                "model": {
                    "type": "string",
                    "description": "gen_prompts / gen_videos 用：模型，如 agnes:agnes-video-2.5-flash",
                },
                "mode": {
                    "type": "string",
                    "description": "gen_prompts / gen_videos 用：生成模式（agnes 视频模型只支持 text）",
                },
                "resolution": {
                    "type": "string", "description": "gen_videos 用：分辨率，如 720P",
                },
                "duration": {
                    # 只有 add_track 读它；描述写错会让模型给 gen_videos 传 duration
                    # 而被静默忽略，按错误时长出片并计费。
                    "type": "number", "description": "add_track 用：单镜时长（秒，4-12）",
                },
                "audio": {
                    "type": "boolean", "description": "gen_videos 用：是否带音频（默认 false）",
                },
                "items": {
                    "type": "array", "items": {"type": "object"},
                    "description": "file_url 用：[{id, sources}]（sources: storyboard/assets）",
                },
                "method": {"type": "string",
                           "description": "call 用：GET / POST / PUT / DELETE"},
                "path": {"type": "string",
                         "description": "call 用：完整路径，如 /api/project/getProject"},
                "query": {"type": "object", "description": "call 用：URL 查询参数"},
                "body": {"type": "object", "description": "call 用：JSON 请求体"},
            },
            "required": ["command"],
        }

    # ------------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client
        from models.toonflow import ToonflowClient
        self._client = ToonflowClient()
        return self._client

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        cmd = str(arguments.get("command") or "health").strip().lower()
        try:
            client = self._get_client()
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"Toonflow 客户端初始化失败: {str(e)[:200]}")

        from models.toonflow import ToonflowError

        try:
            # 该服务的读接口几乎全是 POST + JSON body（GET 会 404），
            # 且 Storyboard 用 scriptId、VideoList 要 projectId+scriptId。
            handler = {
                "health": self._health,
                "models": lambda c, a: self._read(c, "/api/modelSelect/getModelList",
                                                  {"type": str(a.get("type") or "all")}),
                "styles": lambda c, a: self._read(c, "/api/artStyle/getArtStyle", {}),
                "projects": lambda c, a: self._read(c, "/api/project/getProject", {}),
                "novels": lambda c, a: self._read(c, "/api/novel/getNovel",
                                                  {"projectId": a.get("project_id"),
                                                   "page": 1, "limit": 50}),
                "events": lambda c, a: self._read(c, "/api/novel/event/getEvent",
                                                  {"projectId": a.get("project_id"),
                                                   "page": 1, "limit": 50}),
                "storyboard": lambda c, a: self._read(
                    c, "/api/production/storyboard/getStoryboardData",
                    {"scriptId": a.get("script_id"), "page": 1, "limit": 50}),
                "videos": lambda c, a: self._read(
                    c, "/api/production/workbench/getVideoList",
                    {"projectId": a.get("project_id"), "scriptId": a.get("script_id")}),
                "scripts": lambda c, a: self._read(c, "/api/script/getScrptApi",
                                                   {"projectId": a.get("project_id")}),
                "generate_images": self._generate_images,
                "image_state": lambda c, a: self._read(
                    c, "/api/production/storyboard/pollingImage",
                    {"ids": a.get("ids") or a.get("storyboard_ids") or []}),
                "generate_video": self._generate_video,
                "video_state": lambda c, a: self._read(
                    c, "/api/production/workbench/checkVideoStateList",
                    {"projectId": a.get("project_id"), "scriptId": a.get("script_id"),
                     "videoIds": a.get("video_ids") or []}),
                "tasks": lambda c, a: self._read(
                    c, "/api/task/getTaskApi",
                    {"page": int(a.get("page") or 1), "limit": int(a.get("limit") or 20)}),
                "vendor": self._vendor,
                "add_track": self._add_track,
                "gen_prompts": self._gen_prompts,
                "workbench": lambda c, a: self._read(
                    c, "/api/production/workbench/getGenerateData",
                    {"projectId": a.get("project_id"), "scriptId": a.get("script_id")}),
                "file_url": lambda c, a: self._read(
                    c, "/api/production/workbench/getFileUrl",
                    {"items": a.get("items") or []}),
                "create_project": self._create_project,
                "add_novel": self._add_novel,
                "call": self._call,
            }.get(cmd)
            if handler is None:
                return ToolResult(
                    success=False, output="",
                    error=f"未知命令: {cmd}（可用: health/models/styles/projects/novels/"
                          "create_project/add_novel/events/storyboard/videos/call）")
            return handler(client, arguments)
        except ToonflowError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:      # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"Toonflow 调用异常: {str(e)[:300]}")

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`<命令> [参数]`。"""
        text = (input_str or "").strip()
        if not text:
            return self.execute_json({"command": "health"})
        parts = text.split(maxsplit=1)
        args: Dict[str, Any] = {"command": parts[0].lower()}
        rest = parts[1].strip() if len(parts) > 1 else ""
        if rest:
            # 位置参数：health/projects 之类忽略；其余按名称/id 传入
            if args["command"] in ("create_project",):
                args["name"] = rest
            elif args["command"] in ("storyboard",) and rest.isdigit():
                args["script_id"] = int(rest)
            elif rest.isdigit():
                args["project_id"] = int(rest)
            else:
                args["text"] = rest
        return self.execute_json(args)

    # ------------------------------------------------------------
    # 具体命令
    # ------------------------------------------------------------

    def _health(self, client, args) -> ToolResult:
        info = client.health()
        if info.get("login") != "ok":
            return ToolResult(
                success=False, output="",
                error=f"Toonflow 不可用：{info.get('login')}\n"
                      "排查：① Toonflow 是否已启动（桌面端打开后其后端在 "
                      f"{info.get('base_url')}）；② 账号密码是否正确"
                      "（TOONFLOW_USERNAME / TOONFLOW_PASSWORD）。")
        lines = [f"✅ Toonflow 已连通：{info['base_url']}",
                 f"账号: {info['username']}",
                 f"版本: {client.summarize(info.get('version'))}"]
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"base_url": info["base_url"]})

    def _read(self, client, path: str, body: dict = None) -> ToolResult:
        """读接口：**POST + JSON body**（该服务的读接口不用 GET/query）。"""
        data = client.request("POST", path, json_body=body if body is not None else {})
        return ToolResult(success=True, output=client.summarize(data),
                          metadata={"path": path})

    def _create_project(self, client, args) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(
                success=False, output="",
                error="create_project 需要 name。建议先用 styles / models 拿到 "
                      "art_style 与 image_model / video_model 的 ID 再建项目。")
        # 字段名严格对齐上游 zod 校验（全部 string）
        body = {
            "projectType": str(args.get("project_type") or "1"),
            "name": name,
            "intro": str(args.get("intro") or ""),
            "type": str(args.get("type") or "1"),
            "artStyle": str(args.get("art_style") or ""),
            "directorManual": str(args.get("director_manual") or ""),
            "videoRatio": str(args.get("video_ratio") or "9:16"),
            "imageModel": str(args.get("image_model") or ""),
            "videoModel": str(args.get("video_model") or ""),
            "imageQuality": str(args.get("image_quality") or "1"),
            "mode": str(args.get("mode") or "1"),
        }
        data = client.request("POST", "/api/project/addProject", json_body=body)
        return ToolResult(success=True,
                          output=f"已创建项目「{name}」：{client.summarize(data)}\n"
                                 "（下一步：add_novel 导入原文，再 events 生成事件图谱）",
                          metadata={"body": body})

    def _add_novel(self, client, args) -> ToolResult:
        pid = args.get("project_id")
        if pid is None:
            return ToolResult(success=False, output="",
                              error="add_novel 需要 project_id（先用 projects 查项目 ID）")
        data = args.get("data")
        if not data:
            text = str(args.get("text") or "").strip()
            if not text:
                return ToolResult(
                    success=False, output="",
                    error="add_novel 需要 text（原文全文）或 data（章节数组）。")
            # 便捷入口：整段文本按空行/标题切成章节，避免调用方手工构造数组
            chapters = _split_chapters(text)
            data = [{"index": i + 1, "reel": "", "chapter": c["title"],
                     "chapterData": c["body"]} for i, c in enumerate(chapters)]
        body = {"projectId": int(pid), "data": data}
        resp = client.request("POST", "/api/novel/addNovel", json_body=body)
        return ToolResult(
            success=True,
            output=f"已导入 {len(data)} 个章节到项目 {pid}：{client.summarize(resp)}\n"
                   "（下一步：events 查看/触发章节事件图谱）",
            metadata={"chapters": len(data)})

    def _generate_images(self, client, args) -> ToolResult:
        """批量出图（上游字段：storyboardIds + projectId + scriptId）。"""
        ids = args.get("storyboard_ids") or args.get("ids")
        if not ids or args.get("project_id") is None or args.get("script_id") is None:
            return ToolResult(
                success=False, output="",
                error="generate_images 需要 storyboard_ids + project_id + script_id"
                      "（先用 storyboard 拿到分镜 ID）。")
        body = {"storyboardIds": [int(i) for i in ids],
                "projectId": int(args["project_id"]), "scriptId": int(args["script_id"])}
        if args.get("concurrent_count"):
            body["concurrentCount"] = int(args["concurrent_count"])
        data = client.request("POST", "/api/production/storyboard/batchGenerateImage",
                              json_body=body)
        return ToolResult(
            success=True,
            output="已提交 " + str(len(body["storyboardIds"])) + " 个分镜的出图任务：" + chr(10)
                   + client.summarize(data) + chr(10)
                   + "（用 image_state + ids 查进度；出图会消耗额度）",
            metadata={"storyboard_ids": body["storyboardIds"]})

    def _add_track(self, client, args) -> ToolResult:
        """新建出片轨道（免费，仅写库）：gen_prompts / gen_videos 都挂在它上面。"""
        if args.get("project_id") is None or args.get("script_id") is None:
            return ToolResult(success=False, output="",
                              error="add_track 需要 project_id + script_id。")
        body = {"projectId": int(args["project_id"]), "scriptId": int(args["script_id"])}
        if args.get("duration"):
            body["duration"] = float(args["duration"])
        data = client.request("POST", "/api/production/workbench/addTrack", json_body=body)
        track_id = (data or {}).get("data") if isinstance(data, dict) else None
        tip = ("已新建出片轨道 track_id=" + str(track_id)
               + "。下一步 gen_prompts：track_data=[{trackId: <id>, "
                 "info: [{id: <分镜ID>, sources: storyboard}]}]")
        return ToolResult(success=True, output=tip,
                          metadata={"track_id": track_id, "body": body})

    def _gen_prompts(self, client, args) -> ToolResult:
        """为轨道生成视频提示词（走文本模型，消耗文本额度）。"""
        if args.get("project_id") is None or not args.get("track_data"):
            return ToolResult(
                success=False, output="",
                error="gen_prompts 需要 project_id + track_data"
                      "（track_data=[{trackId, info:[{id, sources}]}]）+ model + mode。")
        body = {"projectId": int(args["project_id"]), "trackData": args["track_data"],
                "model": str(args.get("model") or "agnes:agnes-video-2.5-flash"),
                "mode": str(args.get("mode") or "text")}
        if args.get("concurrent_count"):
            body["concurrentCount"] = int(args["concurrent_count"])
        data = client.request("POST", "/api/production/workbench/batchGeneratePrompt",
                              json_body=body, timeout=client.gen_timeout)
        return ToolResult(success=True,
                          output="已提交提示词生成：" + chr(10) + client.summarize(data),
                          metadata={"track_data": body["trackData"]})

    def _generate_video(self, client, args) -> ToolResult:
        """批量出片（上游字段：projectId + scriptId + trackData）。"""
        if (args.get("project_id") is None or args.get("script_id") is None
                or not args.get("track_data")):
            return ToolResult(
                success=False, output="",
                error="generate_video 需要 project_id + script_id + track_data"
                      "（track_data 结构见上游 batchGenerateVideo 的 zod schema；"
                      "可用 call 查 getGenerateData 拿模板）。")
        body = {"projectId": int(args["project_id"]), "scriptId": int(args["script_id"]),
                "trackData": args["track_data"],
                "model": str(args.get("model") or "agnes:agnes-video-2.5-flash"),
                "mode": str(args.get("mode") or "text"),
                "resolution": str(args.get("resolution") or "720P"),
                "audio": bool(args.get("audio"))}
        data = client.request("POST", "/api/production/workbench/batchGenerateVideo",
                              json_body=body, timeout=client.gen_timeout)
        return ToolResult(success=True,
                          output="已提交出片任务：" + chr(10) + client.summarize(data) + chr(10)
                                 + "（用 video_state + video_ids 查进度）",
                          metadata={"project_id": body["projectId"]})

    def _vendor(self, client, args) -> ToolResult:
        """供应商配置（密钥掩码后回给模型，避免 key 进上下文/日志）。"""
        data = client.request("POST", "/api/setting/vendorConfig/getVendorList",
                              json_body={})
        safe = _mask_secrets(data)
        return ToolResult(success=True, output=client.summarize(safe),
                          metadata={"masked": True})

    def _call(self, client, args) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(
                success=False, output="",
                error="call 需要 path，例如 /api/project/getProject 或 "
                      "/api/production/workbench/getVideoList")
        # 方法：显式指定优先；否则查权威路由表（1.1.8 里 159/169 是 POST，读接口也是 POST）
        method = str(args.get("method") or "").upper() or client.method_for(path)
        query = args.get("query") or None
        body = args.get("body")
        if body is None and method == "POST":
            body = {}      # 上游 zod 要求 body 必须是 object（缺省会报"期望 object，实际 undefined"）
        data = client.request(method, path, params=query, json_body=body)
        return ToolResult(success=True,
                          output=f"{method} {path} →\n{client.summarize(data)}",
                          metadata={"path": path, "method": method})

    # ------------------------------------------------------------

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest

        cmd = str(arguments.get("command") or "").lower()
        is_write = cmd in ("create_project", "add_novel", "generate_images",
                           "generate_video", "add_track", "gen_prompts") or (
            cmd == "call" and str(arguments.get("method") or "").upper() not in ("", "GET"))
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"toonflow {cmd}" + (f" {arguments.get('path')}" if arguments.get("path") else ""),
            risk_level="medium" if is_write else "low",
            min_sandbox_mode="read-only",
        )


#: 响应里需要掩码的字段（供应商配置会带回 apiKey/ak/sk 等）
_SECRET_FIELDS = ("apikey", "api_key", "key", "secret", "token", "accesskey", "ak", "sk")


def _mask_secrets(obj, _depth: int = 0):
    """递归掩码密钥字段：只留前后各 4 位，避免密钥进入模型上下文与日志。"""
    if _depth > 6:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(t in str(k).lower() for t in _SECRET_FIELDS) and isinstance(v, str) and v:
                out[k] = (v[:4] + "***" + v[-4:]) if len(v) > 12 else "***"
            else:
                out[k] = _mask_secrets(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [_mask_secrets(v, _depth + 1) for v in obj]
    return obj


def _split_chapters(text: str, max_chars: int = 4000) -> list:
    """把原文切成章节：[{title, body}]。

    优先按"第N章/第N节/Chapter N"这类标题行切；没有标题就按空行聚合，
    再按 max_chars 硬切，保证每章都能过上游（它的 cleanNovel 要逐章处理事件）。
    """
    import re

    lines = text.splitlines()
    # 标题行：第N章/节/回（后面可跟短标题）、Chapter N、Markdown 标题。
    # 注意"第N章"后面**允许跟标题文字**（"第一章 雨夜"），否则真实文本切不开；
    # 同时把尾部长度限住，避免把以"第X章"开头的正文句子误判成标题。
    title_pat = re.compile(
        r"^\s*(?:"
        r"第\s*[0-9一二三四五六七八九十百零]+\s*[章节回](?:\s*[：:、.\-—]?\s*\S.{0,38})?"
        r"|Chapter\s+\d+(?:\s*[：:、.\-—]?\s*.{0,38})?"
        r"|#{1,6}\s+.+"
        r")\s*$",
        re.IGNORECASE)
    chapters = []
    cur_title, cur_body = "", []

    def flush():
        body = "\n".join(cur_body).strip()
        if body or cur_title:
            chapters.append({"title": cur_title or f"第{len(chapters) + 1}节",
                             "body": body})

    for line in lines:
        if title_pat.match(line):
            flush()
            cur_title, cur_body = line.strip().lstrip("#").strip(), []
        else:
            cur_body.append(line)
    flush()

    if not chapters:
        chapters = [{"title": "第1节", "body": text}]

    # 超长章节硬切（保持顺序）
    out = []
    for ch in chapters:
        body = ch["body"]
        if len(body) <= max_chars:
            out.append(ch)
            continue
        for i in range(0, len(body), max_chars):
            part = body[i:i + max_chars]
            out.append({"title": ch["title"] + (f"（{i // max_chars + 1}）" if i else ""),
                        "body": part})
    return out
