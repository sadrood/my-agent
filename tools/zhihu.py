# -*- coding: utf-8 -*-
"""知乎数据开放平台工具（ZhihuTool）。

把开放平台的 9 类能力包成一条命令式工具，让 agent 直接用知乎的真实语料：
    搜索知乎 / 全网搜索 / 热榜 / 直答 / 问题推荐 / 问题回答摘要
    本人创作（全文、评论、账号与单篇数据）
    我的内容 / 关注 / 收藏 / 收藏夹
    知识库（列表、内容、上传、检索）
    小工具（PDF 解析、PPT 生成）
    额度查询

设计取舍：
- 参数用 snake_case（模型更好写），上游要的 PascalCase 由 models/zhihu 统一映射，
  工具层不拼 URL；
- 高频列表类命令（搜索/热榜/回答/知识库检索）**格式化成可读列表**再回给模型，
  比丢一大坨 JSON 省 token 也更好用；结构化数据用 call 命令直取原始信封；
- pdf_parse / ppt 是异步任务，默认轮询到终态并把产物下载到 output/zhihu/；
- 额度按能力独立计数且有限（如热榜 100/日、小工具 10/日），description 里已标注。
"""
from typing import Any, Dict

from tools.base import BaseTool, ToolResult

#: 会写本地文件或改动远端数据的命令（审批元数据与并行策略用）
_WRITE_COMMANDS = ("kb_upload", "pdf_parse", "ppt", "call")

#: 异步任务的失败终态
_TASK_FAILED = "failed"


class ZhihuTool(BaseTool):
    """知乎开放平台工具。"""

    risk_level: str = "low"             # 只读检索为主，不写本地/远端
    approval: str = "auto"
    min_sandbox_mode: str = "read-only"  # 纯 HTTP；写文件的命令在建审批请求时抬到 workspace-write
    parallel_safe: bool = True          # 无状态 HTTP，可并发

    def __init__(self, client=None):
        self._client = client

    # ------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "zhihu"

    @property
    def description(self) -> str:
        return (
            "知乎数据开放平台（真实知乎内容；需在 .env 配 ZHIHU_ACCESS_SECRET）。\n"
            "命令：health 自检 | routes 接口清单 | quota 额度\n"
            "  检索：search 知乎站内搜索 | web_search 全网搜索 | hot 热榜 | "
            "answers 问题回答摘要 | zhida 直答(大模型综合回答)\n"
            "  发现：recommend 问题推荐\n"
            "  我的：my_contents 我发的内容 | content_detail 我的全文 | "
            "content_comments 我的评论 | account_stats 账号数据 | "
            "content_stats 单篇数据 | followees 我关注的人 | collections 我的收藏 | "
            "favlists 收藏夹列表 | favlist_items 收藏夹内容\n"
            "  知识库：kb_list 列表 | kb_items 内容 | kb_upload 上传文件 | kb_search 检索\n"
            "  小工具：pdf_parse PDF 解析 | ppt 由知乎回答/文章生成 PPT | task 查异步任务\n"
            "  call 通用调用（任意 method/path/params/body）\n"
            "额度按能力独立计（每自然日）：全网搜/知乎搜索 5000，用户数据 10000，"
            "知识库 500，热榜/问题回答/创作能力/直答 各 100，小工具 10。\n"
            "典型用法：先 search 或 hot 找线索 → 用 url 调 answers/content_detail/"
            "content_comments 取深入内容 → 需要综合结论时用 zhida。\n"
            "注意：创作类接口（content_detail 等）只能读**当前 Secret 所属账号自己**的内容。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["health", "routes", "quota", "search", "web_search", "hot",
                             "answers", "zhida", "recommend", "my_contents",
                             "content_detail", "content_comments", "account_stats",
                             "content_stats", "followees", "collections", "favlists",
                             "favlist_items", "kb_list", "kb_items", "kb_upload",
                             "kb_search", "pdf_parse", "ppt", "task", "call"],
                    "description": "要执行的命令",
                },
                "query": {
                    "type": "string",
                    "description": "查询词（search / web_search / kb_search / recommend，"
                                   "recommend 可省略表示按账号画像推荐）",
                },
                "count": {"type": "number",
                          "description": "返回条数：search 默认 10 上限 10；"
                                         "web_search 默认 10 上限 20；recommend 默认 5"},
                "limit": {"type": "number",
                          "description": "返回条数（hot 默认 30 上限 30；answers/评论/"
                                         "内容 默认 20；kb_items 上限 20；kb_search 上限 10）"},
                "offset": {"type": "number", "description": "分页偏移，默认 0"},
                "sort_by": {
                    "type": "string",
                    "description": "search 排序：`字段:方向:(最小值,最大值)`，字段可选 "
                                   "CommentCount / VoteUpCount / EditTime，方向 asc|desc。"
                                   "例：`VoteUpCount:desc:(100,)` 取赞同≥100 降序",
                },
                "filter": {
                    "type": "string",
                    "description": "web_search 高级筛选：host==\"example.com\" AND "
                                   "publish_time>=1778494631（AND/OR 必须大写）",
                },
                "search_db": {"type": "string", "enum": ["all", "realtime", "static"],
                              "description": "web_search 索引库，默认 all"},
                "question_url": {"type": "string",
                                 "description": "answers 用：完整的知乎问题 URL"},
                "question": {"type": "string", "description": "zhida 用：要问的问题"},
                "model": {
                    "type": "string",
                    "enum": ["zhida-fast-1p5", "zhida-thinking-1p5", "zhida-agent"],
                    "description": "zhida 模型档位，默认 zhida-thinking-1p5",
                },
                "content_url": {
                    "type": "string",
                    "description": "内容链接（content_detail / content_comments / "
                                   "content_stats / ppt 用）；必须是当前账号自己的内容",
                },
                "content_type": {
                    "type": "string",
                    "enum": ["all", "answer", "article", "pin", "zvideo", "question"],
                    "description": "my_contents / account_stats 的内容类型，默认 all",
                },
                "sort_field": {"type": "string", "enum": ["ts", "like_count"],
                               "description": "my_contents 排序字段，默认 ts"},
                "sort_order": {"type": "string", "enum": ["desc", "asc"],
                               "description": "my_contents 排序方向，默认 desc"},
                "order": {"type": "string",
                          "enum": ["score", "reverse", "ascending"],
                          "description": "content_comments 排序：score 热度 / reverse "
                                         "时间倒序 / ascending 时间正序，默认 score"},
                "start_date": {"type": "string", "description": "统计开始日期 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "统计结束日期 YYYY-MM-DD"},
                "favlist_token": {"type": "string",
                                  "description": "favlist_items 用：收藏夹 UrlToken"
                                                 "（先 favlists 拿）"},
                "scope": {"type": "string", "enum": ["all", "created", "subscribed"],
                          "description": "kb_list 范围，默认 all"},
                "kb_id": {"type": "string",
                          "description": "知识库 ID（kb_items / kb_upload 指定目标库）"},
                "kb_ids": {"type": "array", "items": {"type": "string"},
                           "description": "kb_search 限定知识库 ID 列表"},
                "scopes": {
                    "type": "array", "items": {"type": "string",
                                               "enum": ["personal", "subscription", "public"]},
                    "description": "kb_search 检索范围；与 kb_ids 至少给一个",
                },
                "cursor": {"type": "string", "description": "kb_items 翻页游标"},
                "path": {"type": "string",
                         "description": "本地文件路径（kb_upload / pdf_parse）；"
                                        "call 命令里表示接口路径"},
                "num_pages": {"type": "number",
                              "description": "ppt 生成页数，范围 6~21，默认 12"},
                "wait": {"type": "boolean",
                         "description": "pdf_parse / ppt 是否轮询到完成（默认 true）；"
                                        "false 则只创建任务立即返回 task_id"},
                "task_id": {"type": "string", "description": "task 用：任务 ID"},
                "kind": {"type": "string", "enum": ["pdf", "ppt"],
                         "description": "task 用：任务类型"},
                "method": {"type": "string", "description": "call 用：HTTP 方法，默认 GET"},
                "params": {"type": "object", "description": "call 用：query 参数"},
                "body": {"type": "object", "description": "call 用：JSON body"},
                "api_ids": {"type": "array", "items": {"type": "string"},
                            "description": "quota 用：只要这些额度的 ID（默认全部）"},
            },
            "required": ["command"],
        }

    def is_parallel_safe(self, arguments: Dict[str, Any]) -> bool:
        cmd = str((arguments or {}).get("command") or "").lower()
        return cmd not in _WRITE_COMMANDS

    def build_approval_request(self, arguments: Dict[str, Any]):
        req = super().build_approval_request(arguments)
        cmd = str((arguments or {}).get("command") or "health").lower()
        if cmd in _WRITE_COMMANDS:
            req.risk_level = "medium"          # 会写本地文件 / 改动远端知识库
            req.min_sandbox_mode = "workspace-write"
        else:
            req.risk_level = "low"
            req.min_sandbox_mode = "read-only"
        return req

    # ------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        from models.zhihu import ZhihuClient, ZhihuError

        args = arguments or {}
        cmd = str(args.get("command") or "health").strip().lower()
        client = self._client or ZhihuClient()
        try:
            handler = {
                "health": self._health,
                "routes": self._routes,
                "quota": self._quota,
                "search": self._search,
                "web_search": self._web_search,
                "hot": self._hot,
                "answers": self._answers,
                "zhida": self._zhida,
                "recommend": self._recommend,
                "my_contents": self._my_contents,
                "content_detail": self._content_detail,
                "content_comments": self._content_comments,
                "account_stats": self._account_stats,
                "content_stats": self._content_stats,
                "followees": self._followees,
                "collections": self._collections,
                "favlists": self._favlists,
                "favlist_items": self._favlist_items,
                "kb_list": self._kb_list,
                "kb_items": self._kb_items,
                "kb_upload": self._kb_upload,
                "kb_search": self._kb_search,
                "pdf_parse": self._pdf_parse,
                "ppt": self._ppt,
                "task": self._task,
                "call": self._call,
            }.get(cmd)
            if handler is None:
                return ToolResult(
                    success=False, output="",
                    error=f"未知命令: {cmd}。用 routes 命令查看全部可用命令。")
            return handler(client, args)
        except ZhihuError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:      # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"知乎工具异常: {str(e)[:300]}")

    def execute(self, input_str: str) -> ToolResult:
        """字符串入口：`<命令> [参数]`。"""
        text = (input_str or "").strip()
        if not text:
            return self.execute_json({"command": "health"})
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        args: Dict[str, Any] = {"command": cmd}
        if rest:
            if cmd in ("search", "web_search", "kb_search", "recommend"):
                args["query"] = rest
            elif cmd == "zhida":
                args["question"] = rest
            elif cmd == "answers":
                args["question_url"] = rest
            elif cmd in ("content_detail", "content_comments", "content_stats", "ppt"):
                args["content_url"] = rest
            elif cmd in ("kb_items", "kb_upload"):
                args["kb_id"] = rest
            else:
                args["query"] = rest
        return self.execute_json(args)

    # ------------------------------------------------------------
    # 自检 / 清单 / 额度
    # ------------------------------------------------------------

    def _health(self, client, args) -> ToolResult:
        info = client.health()
        if not info.get("configured"):
            return ToolResult(
                success=False, output="",
                error="未配置知乎 Access Secret：请在 .env 加 "
                      "ZHIHU_ACCESS_SECRET=<在 https://developer.zhihu.com/profile "
                      "获取的 Access Secret>，然后重启 agent。")
        quota = info.get("quota")
        if not isinstance(quota, list):
            return ToolResult(
                success=False, output="",
                error=f"知乎自检失败：{quota}\n"
                      "排查：① ZHIHU_ACCESS_SECRET 是否正确；② 本机时钟是否与实际时间"
                      "相差超过 10 分钟（X-Request-Timestamp 会被服务端拒绝）。")
        lines = [f"✅ 知乎开放平台已连通：{info['base_url']}",
                 f"Secret: {info['secret']}",
                 f"本机时钟偏差: {info.get('clock_skew', 0)} 秒",
                 "今日额度："]
        for q in quota:
            lines.append("  %-14s %s/%s（剩余 %s）" % (q.get("APIName"),
                                                       q.get("TotalUsed"),
                                                       q.get("TotalQuota"),
                                                       q.get("RemainingQuota")))
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"base_url": info["base_url"]})

    def _routes(self, client, args) -> ToolResult:
        from models.zhihu import ENDPOINTS
        lines = ["已核实可用的接口（命令 → 方法 路径）："]
        for name, (method, path) in ENDPOINTS.items():
            lines.append(f"  {name:<16} {method:<5} {path}")
        lines.append("其余路径可用 call 命令直连（method/path/params/body）。")
        return ToolResult(success=True, output="\n".join(lines))

    def _quota(self, client, args) -> ToolResult:
        data = client.quota(args.get("api_ids"))
        return ToolResult(success=True, output=_fmt_quota(data),
                          metadata={"quota": data})

    # ------------------------------------------------------------
    # 检索类
    # ------------------------------------------------------------

    def _search(self, client, args) -> ToolResult:
        query = _need(args, "query")
        if query is None:
            return _missing("search", "query")
        data = client.search(query, int(args.get("count") or 10),
                             args.get("sort_by"))
        return ToolResult(success=True, output=_fmt_search(data, "知乎搜索"),
                          metadata={"query": query})

    def _web_search(self, client, args) -> ToolResult:
        query = args.get("query")
        if not str(query or "").strip():
            return _missing("web_search", "query")
        data = client.web_search(str(query), int(args.get("count") or 10),
                                 args.get("filter"), args.get("search_db"))
        return ToolResult(success=True, output=_fmt_search(data, "全网搜索"),
                          metadata={"query": query})

    def _hot(self, client, args) -> ToolResult:
        data = client.hot(int(args.get("limit") or 30))
        return ToolResult(success=True, output=_fmt_hot(data))

    def _answers(self, client, args) -> ToolResult:
        url = args.get("question_url")
        if not str(url or "").strip():
            return _missing("answers", "question_url")
        data = client.answers(str(url), int(args.get("offset") or 0),
                              int(args.get("limit") or 20))
        return ToolResult(success=True, output=_fmt_answers(data))

    def _recommend(self, client, args) -> ToolResult:
        data = client.recommend(args.get("query"), int(args.get("count") or 5))
        items = (data or {}).get("Items") or []
        lines = ["根据账号画像推荐的问题：" if not args.get("query")
                 else f"主题「{args['query']}」相关问题："]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it.get('Title')}\n   {it.get('Url')}")
        return ToolResult(success=True, output="\n".join(lines) or "（无结果）")

    def _zhida(self, client, args) -> ToolResult:
        question = args.get("question") or args.get("query")
        if not str(question or "").strip():
            return _missing("zhida", "question")
        model = str(args.get("model") or "zhida-thinking-1p5")
        res = client.zhida(str(question), model=model,
                           stream=bool(args.get("stream")))
        parts = [f"【直答 · {res.get('model')}】", res.get("content") or "（空回答）"]
        reasoning = res.get("reasoning") or ""
        if reasoning:
            if len(reasoning) > 1200:
                reasoning = reasoning[:1200] + "…（思考过程已截断）"
            parts.append("\n— 思考过程 —\n" + reasoning)
        return ToolResult(success=True, output="\n".join(parts),
                          metadata={"model": res.get("model")})

    # ------------------------------------------------------------
    # 我的内容 / 数据 / 关注 / 收藏
    # ------------------------------------------------------------

    def _my_contents(self, client, args) -> ToolResult:
        data = client.my_contents(str(args.get("content_type") or "all"),
                                 int(args.get("offset") or 0),
                                 int(args.get("limit") or 20),
                                 args.get("sort_field"), args.get("sort_order"))
        return ToolResult(success=True, output=_fmt_contents(data, "我的内容"))

    def _content_detail(self, client, args) -> ToolResult:
        url = args.get("content_url")
        if not str(url or "").strip():
            return _missing("content_detail", "content_url")
        data = client.content_detail(str(url)) or {}
        body = str(data.get("Body") or "")
        text = (f"【{data.get('ContentType')}】{data.get('Title') or '(无标题)'}\n"
                f"{data.get('Url')}\n\n{body}")
        return ToolResult(success=True, output=client.summarize(text),
                          metadata={"content_type": data.get("ContentType")})

    def _content_comments(self, client, args) -> ToolResult:
        url = args.get("content_url")
        if not str(url or "").strip():
            return _missing("content_comments", "content_url")
        data = client.content_comments(str(url), int(args.get("offset") or 0),
                                       int(args.get("limit") or 20),
                                       args.get("order"))
        return ToolResult(success=True, output=_fmt_comments(data))

    def _account_stats(self, client, args) -> ToolResult:
        data = client.account_stats(str(args.get("content_type") or "all"),
                                    args.get("start_date"), args.get("end_date"))
        return ToolResult(success=True, output=_fmt_stats(data, "账号创作数据"))

    def _content_stats(self, client, args) -> ToolResult:
        url = args.get("content_url")
        if not str(url or "").strip():
            return _missing("content_stats", "content_url")
        data = client.content_stats(str(url), args.get("start_date"),
                                    args.get("end_date"))
        return ToolResult(success=True, output=_fmt_content_stats(data))

    def _followees(self, client, args) -> ToolResult:
        data = client.followees(int(args.get("offset") or 0),
                                int(args.get("limit") or 20))
        items = (data or {}).get("Items") or []
        lines = [f"我关注的人（共 {(data or {}).get('Paging', {}).get('Totals', '?')}）："]
        for i, it in enumerate(items, 1):
            lines.append("%d. %s（粉丝 %s）\n   %s\n   %s"
                         % (i, it.get("Fullname"), it.get("FollowerCount"),
                            (it.get("Headline") or "").strip(), it.get("Url")))
        return ToolResult(success=True, output="\n".join(lines) if items else "（无）")

    def _collections(self, client, args) -> ToolResult:
        data = client.collections(int(args.get("limit") or 20))
        return ToolResult(success=True, output=_fmt_contents(data, "我的收藏"))

    def _favlists(self, client, args) -> ToolResult:
        data = client.favlists(int(args.get("limit") or 20))
        items = (data or {}).get("Items") or []
        lines = ["我的收藏夹（UrlToken 可用于 favlist_items）："]
        for i, it in enumerate(items, 1):
            lines.append("%d. %s（%s）token=%s\n   %s"
                         % (i, it.get("Title"),
                            "公开" if it.get("IsPublic") else "私密",
                            it.get("UrlToken"), (it.get("Description") or "").strip()))
        return ToolResult(success=True, output="\n".join(lines) if items else "（无收藏夹）")

    def _favlist_items(self, client, args) -> ToolResult:
        token = args.get("favlist_token")
        if token in (None, ""):
            return _missing("favlist_items", "favlist_token")
        data = client.favlist_items(token, int(args.get("offset") or 0),
                                    int(args.get("limit") or 20))
        return ToolResult(success=True, output=_fmt_contents(data, "收藏夹内容"))

    # ------------------------------------------------------------
    # 知识库
    # ------------------------------------------------------------

    def _kb_list(self, client, args) -> ToolResult:
        data = client.kb_list(str(args.get("scope") or "all"))
        items = (data or {}).get("Items") or []
        lines = ["知识库列表："]
        for i, it in enumerate(items, 1):
            lines.append("%d. %s%s（%s，%s 条）id=%s"
                         % (i, it.get("Name"), "【默认】" if it.get("IsDefault") else "",
                            it.get("Relation"), it.get("ContentCount"),
                            it.get("KnowledgeBaseID")))
            if it.get("Description"):
                lines.append("   " + str(it["Description"]).strip())
        return ToolResult(success=True, output="\n".join(lines) if items else "（无知识库）")

    def _kb_items(self, client, args) -> ToolResult:
        kb_id = args.get("kb_id")
        if not str(kb_id or "").strip():
            return _missing("kb_items", "kb_id")
        data = client.kb_items(str(kb_id), args.get("cursor"),
                               int(args.get("limit") or 20))
        items = (data or {}).get("Items") or []
        lines = [f"知识库 {kb_id} 的内容（共 {(data or {}).get('Total', '?')} 条）："]
        for i, it in enumerate(items, 1):
            lines.append("%d. [%s] %s" % (i, it.get("ContentType"), it.get("Title")))
            if it.get("Abstract"):
                lines.append("   " + str(it["Abstract"]).strip()[:200])
            if it.get("OriginUrl"):
                lines.append("   " + str(it["OriginUrl"]))
        if (data or {}).get("HasMore"):
            lines.append(f"（还有下一页，NextCursor={data.get('NextCursor')}）")
        return ToolResult(success=True, output="\n".join(lines) if items else "（无内容）")

    def _kb_upload(self, client, args) -> ToolResult:
        path = args.get("path")
        if not str(path or "").strip():
            return _missing("kb_upload", "path")
        data = client.kb_upload(str(path), args.get("kb_id")) or {}
        return ToolResult(
            success=True,
            output=("✅ 已上传到知识库 %s\n文件名: %s（%s 字节）\n"
                    "内容 ID: %s\n标题: %s\n摘要: %s"
                    % (data.get("KnowledgeBaseID"), data.get("FileName"),
                       data.get("FileSize"), data.get("RecallContentID"),
                       data.get("Title") or "(无)", data.get("Abstract") or "(无)")),
            metadata={"recall_content_id": data.get("RecallContentID")})

    def _kb_search(self, client, args) -> ToolResult:
        query = args.get("query")
        if not str(query or "").strip():
            return _missing("kb_search", "query")
        kb_ids = args.get("kb_ids")
        scopes = args.get("scopes")
        if not kb_ids and not scopes:
            return ToolResult(
                success=False, output="",
                error="kb_search 需要 kb_ids 或 scopes 至少一个"
                      "（kb_ids 用 kb_list 查；scopes 如 [\"personal\"]）。")
        data = client.kb_search(str(query), kb_ids, scopes,
                                int(args.get("limit") or 10))
        items = (data or {}).get("Items") or []
        lines = [f"知识库检索「{query}」命中 {len(items)} 篇："]
        for i, it in enumerate(items, 1):
            lines.append("%d. %s（库 %s）" % (i, it.get("DocName"),
                                              it.get("KnowledgeBaseID")))
            for frag in (it.get("Content") or [])[:3]:
                lines.append("   · " + str(frag).strip()[:300])
            if it.get("OriginUrl"):
                lines.append("   来源: " + str(it["OriginUrl"]))
        return ToolResult(success=True, output="\n".join(lines) if items else "（无命中）")

    # ------------------------------------------------------------
    # 小工具（异步任务）
    # ------------------------------------------------------------

    def _pdf_parse(self, client, args) -> ToolResult:
        path = args.get("path")
        if not str(path or "").strip():
            return _missing("pdf_parse", "path")
        file_id = client.upload_file(str(path))
        task_id = client.create_pdf_task(file_id)
        if not task_id:
            return ToolResult(success=False, output="",
                              error="PDF 解析任务创建失败：未返回 task_id。")
        if not args.get("wait", True):
            return ToolResult(success=True,
                              output=f"已创建 PDF 解析任务 task_id={task_id}"
                                     f"（file_id={file_id}）。用 task 命令查进度。",
                              metadata={"task_id": task_id})
        final, early = _wait_task_or_pending(client, "pdf", task_id)
        if early is not None:
            return early
        if final.get("task_status") == _TASK_FAILED:
            return _task_failure("pdf", final)
        result = final.get("result") or {}
        if not result.get("url"):
            return ToolResult(
                success=False, output="",
                error=f"PDF 解析任务 {task_id} 状态 {final.get('task_status')} "
                      f"但没有结果链接：{str(final.get('error'))[:200]}")
        lines = [f"✅ PDF 解析完成（task_id={task_id}）"]
        if result.get("summary"):
            lines.append("摘要: " + str(result["summary"]))
        local = client.download(result["url"], f"pdf_parse_{task_id}.json")
        lines.append(f"解析结果已保存: {local}")
        if result.get("expires_at_ms"):
            lines.append(f"（原始下载链接 {_human_ms(result['expires_at_ms'])} 过期，"
                         "已及时落盘）")
        return ToolResult(success=True, output="\n".join(lines),
                          metadata={"task_id": task_id, "path": local})

    def _ppt(self, client, args) -> ToolResult:
        url = args.get("content_url")
        if not str(url or "").strip():
            return _missing("ppt", "content_url")
        task_id = client.create_ppt_task(str(url), int(args.get("num_pages") or 12))
        if not task_id:
            return ToolResult(success=False, output="",
                              error="PPT 任务创建失败：未返回 task_id。")
        if not args.get("wait", True):
            return ToolResult(success=True,
                              output=f"已创建 PPT 生成任务 task_id={task_id}。"
                                     "用 task 命令（kind=ppt）查进度。",
                              metadata={"task_id": task_id})
        final, early = _wait_task_or_pending(client, "ppt", task_id)
        if early is not None:
            return early
        if final.get("task_status") == _TASK_FAILED:
            return _task_failure("ppt", final)
        result = final.get("result") or {}
        if not result.get("url"):
            return ToolResult(success=False, output="",
                              error=f"PPT 任务 {task_id} 状态 {final.get('task_status')} "
                                    f"但没有产物链接：{str(final.get('error'))[:200]}")
        local = client.download(result["url"], f"zhihu_{task_id}.pptx")
        return ToolResult(success=True,
                          output=f"✅ PPT 已生成并保存: {local}\n"
                                 f"（task_id={task_id}，原始链接 "
                                 f"{_human_ms(result.get('expires_at_ms'))} 过期）",
                          metadata={"task_id": task_id, "path": local})

    def _task(self, client, args) -> ToolResult:
        task_id = args.get("task_id")
        if not str(task_id or "").strip():
            return _missing("task", "task_id")
        kind = str(args.get("kind") or "").lower()
        if kind not in ("pdf", "ppt"):
            return ToolResult(success=False, output="",
                              error="task 需要 kind=pdf 或 kind=ppt（按任务 ID 前缀判断也可："
                                    "pdf_… / ppt_…）。")
        data = client.task_status(kind, str(task_id))
        lines = [f"任务 {task_id}：状态={data.get('task_status')} "
                 f"进度={data.get('progress')}"]
        if data.get("result"):
            lines.append("产物: " + str(data["result"]))
        if data.get("error"):
            lines.append("错误: " + str(data["error"]))
        return ToolResult(success=True, output="\n".join(lines), metadata=data)

    # ------------------------------------------------------------
    # 通用兜底
    # ------------------------------------------------------------

    def _call(self, client, args) -> ToolResult:
        path = str(args.get("path") or "").strip()
        if not path:
            return _missing("call", "path")
        method = str(args.get("method") or "GET").upper()
        data = client.request(method, path, params=args.get("params"),
                              json_body=args.get("body"))
        return ToolResult(success=True, output=client.summarize(data),
                          metadata={"path": path, "method": method})


# ------------------------------------------------------------
# 输出格式化（把 JSON 变成模型更好读的列表）
# ------------------------------------------------------------

def _need(args: Dict[str, Any], key: str):
    """取必填字符串参数；空/全空白返回 None。"""
    value = args.get(key)
    return str(value).strip() if str(value or "").strip() else None


def _missing(cmd: str, field: str) -> ToolResult:
    return ToolResult(success=False, output="",
                      error=f"{cmd} 需要 {field}（必填）。")


def _wait_budget() -> float:
    """轮询上限必须落在**执行器工具硬超时之内**。

    `ZHIHU_TASK_TIMEOUT` 默认 600s，而 `agent/executor.py` 对非 browser 工具一律
    300s 硬超时 —— 工具侧还在轮询、上层已经判超时并把整个 ToolResult 丢掉（含
    task_id），任务却在知乎侧继续跑、小工具额度已消耗，模型只能再建一个
    （2026-09-22 审计）。留 20s 余量让轮询自己收尾并把 task_id 交回给模型。
    """
    try:
        from config import TOOL_CONFIG
        tool_timeout = float(TOOL_CONFIG.get("tool_timeout", 300))
    except Exception:                   # noqa: BLE001
        tool_timeout = 300.0
    try:
        from config import ZHIHU_CONFIG
        configured = float(ZHIHU_CONFIG.get("task_timeout", 600))
    except Exception:                   # noqa: BLE001
        configured = 600.0
    return max(30.0, min(configured, tool_timeout - 20.0))


def _wait_task_or_pending(client, kind: str, task_id: str):
    """轮询到终态；到点还没结果就把 task_id 交回给模型。

    返回 `(final, early)`：`early` 非 None 时调用方直接把它当工具结果返回。
    这里集中 `ZhihuError` 的导入与捕获 —— 调用点在别的方法里，拿不到
    `execute_json` 那次局部 import。
    """
    from models.zhihu import ZhihuError
    try:
        return client.wait_task(kind, task_id, timeout=_wait_budget()), None
    except ZhihuError as e:
        return None, _still_running(kind, task_id, e)


def _still_running(kind: str, task_id: str, err) -> ToolResult:
    """轮询到点还没出终态：**按成功返回 task_id**，让模型用 task 命令继续查。

    这里若报失败（或让异常往上抛成"工具异常"），task_id 就丢了 —— 而任务在知乎侧
    还在跑、额度已经花掉（2026-09-22 审计）。与 video_gen 的 timed_out 分支同款处理。
    """
    return ToolResult(
        success=True,
        output=(f"⏳ {kind} 任务 {task_id} 仍在进行中，已到工具等待上限提前返回。\n"
                f"{str(err)[:200]}\n"
                f"稍后用 zhihu(command=\"task\", kind=\"{kind}\", task_id=\"{task_id}\") 继续查询。"),
        metadata={"task_id": task_id, "pending": True},
    )


def _human_ms(value: Any) -> str:
    """毫秒级时间戳 → 可读时间。"""
    try:
        import time as _t
        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(value) / 1000))
    except (TypeError, ValueError, OSError):
        return "-"


def _task_failure(kind: str, final: Dict[str, Any]) -> ToolResult:
    """把上游的 failed 终态翻成可行动的说明（不要当成成功）。"""
    err = final.get("error")
    code = err.get("code") if isinstance(err, dict) else err
    message = err.get("message") if isinstance(err, dict) else ""
    label = "PDF 解析" if kind == "pdf" else "PPT 生成"
    return ToolResult(
        success=False, output="",
        error=f"{label}任务 {final.get('task_id')} 失败（task_status=failed）："
              f"{code or ''} {message or ''}".strip()
              + "。这是知乎服务端返回的失败，不是本地参数问题；"
                "可稍后重试，或换一篇内容/文件再试"
                "（PDF 解析要点：文件本身能正常打开；PPT 生成要点："
                "resource_url 是知乎回答或文章链接）。",
        metadata={"task_id": final.get("task_id"), "task_status": _TASK_FAILED})


def _human_ts(value: Any) -> str:
    try:
        import time as _t
        return _t.strftime("%Y-%m-%d", _t.localtime(int(value)))
    except (TypeError, ValueError, OSError):
        return "-"


def _fmt_quota(data: Any) -> str:
    if not isinstance(data, list):
        return str(data)
    lines = ["今日额度（每自然日独立计数）："]
    for q in data:
        lines.append("  %-14s 已用 %s / %s，剩余 %s"
                     % (q.get("APIName"), q.get("TotalUsed"), q.get("TotalQuota"),
                        q.get("RemainingQuota")))
    return "\n".join(lines)


def _fmt_search(data: Any, title: str) -> str:
    items = (data or {}).get("Items") or []
    if not items:
        reason = (data or {}).get("EmptyReason")
        return f"{title}：无结果" + (f"（{reason}）" if reason else "")
    lines = [f"{title} 命中 {len(items)} 条："]
    for i, it in enumerate(items, 1):
        lines.append("%d. 【%s】%s" % (i, it.get("ContentType"), it.get("Title")))
        meta = "   %s | 赞同 %s | 评论 %s | %s | 权威 %s" % (
            it.get("AuthorName"), it.get("VoteUpCount"), it.get("CommentCount"),
            _human_ts(it.get("EditTime")), it.get("AuthorityLevel"))
        lines.append(meta)
        text = (it.get("ContentText") or "").replace("<em>", "").replace("</em>", "")
        if text:
            lines.append("   " + text.strip()[:300])
        for c in (it.get("CommentInfoList") or [])[:1]:
            if c.get("Content"):
                lines.append("   热门评论: " + str(c["Content"]).strip()[:150])
        lines.append("   " + str(it.get("Url")))
    return "\n".join(lines)


def _fmt_hot(data: Any) -> str:
    items = (data or {}).get("Items") or []
    lines = [f"知乎热榜（{len(items)} 条）："]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it.get('Title')}")
        if it.get("Summary"):
            lines.append("   " + str(it["Summary"]).strip()[:250])
        lines.append("   " + str(it.get("Url")))
    return "\n".join(lines) if items else "（热榜为空）"


def _fmt_answers(data: Any) -> str:
    items = (data or {}).get("Items") or []
    paging = (data or {}).get("Paging") or {}
    lines = [f"问题下的回答摘要（{len(items)} 条，共 {paging.get('Totals', '?')}）："]
    for i, it in enumerate(items, 1):
        lines.append("%d. %s" % (i, it.get("Url")))
        if it.get("Summary"):
            lines.append("   " + str(it["Summary"]).strip()[:400])
    if not paging.get("IsEnd", True):
        lines.append(f"（未到底，下一页 offset={paging.get('NextOffset')}）")
    return "\n".join(lines) if items else "（无回答）"


def _fmt_contents(data: Any, title: str) -> str:
    items = (data or {}).get("Items") or []
    paging = (data or {}).get("Paging") or {}
    lines = [f"{title}（{len(items)} 条，共 {paging.get('Totals', '?')}）："]
    for i, it in enumerate(items, 1):
        lines.append("%d. 【%s】%s" % (i, it.get("ContentType"),
                                       it.get("Title") or "(无标题)"))
        lines.append("   赞 %s | 评 %s | 收藏 %s | 发布 %s"
                     % (it.get("LikeCount"), it.get("CommentCount"),
                        it.get("FavoriteCount"), _human_ts(it.get("CreatedAt"))))
        if it.get("Summary"):
            lines.append("   " + str(it["Summary"]).strip()[:200])
        lines.append("   " + str(it.get("Url")))
    if paging and not paging.get("IsEnd", True):
        lines.append(f"（未到底，下一页 offset={paging.get('NextOffset')}）")
    return "\n".join(lines) if items else f"{title}：空"


def _fmt_comments(data: Any) -> str:
    items = (data or {}).get("Items") or []
    paging = (data or {}).get("Paging") or {}
    lines = [f"评论 {len(items)} 条："]
    for i, it in enumerate(items, 1):
        c = it.get("Comment") or {}
        content = str(c.get("Content") or "").replace("<p>", "").replace("</p>", "")
        lines.append("%d. %s（赞 %s | %s）" % (i, content.strip()[:300],
                                               c.get("LikeCount"),
                                               _human_ts(c.get("CreatedAt"))))
        for child in (it.get("Children") or [])[:3]:
            cc = child.get("Comment") or child
            text = str(cc.get("Content") or "").replace("<p>", "").replace("</p>", "")
            if text.strip():
                lines.append("   └ " + text.strip()[:200])
    if paging and not paging.get("IsEnd", True):
        lines.append("（还有下一页）")
    return "\n".join(lines) if items else "（无评论）"


def _fmt_stats(data: Any, title: str) -> str:
    if not isinstance(data, dict):
        return str(data)
    import json as _json
    lines = [f"{title}（content_type={data.get('ContentType')}）："]
    metrics = data.get("Metrics") or {}
    if metrics:
        keep = {k: v for k, v in metrics.items() if not isinstance(v, (dict, list))}
        lines.append("  总览: " + _json.dumps(keep, ensure_ascii=False))
        for label, key in (("昨日", "Yesterday"), ("今日", "Today")):
            sub = metrics.get(key)
            if isinstance(sub, dict):
                flat = {k: v for k, v in sub.items() if not isinstance(v, (dict, list))}
                if flat:
                    lines.append(f"  {label}: " + _json.dumps(flat, ensure_ascii=False))
    if data.get("CreationCounts"):
        lines.append("  创作量: " + _json.dumps(data["CreationCounts"], ensure_ascii=False))
    if data.get("Followers"):
        lines.append("  粉丝: " + _json.dumps(data["Followers"], ensure_ascii=False))
    followers = data.get("FollowerDetails") or {}
    if isinstance(followers.get("Period"), dict):
        lines.append("  粉丝周期: " + _json.dumps(followers["Period"], ensure_ascii=False))
    audience = data.get("Audience") or {}
    if audience:
        if audience.get("Reason"):
            lines.append("  画像说明: " + str(audience["Reason"]))
        for key in ("Gender", "Age", "Interest", "Location", "Source", "ActiveTime"):
            rows = audience.get(key)
            if isinstance(rows, list) and rows:
                top = ", ".join("%s %s%%" % (r.get("Name"), round(float(r.get("Ratio") or 0) * 100, 1))
                                for r in rows[:6])
                lines.append(f"  画像·{key}: {top}")
    return "\n".join(lines)


def _fmt_content_stats(data: Any) -> str:
    items = (data or {}).get("Items") or []
    import json as _json
    lines = [f"单篇创作数据（{len(items)} 条）："]
    for i, it in enumerate(items, 1):
        lines.append("%d. 【%s】%s" % (i, it.get("ContentType"),
                                       it.get("Title") or it.get("Url")))
        metrics = it.get("Metrics") or {}
        flat = {k: v for k, v in metrics.items() if not isinstance(v, (dict, list))}
        if flat:
            lines.append("   " + _json.dumps(flat, ensure_ascii=False))
        for label, key in (("昨日", "Yesterday"), ("今日", "Today")):
            sub = metrics.get(key)
            if isinstance(sub, dict):
                sub_flat = {k: v for k, v in sub.items() if not isinstance(v, (dict, list))}
                if sub_flat:
                    lines.append(f"  {label}: " + _json.dumps(sub_flat, ensure_ascii=False))
        if it.get("Url"):
            lines.append("   " + str(it["Url"]))
    return "\n".join(lines) if items else "（无数据）"
