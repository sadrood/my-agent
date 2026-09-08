"""
Deep Research 深度研究模块。

工作流程：
1. 搜索规划 → 确定搜索策略和关键词
2. 多源采集 → 并行爬取多个来源
3. 内容提取 → 从HTML中提取标题、正文、关键数据
4. 交叉验证 → 对比不同来源的一致性
5. 综合报告 → 生成带引用来源的结构化报告

依赖: BrowserTool (浏览器工具)、VisionModel (视觉辅助)
"""
import re
import time
import json
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from tools.tool_manager import ToolManager


@dataclass
class ResearchSource:
    """研究来源。"""
    url: str
    title: str = ""
    snippet: str = ""
    full_content: str = ""
    relevance: float = 0.0       # 相关性评分 0-1
    credibility: float = 0.0     # 可信度评分 0-1
    key_facts: List[str] = field(default_factory=list)


@dataclass
class ResearchReport:
    """研究报告。"""
    topic: str
    summary: str                  # 摘要
    sections: List[Dict]          # 各章节
    sources: List[ResearchSource] # 引用来源
    confidence: float = 0.0       # 整体置信度


class DeepResearcher:
    """
    深度研究员。
    自动搜索 → 采集 → 提取 → 验证 → 生成报告。

    用法:
        researcher = DeepResearcher(tool_manager, llm)
        report = researcher.research("2024年AI Agent发展趋势", depth=3)
        print(report.summary)
    """

    # 默认搜索引擎和搜索 URL 模板
    SEARCH_ENGINES = {
        "google": "https://www.google.com/search?q={query}",
        "bing": "https://www.bing.com/search?q={query}",
        "duckduckgo": "https://duckduckgo.com/?q={query}",
    }

    def __init__(self, tool_manager: ToolManager, llm=None):
        self.tool_manager = tool_manager
        self.llm = llm
        self._results_cache: Dict[str, Any] = {}

    def research(
        self,
        topic: str,
        depth: int = 3,
        sources_per_query: int = 5,
        include_citations: bool = True,
    ) -> ResearchReport:
        """
        执行深度研究。

        Args:
            topic: 研究主题
            depth: 研究深度（1-5），越高越深入
            sources_per_query: 每个搜索词采集的源数量
            include_citations: 是否包含引用

        Returns:
            ResearchReport
        """
        print(f"\n[DeepResearch] 开始研究: {topic}")
        print(f"[DeepResearch] 深度: {depth}, 每词源数: {sources_per_query}")

        # 1. 生成搜索策略
        queries = self._generate_search_queries(topic, depth)
        print(f"[DeepResearch] 生成 {len(queries)} 个搜索词")

        # 2. 多源采集
        all_sources: List[ResearchSource] = []
        for query in queries[:depth * 2]:
            sources = self._search_and_collect(query, limit=sources_per_query)
            all_sources.extend(sources)
            print(f"[DeepResearch] '{query}' → {len(sources)} 个来源")

        # 去重
        seen_urls = set()
        unique_sources = []
        for s in all_sources:
            if s.url not in seen_urls:
                seen_urls.add(s.url)
                unique_sources.append(s)

        print(f"[DeepResearch] 去重后共 {len(unique_sources)} 个来源")

        # 3. 深度提取（访问页面获取详细内容）
        for i, source in enumerate(unique_sources[:depth * 3]):
            print(f"[DeepResearch] 提取 [{i+1}/{min(len(unique_sources), depth*3)}]: {source.url[:80]}")
            content = self._extract_page_content(source.url)
            if content:
                source.full_content = content[:3000]
                source.key_facts = self._extract_key_facts(content, topic)

        # 4. 综合分析
        report = self._synthesize_report(topic, unique_sources, include_citations)
        print(f"[DeepResearch] 报告生成完毕，{len(report.sections)} 个章节")

        return report

    def _generate_search_queries(self, topic: str, depth: int) -> List[str]:
        """生成搜索关键词策略。"""
        # 策略1: 使用 LLM 生成搜索词
        if self.llm:
            try:
                messages = [
                    {"role": "system", "content": (
                        "你是一个搜索策略专家。为研究主题生成多样的搜索关键词。\n"
                        "要求：覆盖不同角度、使用不同表述、中英文结合。"
                    )},
                    {"role": "user", "content": (
                        f"研究主题: {topic}\n"
                        f"请生成 {depth * 3} 个搜索关键词，每行一个，多样化。"
                    )},
                ]
                response = self.llm.chat(messages, temperature=0.7, max_tokens=500)
                queries = [q.strip() for q in response.split("\n") if q.strip()]
                queries = [q for q in queries if not q.startswith("#")]
                if queries:
                    return queries[:depth * 3]
            except Exception:
                pass

        # 策略2: 规则生成回退
        base_queries = [
            topic,
            f"{topic} 2024",
            f"{topic} 2025",
            f"{topic} 最新",
            f"{topic} 发展趋势",
            f"{topic} 技术分析",
            f"{topic} 对比",
            f"what is {topic}",
            f"{topic} latest research",
            f"{topic} best practices",
        ]
        return base_queries[:depth * 2]

    def _search_and_collect(self, query: str, limit: int = 5) -> List[ResearchSource]:
        """搜索并收集结果源。"""
        sources = []
        engine = "bing"

        try:
            browser = self.tool_manager.get_tool("browser")
            if browser is None:
                return sources

            # 打开搜索结果页
            search_url = self.SEARCH_ENGINES[engine].format(
                query=query.replace(" ", "+")
            )

            goto_result = self.tool_manager.execute("browser", f"goto {search_url}")
            if not goto_result.success:
                return sources

            time.sleep(1)

            # 获取页面文本，提取链接
            text_result = self.tool_manager.execute("browser", "text")
            if not text_result.success:
                return sources

            # 从文本中提取 URL
            href_result = self.tool_manager.execute("browser", "html")
            urls = self._extract_urls_from_html(href_result.output) if href_result.success else []

            # 尝试从搜索结果中提取标题和摘要
            page_text = text_result.output

            for i, url in enumerate(urls[:limit]):
                if any(noise in url.lower() for noise in [
                    "google.com", "bing.com", "youtube.com",
                    "facebook.com", "twitter.com", "instagram.com",
                    "login", "signin", "javascript:",
                ]):
                    continue

                # 提取标题（从页面文本中模糊匹配）
                title = self._extract_title_for_url(page_text, url)

                sources.append(ResearchSource(
                    url=url,
                    title=title,
                    snippet="",
                ))

        except Exception as e:
            print(f"[DeepResearch] 搜索 '{query}' 出错: {e}")

        return sources

    def _extract_page_content(self, url: str) -> str:
        """访问页面并提取正文内容。"""
        try:
            self.tool_manager.execute("browser", f"goto {url}")
            time.sleep(0.5)

            # 尝试提取主要文本
            text_result = self.tool_manager.execute("browser", "text")
            if text_result.success and text_result.output:
                text = text_result.output
                # 清理：去掉导航、页脚等噪音
                cleaned = self._clean_page_text(text)
                return cleaned[:5000]

        except Exception as e:
            print(f"[DeepResearch] 提取页面失败: {url[:60]} - {e}")

        return ""

    def _extract_key_facts(self, content: str, topic: str) -> List[str]:
        """从内容中提取关键事实。"""
        if not self.llm or not content or len(content) < 50:
            return []

        try:
            messages = [
                {"role": "system", "content": "从文本中提取与主题相关的关键事实，每条一行，用中文。只提取事实性陈述。"},
                {"role": "user", "content": f"主题: {topic}\n\n文本:\n{content[:3000]}"},
            ]
            response = self.llm.chat(messages, temperature=0.2, max_tokens=800)
            facts = [f.strip().lstrip("- ").lstrip("1234567890. ")
                     for f in response.split("\n") if f.strip()]
            return facts[:10]
        except Exception:
            return []

    def _synthesize_report(
        self,
        topic: str,
        sources: List[ResearchSource],
        include_citations: bool = True,
    ) -> ResearchReport:
        """综合分析并生成研究报告。"""
        # 收集所有关键事实
        all_facts = []
        for s in sources:
            for f in s.key_facts:
                all_facts.append({"fact": f, "source": s.url, "title": s.title})

        # 让 LLM 综合生成报告
        if self.llm:
            sources_text = "\n\n".join(
                f"来源 {i+1}: {s.title}\nURL: {s.url}\n内容摘要: {s.full_content[:500] if s.full_content else s.snippet[:500]}"
                for i, s in enumerate(sources[:10]) if s.full_content or s.snippet
            )

            messages = [
                {"role": "system", "content": (
                    "你是一个专业研究分析师。根据收集的资料，撰写一份结构化的研究报告。\n"
                    "格式要求：\n"
                    "## 摘要\n## 核心观点\n## 详细分析\n## 趋势与展望\n## 结论\n"
                    + ("需要标注引用来源 [来源N]" if include_citations else "")
                )},
                {"role": "user", "content": (
                    f"研究主题: {topic}\n\n"
                    f"收集的资料:\n{sources_text[:6000]}\n\n"
                    + (f"关键事实:\n" + "\n".join(f"- {af['fact']}" for af in all_facts[:30]) + "\n\n"
                       if all_facts else "")
                    + "请撰写结构化研究报告。"
                )},
            ]

            try:
                report_text = self.llm.chat(messages, temperature=0.5, max_tokens=4000)

                # 解析章节
                sections = []
                current_section = None
                current_content = []

                for line in report_text.split("\n"):
                    if line.startswith("## "):
                        if current_section:
                            sections.append({
                                "title": current_section,
                                "content": "\n".join(current_content),
                            })
                        current_section = line[3:].strip()
                        current_content = []
                    else:
                        current_content.append(line)

                if current_section:
                    sections.append({
                        "title": current_section,
                        "content": "\n".join(current_content),
                    })

                # 提取摘要
                summary = ""
                for s in sections:
                    if "摘要" in s.get("title", ""):
                        summary = s.get("content", "")

                return ResearchReport(
                    topic=topic,
                    summary=summary or report_text[:500],
                    sections=sections or [{"title": "报告", "content": report_text}],
                    sources=sources[:20],
                    confidence=min(1.0, len([s for s in sources if s.full_content]) * 0.2),
                )

            except Exception as e:
                print(f"[DeepResearch] LLM 综合失败: {e}")

        # 回退：纯规则生成
        return ResearchReport(
            topic=topic,
            summary=f"关于 {topic} 的研究报告，共收集 {len(sources)} 个来源。",
            sections=[{
                "title": "收集的资料",
                "content": "\n".join(
                    f"- [{s.title or '未知'}]({s.url})" for s in sources
                ),
            }],
            sources=sources,
            confidence=0.3,
        )

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _extract_urls_from_html(html: str) -> List[str]:
        """从 HTML 中提取链接 URL。"""
        urls = []
        # 匹配 href 属性
        href_pattern = re.compile(r'href=["\'](https?://[^"\']+)["\']')
        matches = href_pattern.findall(html)

        seen = set()
        for url in matches:
            url = url.strip().rstrip("/")
            if url not in seen:
                seen.add(url)
                urls.append(url)

        return urls

    @staticmethod
    def _extract_title_for_url(page_text: str, url: str) -> str:
        """从页面文本中为 URL 提取标题。"""
        # 简单策略：找包含 URL 关键词的行
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            lines = page_text.split("\n")
            for line in lines:
                if any(kw in line.lower() for kw in domain.split(".")[:1]):
                    return line.strip()[:100]
        except Exception:
            pass
        return ""

    @staticmethod
    def _clean_page_text(text: str) -> str:
        """清理页面文本，去除导航、页脚等噪音。"""
        lines = text.split("\n")
        cleaned = []
        # 跳过长行（可能是代码或样式）
        # 跳过太短的行（可能是导航项）
        for line in lines:
            stripped = line.strip()
            if not stripped:
                cleaned.append("")
                continue
            if len(stripped) < 5:
                continue
            if len(stripped) > 500:
                continue
            if stripped.startswith(("<!--", "<script", "<style")):
                continue
            # 去掉大量是标点/空格的行
            if sum(1 for c in stripped if c.isalpha()) < 3:
                continue
            cleaned.append(stripped)

        return "\n".join(cleaned)
