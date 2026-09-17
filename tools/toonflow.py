"""
Toonflow 工具（ToonflowTool）：让 agent 通过 REST API 驱动外部短剧工厂。

为什么是 API 而不是点界面：Toonflow 的后端是独立 Express 服务
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
    """Toonflow 短剧工厂对接工具。"""

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
            "  storyboard     分镜数据（project_id）\n"
            "  videos         成片清单（project_id）\n"
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
                    "enum": ["health", "models", "styles", "projects",
                             "create_project", "add_novel", "events",
                             "storyboard", "videos", "call"],
                    "description": "要执行的命令",
                },
                "project_id": {
                    "type": "number",
                    "description": "项目 ID（add_novel / events / storyboard / videos 用）",
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
            handler = {
                "health": self._health,
                "models": lambda c, a: self._get(c, "/api/modelSelect/getModelList"),
                "styles": lambda c, a: self._get(c, "/api/artStyle/getArtStyle"),
                "projects": lambda c, a: self._get(c, "/api/project/getProject"),
                "events": lambda c, a: self._get(c, "/api/novel/event/getEvent",
                                                  {"projectId": a.get("project_id")}),
                "storyboard": lambda c, a: self._get(c, "/api/production/storyboard/getStoryboardData",
                                                     {"projectId": a.get("project_id")}),
                "videos": lambda c, a: self._get(c, "/api/production/workbench/getVideoList",
                                                 {"projectId": a.get("project_id")}),
                "create_project": self._create_project,
                "add_novel": self._add_novel,
                "call": self._call,
            }.get(cmd)
            if handler is None:
                return ToolResult(
                    success=False, output="",
                    error=f"未知命令: {cmd}（可用: health/models/styles/projects/"
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

    def _get(self, client, path: str, params: dict = None) -> ToolResult:
        data = client.request("GET", path, params={k: v for k, v in (params or {}).items()
                                                   if v is not None} or None)
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

    def _call(self, client, args) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return ToolResult(
                success=False, output="",
                error="call 需要 path，例如 /api/project/getProject 或 "
                      "/api/production/workbench/getVideoList")
        method = str(args.get("method") or "GET").upper()
        query = args.get("query") or None
        body = args.get("body") or None
        data = client.request(method, path, params=query, json_body=body)
        return ToolResult(success=True,
                          output=f"{method} {path} →\n{client.summarize(data)}",
                          metadata={"path": path, "method": method})

    # ------------------------------------------------------------

    def build_approval_request(self, arguments: Dict[str, Any]):
        from agent.approval import ApprovalRequest

        cmd = str(arguments.get("command") or "").lower()
        is_write = cmd in ("create_project", "add_novel") or (
            cmd == "call" and str(arguments.get("method") or "GET").upper() != "GET")
        return ApprovalRequest(
            tool_name=self.name,
            arguments=arguments,
            command=f"toonflow {cmd}" + (f" {arguments.get('path')}" if arguments.get("path") else ""),
            risk_level="medium" if is_write else "low",
            min_sandbox_mode="read-only",
        )


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
