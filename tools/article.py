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
  article write <主题>                 写一篇（默认：大纲→初稿→审阅→核查→修订→校对→定稿）
  article write <主题> | <要求>         带写作要求（受众、字数、风格、必须包含的点）
  article models                       查看各阶段用哪个模型（谁写、谁审）
  article help                         本帮助

产物落在 output/articles/<主题>-<时间>/：outline.md / draft.md / review-rN.md /
factcheck-rN.md / revise-rN.md / proofread.md / final.md / changes.md（修改台账）/ meta.json
模型分配与论数上限在 .env：ARTICLE_MODEL_* / ARTICLE_MAX_REVISE_ROUNDS / ARTICLE_FACTCHECK"""


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
            "文章工坊（多模型互审写作）：一个模型写、**另一个厂商的模型**审阅与校对，"
            "中间自动做事实核查与逐条修订，最后定稿。适合需要质量保证的长文/报告。\n"
            "  article write <主题> [| 写作要求]   跑完整流水线并落盘\n"
            "  article models                    查看各阶段用了哪个模型\n"
            "产物在 output/articles/<主题>-<时间>/（final.md 是定稿，changes.md 是修改台账）；"
            "耗时较长（多轮模型调用 + 联网核查），短文/草稿不要用它，直接用 write 工具更快。"
        )

    @property
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["write", "models", "help"],
                              "description": "write=写文章；models=查看模型分配；help=帮助"},
                "topic": {"type": "string", "description": "write 的文章主题/标题"},
                "requirements": {"type": "string",
                                 "description": "write 的写作要求（受众、字数、风格、必须包含的内容）"},
                "target_length": {"type": "string", "description": "目标篇幅，如 '1500字'（可省）"},
                "max_rounds": {"type": "integer",
                               "description": "审阅→修订最多几轮（默认取配置 ARTICLE_MAX_REVISE_ROUNDS）"},
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

    def execute_json(self, arguments: Dict[str, Any]) -> ToolResult:
        op = str(arguments.get("operation", "") or "").strip().lower()
        if op in ("help", ""):
            return ToolResult(success=True, output=_HELP)
        if op == "models":
            return ToolResult(success=True, output=self._models_text())
        if op != "write":
            return ToolResult(success=False, output="",
                              error=f"未知操作 {op!r}（write / models / help）")

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
        """文本协议：write <主题> [| 要求] / models / help"""
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
        if op == "write":
            if not rest:
                return ToolResult(success=False, output="",
                                  error="用法: article write <主题> [| 写作要求]")
            topic, _, requirements = rest.partition("|")
            return self.execute_json({"operation": "write", "topic": topic.strip(),
                                      "requirements": requirements.strip()})
        # 容错：直接给了主题（没写 write）
        return self.execute_json({"operation": "write", "topic": text})
