"""文章工坊工具（article）：多模型互审写文章。

与 research / team 的区别：
- research 是"查资料写报告"，只有一个模型；
- team 是通用任务拆解，各角色共用同一个模型，审校意见不回炉；
- article 是**以文章为中心**的流水线：初稿 → 另一个模型审阅 → 事实核查 →
  作者逐条回应并改稿（有界循环）→ 另一个模型校对 → 定稿，全程留痕。

用法（文本协议）：
    article write <主题>                    直接写
    article write <主题> | <写作要求>        带要求（受众/字数/风格）
    article models                          查看各阶段用哪个模型
    article help                            帮助
"""
import os
from typing import Any, Dict, Optional

from tools.base import BaseTool, ToolResult

_HELP = """文章工坊（多模型互审）：
  —— 写新文章 ——
  article write <主题>                 写一篇（默认：大纲→初稿→审阅→核查→修订→校对→定稿）
  article write <主题> | <要求>         带写作要求（受众、字数、风格、必须包含的点）
  —— 检查已有文稿（不重写全文）——
  article review <文件路径>             审阅提意见（事实/逻辑/结构），有事实问题时联网核查
  article proofread <文件路径>          校对挑错（错别字/标点/术语/语法/格式）并给修正稿
  article models                       查看各阶段用哪个模型（谁写、谁审）
  article help                         本帮助

审阅/校对由**另一个厂商的模型**做（默认 Agnes），所以能挑出作者模型自己看不见的问题。
若用户只是让你"顺手改一下措辞"，直接用 edit 工具即可，不必走这里。

产物落在 output/articles/<名字>-<时间>/：write 走 outline/draft/review-rN/factcheck-rN/
revise-rN/proofread/final/changes；review 走 review.md+review.json(+factcheck.md)；
proofread 走 proofread.md+proofread.json+final.md（修正稿）。都带 meta.json。
模型分配在 .env：ARTICLE_MODEL_* / ARTICLE_MAX_REVISE_ROUNDS / ARTICLE_FACTCHECK"""


class ArticleTool(BaseTool):
    """多模型互审写作流水线（跨厂商：一家写、另一家审）。"""

    risk_level: str = "medium"                 # 会写文件 + 大量外部模型调用
    approval: str = "auto"
    min_sandbox_mode: str = "workspace-write"
    parallel_safe: bool = False                # 内部串行多阶段，并行只会互相抢配额

    def __init__(self, tool_manager=None):
        self._tool_manager = tool_manager
        self._emit_fn = None

    def set_emit(self, fn) -> None:
        """注入事件回调（Agent 的 _emit）：阶段进度实时进 dashboard/rollout。"""
        self._emit_fn = fn

    @property
    def name(self) -> str:
        return "article"

    @property
    def description(self) -> str:
        return (
            "文章工坊（多模型互审写作 / 校对审阅）：一个模型写、**另一个厂商的模型**审阅与校对。\n"
            "  article write <主题> [| 写作要求]    从零写一篇（大纲→初稿→审阅→事实核查→修订→校对→定稿）\n"
            "  article review <文件路径>           审阅**已有文稿**并提意见（不改写原文）\n"
            "  article proofread <文件路径>        校对**已有文稿**（错别字/标点/术语/语法/格式）并给修正稿\n"
            "  article models                     查看各阶段用了哪个模型\n"
            "用户说「校对 / 审阅 / 挑错 / 提意见 / 润色 / 帮我把把关 / 看看有没有错别字」时用 review 或 "
            "proofread（短文可直接传 text 参数，长文传 file 路径）；用户说「写一篇/写篇文章/写报告」时用 write。\n"
            "产物在 output/articles/<名字>-<时间>/（final.md 是成品/修正稿，changes.md 是修改台账）；"
            "写一篇耗时较长（多轮模型调用 + 联网核查），只是顺手改几句措辞就别用它，直接用 edit 更快。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["write", "review", "proofread", "models", "help"],
                    "description": "write=写新文章；review=审阅已有文稿提意见；"
                                   "proofread=校对已有文稿并给修正稿；models=模型分配；help=帮助"},
                "topic": {"type": "string", "description": "write 的文章主题/标题"},
                "requirements": {"type": "string",
                                 "description": "write 的写作要求（受众、字数、风格、必须包含的内容）"},
                "target_length": {"type": "string", "description": "目标篇幅，如 '1500字'（可省）"},
                "max_rounds": {"type": "integer",
                               "description": "write 时审阅→修订最多几轮（默认 ARTICLE_MAX_REVISE_ROUNDS）"},
                "file": {"type": "string",
                         "description": "review/proofread 的目标文件路径（.md/.txt；与 text 二选一）"},
                "text": {"type": "string",
                         "description": "review/proofread 的正文（短文本直接给；与 file 二选一）"},
                "title": {"type": "string", "description": "review/proofread 的文稿标题（可省）"},
                "apply": {"type": "boolean",
                          "description": "proofread 时是否把修正稿写回原文件（默认 false，"
                                         "只产出 output 下的 final.md；写回前会留一份 .bak 备份）"},
            },
            "required": ["operation"],
        }

    # ------------------------------------------------------------

    def _emit(self, event: str, data: dict) -> None:
        if self._emit_fn is None:
            return
        try:
            self._emit_fn(event, data)
        except Exception:                       # noqa: BLE001
            pass

    def _models_text(self) -> str:
        from config import ARTICLE_CONFIG
        from models.article import stage_models

        lines = ["文章工坊 · 阶段模型分配："]
        for stage, tag in stage_models(ARTICLE_CONFIG).items():
            lines.append(f"  {stage:10s} → {tag}")
        stages = stage_models(ARTICLE_CONFIG)
        draft = stages.get("draft", "").split(":")[0]
        reviewers = {stages.get(s, "").split(":")[0] for s in ("review", "proofread")}
        if reviewers and reviewers == {draft}:
            lines.append("  ⚠ 写与审是同一个端点：建议在 .env 把 ARTICLE_MODEL_REVIEW / "
                         "ARTICLE_MODEL_PROOFREAD 指向另一家模型（跨厂商互审才有信息增益）")
        lines.append("  （.env 里 ARTICLE_MODEL_* 可逐阶段改，写 'agnes:模型名' 也能指定具体模型）")
        return "\n".join(lines)

    def _read_target(self, arguments: Dict[str, Any]) -> tuple:
        """取出待检查的正文 → (正文, 标题, 来源路径)。file 与 text 二选一。"""
        path = str(arguments.get("file", "") or "").strip()
        inline = str(arguments.get("text", "") or "").strip()
        title = str(arguments.get("title", "") or "").strip()
        if path:
            from config import resolve_under_root
            full = resolve_under_root(path)
            if not os.path.isfile(full):
                raise ValueError(f"找不到文件：{path}")
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
            if not body.strip():
                raise ValueError(f"文件是空的：{path}")
            return body, (title or os.path.splitext(os.path.basename(full))[0]), full
        if inline:
            return inline, (title or inline[:20]), ""
        raise ValueError("需要 file（文件路径）或 text（正文）其中之一")

    @staticmethod
    def _apply_fixes(path: str, fixed: str) -> str:
        """把修正稿写回原文件（先留 .bak 备份）。返回备份路径。"""
        backup = path + ".bak"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            original = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(original)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fixed if fixed.endswith("\n") else fixed + "\n")
        return backup

    def _run_check(self, op: str, arguments: Dict[str, Any]) -> ToolResult:
        try:
            body, title, source = self._read_target(arguments)
        except ValueError as e:
            return ToolResult(success=False, output="", error=str(e))

        from models.article import ArticleError, ArticlePipeline

        pipeline = ArticlePipeline(tool_manager=self._tool_manager,
                                   emit=self._emit, verbose=False)
        try:
            result = pipeline.check_text(body, mode=op, title=title, source=source)
        except ArticleError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:                  # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"文章流水线异常：{type(e).__name__}: {str(e)[:200]}")

        out = [result.summary()]
        if result.issues:
            out.append("\n--- 问题清单 ---")
            out.extend(f"  · {i.line()}" for i in result.issues[:20])
            if len(result.issues) > 20:
                out.append(f"  …（共 {len(result.issues)} 条，全部见产物目录）")
        if result.fixed_text:
            preview = result.fixed_text[:600]
            out.append(f"\n--- 修正稿预览 ---\n{preview}")
        if arguments.get("apply") and result.mode == "proofread" and result.fixed_text:
            if not source:
                out.append("\n（apply 需要 file 参数：text 形式的输入没有可回写的文件）")
            else:
                try:
                    backup = self._apply_fixes(source, result.fixed_text)
                    out.append(f"\n已写回原文件：{source}（原内容备份在 {backup}）")
                except Exception as e:          # noqa: BLE001
                    out.append(f"\n写回失败：{type(e).__name__}: {str(e)[:150]}")
        elif result.mode == "proofread" and result.fixed_text:
            out.append("\n（原文件未改动；需要就地替换原文件时再带 apply=true）")
        return ToolResult(success=True, output="\n".join(out))

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "") or "").strip().lower()
        if op in ("help", ""):
            return ToolResult(success=True, output=_HELP)
        if op == "models":
            return ToolResult(success=True, output=self._models_text())
        if op in ("review", "proofread"):
            return self._run_check(op, arguments)
        if op != "write":
            return ToolResult(success=False, output="",
                              error=f"未知操作 {op!r}（write / review / proofread / models / help）")

        topic = str(arguments.get("topic", "") or "").strip()
        if not topic:
            return ToolResult(success=False, output="", error="write 需要 topic（文章主题）。")
        requirements = str(arguments.get("requirements", "") or "").strip()
        target_length = str(arguments.get("target_length", "") or "").strip()
        max_rounds = arguments.get("max_rounds")

        from models.article import ArticleError, ArticlePipeline

        pipeline = ArticlePipeline(tool_manager=self._tool_manager,
                                   emit=self._emit, verbose=False)
        try:
            result = pipeline.write(topic, requirements=requirements,
                                    target_length=target_length,
                                    max_rounds=int(max_rounds) if max_rounds else None)
        except ArticleError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:                  # noqa: BLE001
            return ToolResult(success=False, output="",
                              error=f"文章流水线异常：{type(e).__name__}: {str(e)[:200]}")

        final_path = os.path.join(result.out_dir, "final.md")
        preview = result.final_text[:1200]
        if len(result.final_text) > 1200:
            preview += f"\n…（全文 {len(result.final_text)} 字，见 {final_path}）"
        return ToolResult(success=True, output=f"{result.summary()}\n\n--- 定稿预览 ---\n{preview}")

    def execute(self, input_str: str) -> ToolResult:
        """文本协议：write <主题> [| 要求] / review <文件> / proofread <文件> / models / help"""
        text = (input_str or "").strip()
        if not text or text.lower() in ("help", "?"):
            return ToolResult(success=True, output=_HELP)
        op, _, rest = text.partition(" ")
        op = op.lower()
        rest = rest.strip()
        if op in ("help", "?"):
            return ToolResult(success=True, output=_HELP)
        if op == "models":
            return ToolResult(success=True, output=self._models_text())
        if op in ("review", "proofread", "check", "校", "校对", "审阅"):
            if not rest:
                return ToolResult(success=False, output="",
                                  error=f"用法: article {op} <文件路径>")
            mode = "review" if op in ("review", "审阅") else "proofread"
            return self.execute_json({"operation": mode, "file": rest})
        if op == "write":
            if not rest:
                return ToolResult(success=False, output="",
                                  error="用法: article write <主题> [| 写作要求]")
            topic, _, requirements = rest.partition("|")
            return self.execute_json({"operation": "write", "topic": topic.strip(),
                                      "requirements": requirements.strip()})
        # 容错：直接给了主题（没写 write）
        return self.execute_json({"operation": "write", "topic": text})
