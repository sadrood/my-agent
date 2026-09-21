"""
记忆模块（进化版 v2）。
支持：
- 短期记忆：对话历史 + 步骤历史（当前会话）
- 长期记忆：手动存储的知识
- 经验库：每次任务完成后自动保存，下次自动召回
- 失败模式库：自动归纳常见失败原因，生成规避策略
- 策略库：追踪不同方案的成功率，优先推荐高成功率方案
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import LEARN_CONFIG

from config import PROJECT_ROOT


# ============================================================
# 数据结构
# ============================================================

@dataclass
class MemoryEntry:
    """单条记忆条目。"""
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ExperienceEntry:
    """一次完整任务的经验记录。"""
    goal: str
    plan_steps: list
    success: bool
    total_steps: int
    completed_steps: int
    failed_steps: int
    summary: str
    task_category: str        # 任务类别标签
    tool_usage: dict          # {tool_name: usage_count}
    errors: list              # 遇到的错误列表
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class FailurePattern:
    """一个失败模式。"""
    pattern_id: str           # 唯一标识（错误类型 + 上下文）
    error_type: str           # 错误分类
    description: str          # 错误描述
    context_keywords: list    # 触发此错误的任务关键词
    avoidance_strategy: str   # 规避策略
    occurrence_count: int = 1
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class StrategyEntry:
    """一个策略方案的成功率记录。"""
    task_category: str
    approach: str             # 方案描述（如 "python+openpyxl" vs "启动Excel GUI"）
    success_count: int = 0
    failure_count: int = 0
    last_used: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def win_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.5


# ============================================================
# Memory 类（升级版）
# ============================================================

class Memory:
    """
    Agent 记忆管理器 v2。
    四层记忆体系：短期记忆 → 长期记忆 → 经验库 → 失败模式库
    """

    # 长期记忆自动截断上限（remember() 超过时清理最旧条目）
    MAX_LONG_TERM = 200

    # 失败模式分类规则：按关键词匹配
    ERROR_CLASSIFIERS = [
        (r"(命令返回码|return code|exit code).*1\b|command failed|命令失败", "command_failed"),
        (r"(找不到文件|not found|no such file|cannot find|找不到|找不)", "file_not_found"),
        (r"(目录.*已存在|already exists|已经存在|已存在)", "directory_exists"),
        (r"(cannot import|no module|ModuleNotFoundError|ImportError|导入错误)", "import_error"),
        (r"(timeout|超时|timed out|连接超时)", "timeout"),
        (r"(permission|权限|access denied|拒绝访问)", "permission_denied"),
        (r"(syntax.*error|name.*error|attribute.*error|语法错误)", "code_error"),
        (r"(浏览器.*未启动|browser.*not.*launch|page.*not.*found)", "browser_not_ready"),
        (r"(cookie|logo|captcha|验证|登录)", "auth_required"),
        (r"(编码|encode|decode|乱码|gbk|utf)", "encoding_error"),
    ]

    # 任务类别关键词 → 标签（顺序即优先级：先命中先归类）
    #
    # ⚠️ 这张表之前**漏掉了整个开发与视频词汇**：实测 7 个典型目标
    # （"修 pytest 报的 bug"、"重构 memory.py 的召回逻辑"、"配音+字幕合成视频"…）
    # **全部**落到 general，而库里 117/159 条都是 general —— 于是"同类别加权"
    # （W_CATEGORY）形同虚设，召回退化成 2-gram 关键词硬凑。
    # 补词表时注意：只放**有区分度**的词，别把"写""做"这类放进 coding。
    TASK_CLASSIFIERS = [
        (["安装", "pip", "install", "配置", "部署", "setup"], "setup"),
        (["excel", "表格", "xlsx", "csv", "单元格", "工作表"], "file_excel"),
        # 视频/音频制作（本仓库高频任务，之前完全没有类别）
        (["视频", "剪辑", "配音", "字幕", "漫剧", "镜头", "运镜", "合成", "音频",
          "语音", "tts", "生图", "配音员", "成片", "转场"], "video_media"),
        # 写作/校对（文章工坊那条线）
        (["文章", "润色", "校对", "审阅", "文案", "稿子", "写一篇", "写作", "排版"], "writing"),
        # 网上的事先判：像"用浏览器搜索这个报错的解法"里也含"报错"，
        # 若 coding 排在前面会被误判成编程任务（实测踩到）。
        (["浏览器", "网页", "搜索", "访问", "打开.*网", "浏览"], "web_browse"),
        # 编程/调试（"测试""用例""pytest""bug"这些最常见的说法之前一个都没有）
        (["测试", "用例", "单元测试", "pytest", "unittest", r"\btest\b", "bug", "报错",
          "异常", "修复", "修好", "调试", "重构", "函数", "代码", "编程", r"写.*程序",
          "脚本", "python", "接口", r"\bapi\b", "类型标注", "静态检查", "lint"], "coding"),
        (["word", "文档", "docx", "报告", "论文"], "file_document"),
        (["下载", "爬虫", "抓取", "数据采集", "scrape"], "web_scrape"),
        (["计算", "统计", "分析", "数据分析", "pandas", "图表"], "data_analysis"),
        (["文件", "目录", "文件夹", "复制", "移动", "删除", "重命名"], "file_ops"),
    ]

    #: 经验召回打分的权重与近期性半衰期。
    #: 用显式权重而不是把各项塞进 lexicographic 元组：后者无法表达"近期性只占
    #: 一部分分量"，也没法调参。关键词重叠是主因，所以权重远高于其它项。
    W_OVERLAP = 10.0          # 与经验 **goal** 每命中一个关键词
    W_SUMMARY = 2.0           # 与经验 **summary** 命中（次要信号，防通用词刷分）
    W_CATEGORY = 3.0          # 同一任务类别
    W_SUCCESS = 1.0           # 成功经验（失败经验也有参考价值，故只有 1 分）
    W_RECENCY = 2.0           # 近期性上限（乘以 0~1 的衰减系数）
    RECENCY_HALF_LIFE_DAYS = 30.0   # 30 天衰减一半

    def __init__(self, db_path: str = None, chat_id: str = None):
        """
        Args:
            db_path: 记忆数据根目录（默认锚定项目根 memory/，不依赖进程 cwd，
                     避免 cwd 漂移导致记忆写到 output/ 子目录造成分叉）
            chat_id: 当前对话 ID（如 conv-20260825-abc123）。
                     为 None 时使用 "default" 兼容旧行为。
                     长期记忆按 chat_id 分目录存储，实现对话间记忆隔离。
        """
        if db_path is None:
            db_path = os.path.join(PROJECT_ROOT, "memory")
        elif not os.path.isabs(db_path):
            db_path = os.path.abspath(os.path.join(PROJECT_ROOT, db_path))
        self.chat_id = chat_id or "default"
        self.db_path = db_path
        # 按 chat_id 分目录存储长期记忆
        self.chat_db_path = os.path.join(db_path, self.chat_id)
        os.makedirs(self.chat_db_path, exist_ok=True)

        # 短期记忆
        self.conversation_history: list[MemoryEntry] = []
        self.step_history: list[dict] = []

        # 长期记忆
        self.long_term_memory: list[MemoryEntry] = []

        # 经验库
        self.experiences: list[ExperienceEntry] = []

        # 学习相关的容量与预算（全部来自 LEARN_CONFIG，不再写死在代码里）
        #   max_experiences  : 经验库容量，0 = 不限制
        #   recall_n         : 每次任务注入几条
        #   recall_pool      : 打分候选池，每类别各取最近 N 条，0 = 全量
        self.max_experiences = int(LEARN_CONFIG.get("max_experiences_store", 2000))
        self.recall_n = int(LEARN_CONFIG.get("max_experiences_recall", 3))
        self.recall_pool = int(LEARN_CONFIG.get("recall_pool", 200))
        # 相关性下限：没达到就少注入/不注入（"宁缺毋滥"），0 = 关闭
        self.min_overlap = int(LEARN_CONFIG.get("min_overlap", 1))
        # 写入侧策略阈值（见 should_record_experience）
        self.min_goal_chars = int(LEARN_CONFIG.get("min_goal_chars", 15))
        self.min_tool_calls = int(LEARN_CONFIG.get("min_tool_calls", 2))
        self.max_failure_patterns = int(LEARN_CONFIG.get("max_failure_patterns", 50))

        # 失败模式库
        self.failure_patterns: list[FailurePattern] = []

        # 策略库
        self.strategies: list[StrategyEntry] = []

        # 加载所有持久化数据
        self._load_all()

    # ================================================================
    # 短期记忆（未变）
    # ================================================================

    def add_message(self, role: str, content: str):
        self.conversation_history.append(
            MemoryEntry(role=role, content=content)
        )

    def get_messages(self) -> list[dict]:
        return [{"role": e.role, "content": e.content} for e in self.conversation_history]

    def get_recent_messages(self, n: int = 10) -> list[dict]:
        return [{"role": e.role, "content": e.content} for e in self.conversation_history[-n:]]

    def add_step_result(self, step: dict):
        step["timestamp"] = datetime.now().isoformat()
        self.step_history.append(step)

    def get_completed_steps(self) -> list[dict]:
        return [s for s in self.step_history if s.get("status") == "completed"]

    def get_failed_steps(self) -> list[dict]:
        return [s for s in self.step_history if s.get("status") == "failed"]

    def get_step_history_summary(self) -> str:
        if not self.step_history:
            return "暂无已执行步骤。"
        lines = []
        for i, s in enumerate(self.step_history, 1):
            status = s.get("status", "unknown")
            desc = s.get("step", f"步骤{i}")
            result = s.get("result", s.get("output", ""))
            lines.append(f"{i}. [{status}] {desc}")
            if result:
                lines.append(f"   结果: {result[:200]}")
        return "\n".join(lines)

    def clear_session(self):
        self.conversation_history.clear()
        self.step_history.clear()

    def clear_all(self):
        self.conversation_history.clear()
        self.step_history.clear()
        self.long_term_memory.clear()
        self.experiences.clear()
        self.failure_patterns.clear()
        self.strategies.clear()
        for fname in ["memory.json", "experiences.json", "failure_patterns.json", "strategies.json"]:
            fp = os.path.join(self.db_path, fname)
            if os.path.exists(fp):
                os.remove(fp)

    # ================================================================
    # 长期记忆
    # ================================================================

    def remember(self, content: str, role: str = "memory"):
        self.long_term_memory.append(MemoryEntry(role=role, content=content))
        # 超过上限时自动清理最旧的记忆
        if len(self.long_term_memory) > self.MAX_LONG_TERM:
            del self.long_term_memory[:-self.MAX_LONG_TERM]
        self._save_long_term()

    def _save_long_term(self):
        """持久化长期记忆（remember / prune_long_term 共用）。"""
        self._save_json("memory.json", [
            {"role": e.role, "content": e.content, "timestamp": e.timestamp}
            for e in self.long_term_memory
        ])

    def recall(self, keyword: str = None, n: int = 10) -> list[MemoryEntry]:
        if keyword:
            results = [e for e in self.long_term_memory if keyword.lower() in e.content.lower()]
        else:
            results = list(self.long_term_memory)
        return results[-n:]

    def prune_long_term(self, keep: int = 100):
        """
        清理长期记忆：只保留最新的 keep 条。

        keep <= 0 时清空全部长期记忆。
        返回被清理的条数。
        """
        keep = max(0, int(keep))
        removed = max(0, len(self.long_term_memory) - keep)
        if removed == 0:
            return 0
        if keep > 0:
            del self.long_term_memory[:-keep]
        else:
            self.long_term_memory.clear()
        self._save_long_term()
        return removed

    def summary(self) -> dict:
        """
        返回记忆模块的统计信息。
        """
        return {
            "chat_id": self.chat_id,
            "conversation_history": len(self.conversation_history),
            "step_history": len(self.step_history),
            "long_term_memory": len(self.long_term_memory),
            "experiences": len(self.experiences),
            "failure_patterns": len(self.failure_patterns),
            "strategies": len(self.strategies),
            "completed_steps": len(self.get_completed_steps()),
            "failed_steps": len(self.get_failed_steps()),
        }

    # ================================================================
    # 记忆嵌套：跨对话读取记忆（v3）
    # ================================================================

    def load_chat_memory(self, other_chat_id: str,
                          sources: list = None) -> dict:
        """
        读取另一个对话（chat_id）的记忆数据，注入到当前对话上下文中。

        这是"记忆嵌套"的核心方法：让当前对话能看到另一个对话的长期记忆、
        经验和失败模式，实现跨对话的知识复用。

        Args:
            other_chat_id: 目标对话 ID
            sources: 要加载的记忆来源列表。可选值：
                     "long_term"  — 长期记忆
                     "experiences" — 经验库
                     "failure_patterns" — 失败模式
                     "strategies"  — 策略库
                     默认加载全部。

        Returns:
            dict，包含各来源的摘要信息和注入的上下文文本。
        """
        if sources is None:
            sources = ["long_term", "experiences", "failure_patterns", "strategies"]

        other_dir = os.path.join(self.db_path, other_chat_id)
        if not os.path.isdir(other_dir):
            return {
                "chat_id": other_chat_id,
                "error": f"对话 {other_chat_id} 不存在或无记忆数据",
                "context_text": "",
            }

        result = {"chat_id": other_chat_id, "context_text": ""}
        parts = [f"\n## 来自对话 [{other_chat_id}] 的记忆\n"]

        if "long_term" in sources:
            raw = self._load_json("memory.json", chat_dir=other_dir)
            entries = [MemoryEntry(role=e.get("role", "memory"),
                                   content=e.get("content", ""),
                                   timestamp=e.get("timestamp", ""))
                       for e in raw]
            result["long_term_count"] = len(entries)
            for entry in entries:
                self.long_term_memory.append(entry)
            if entries:
                parts.append(f"\n### 长期记忆（{len(entries)} 条）\n")
                for e in entries[-20:]:
                    parts.append(f"- {e.content[:200]}\n")
                if len(entries) > 20:
                    parts.append(f"  ... 还有 {len(entries) - 20} 条\n")
            self._save_long_term()

        if "experiences" in sources:
            raw = self._load_json("experiences.json", chat_dir=other_dir)
            exp_entries = [
                ExperienceEntry(
                    goal=e.get("goal", ""), plan_steps=e.get("plan_steps", []),
                    success=e.get("success", False),
                    total_steps=e.get("total_steps", 0),
                    completed_steps=e.get("completed_steps", 0),
                    failed_steps=e.get("failed_steps", 0),
                    summary=e.get("summary", ""),
                    task_category=e.get("task_category", "general"),
                    tool_usage=e.get("tool_usage", {}),
                    errors=e.get("errors", []),
                    timestamp=e.get("timestamp", ""),
                )
                for e in raw
            ]
            result["experiences_count"] = len(exp_entries)
            existing_goals = {e.goal[:50] for e in self.experiences}
            new_count = 0
            for e in exp_entries:
                if e.goal[:50] not in existing_goals:
                    self.experiences.append(e)
                    existing_goals.add(e.goal[:50])
                    new_count += 1
            self._trim_experiences()
            if exp_entries:
                parts.append(f"\n### 经验库（{len(exp_entries)} 条，新增 {new_count} 条）\n")
                for e in exp_entries[-5:]:
                    status = "\u2713 成功" if e.success else "\u2717 失败"
                    parts.append(f"- [{status}] {e.goal[:120]}\n")

        if "failure_patterns" in sources:
            raw = self._load_json("failure_patterns.json", chat_dir=other_dir)
            fp_entries = [
                FailurePattern(
                    pattern_id=e.get("pattern_id", ""),
                    error_type=e.get("error_type", ""),
                    description=e.get("description", ""),
                    context_keywords=e.get("context_keywords", []),
                    avoidance_strategy=e.get("avoidance_strategy", ""),
                    occurrence_count=e.get("occurrence_count", 1),
                    last_seen=e.get("last_seen", ""),
                )
                for e in raw
            ]
            result["failure_patterns_count"] = len(fp_entries)
            if fp_entries:
                parts.append(f"\n### 失败模式（{len(fp_entries)} 条）\n")
                for fp in fp_entries[:5]:
                    parts.append(
                        f"- **{fp.error_type}** ({fp.occurrence_count} 次): "
                        f"{fp.avoidance_strategy}\n"
                    )

        if "strategies" in sources:
            raw = self._load_json("strategies.json", chat_dir=other_dir)
            st_entries = [
                StrategyEntry(
                    task_category=e.get("task_category", ""),
                    approach=e.get("approach", ""),
                    success_count=e.get("success_count", 0),
                    failure_count=e.get("failure_count", 0),
                    last_used=e.get("last_used", ""),
                )
                for e in raw
            ]
            result["strategies_count"] = len(st_entries)
            if st_entries:
                parts.append(f"\n### 策略库（{len(st_entries)} 条）\n")
                for s in st_entries[:5]:
                    parts.append(
                        f"- {s.approach} (成功率 {s.win_rate:.0%})\n"
                    )

        result["context_text"] = "\n".join(parts)
        return result

    def inject_chat_context(self, other_chat_id: str, n_messages: int = 10) -> str:
        """
        从 SessionStore 读取另一个对话的最近对话记录，注入当前对话上下文。

        与 load_chat_memory 的区别：
        - load_chat_memory: 加载长期记忆/经验/策略等结构化数据
        - inject_chat_context: 加载原始对话消息（user/assistant 交替）

        Args:
            other_chat_id: 目标对话 ID
            n_messages: 读取最近多少条消息

        Returns:
            格式化的对话上下文文本。
        """
        from agent.session import SessionStore
        store = SessionStore()
        data = store.load_conversation(other_chat_id)
        if not data or "messages" not in data:
            return f"\n## 对话 [{other_chat_id}] 记录\n无法加载对话 {other_chat_id}\n"

        messages = data["messages"][-n_messages:]
        if not messages:
            return f"\n## 对话 [{other_chat_id}] 记录\n该对话暂无消息记录\n"

        lines = [f"\n## 对话 [{other_chat_id}] 最近 {len(messages)} 条记录\n"]
        for m in messages:
            role = m.get("role", "unknown")
            msg_content = (m.get("content", "") or "")[:300]
            label = "用户" if role == "user" else "助手"
            lines.append(f"[{label}] {msg_content}\n")

        return "\n".join(lines)    # ================================================================
    # 经验库（自我进化核心）
    # ================================================================

    #: 像"问句/闲聊"的目标（不是任务）。**只用来判断短目标**：长目标里出现"如何"
    #: 往往是真任务（"如何让 agent 学会用知乎"），不能一刀切。
    _CHAT_HINT = re.compile(
        r"(吗|呢|什么|为什么|怎么|如何|哪个|哪些|能不能|可不可以|是不是|有没有|"
        r"^\s*(你|您)|谢谢|你好|\?|？)")

    def should_record_experience(self, goal: str, tool_usage: dict = None,
                                 success: bool = True, errors: list = None) -> bool:
        """这次任务值不值得沉淀成"经验"？（调用方在 save_experience 前问一句）

        为什么需要它（实测数据）：库里 159 条中有 59 条是"你能做什么""你现在用的
        什么模型""为什么停止了""还有什么需要升级迭代的地方吗"这类一句话问答。它们
        往往**确实调用过一两个工具**（列目录、看模型），所以能穿过"有没有干活"的
        判断，但毫无可复用价值；召回时还会靠一两个通用 bigram 挤进前 3 条，把提示词
        塞满噪音——这正是"召回看起来没用"的主因。

        规则（宁可少记，也别记没用的）：
          · 失败/踩坑的**总是记**（失败模式靠它）；
          · 工具调用 ≥ min_tool_calls（默认 2）→ 记，不管目标长短
            （"重构召回逻辑"这种短目标是真任务）；
          · 很短（< 8 字）→ 不记（"你能做什么""为什么停止了"）；
          · 像问句/闲聊 且 短于 min_goal_chars（默认 15）→ 不记。
        阈值由 LEARN_MIN_* 配置，设 0 分别关闭。
        """
        if not success or errors:
            return True                       # 失败/踩坑：必须留
        usage = tool_usage or {}
        calls = sum(int(v) for v in usage.values() if isinstance(v, (int, float)))
        min_calls = int(getattr(self, "min_tool_calls", 2) or 0)
        if min_calls and calls >= min_calls:
            return True
        text = (goal or "").strip()
        min_chars = int(getattr(self, "min_goal_chars", 15) or 0)
        if min_chars and len(text) < min_chars:
            # 短目标里只有"纯闲聊/问句"该丢；祈使句的小任务（"用python计算3加4"）照记。
            # 8 字以下无论像不像问句都不像任务。min_goal_chars=0 → 整段判断关闭。
            if len(text) < 8 or self._CHAT_HINT.search(text):
                return False
        return bool(usage) or bool(errors)

    def save_experience(self, goal: str, plan_steps: list, success: bool,
                         summary: str = "", tool_usage: dict = None,
                         errors: list = None) -> ExperienceEntry:
        """
        任务完成后自动保存经验。
        Agent 调用此方法后，经验将持久化并可在下次任务时召回。

        质量门槛：纯问答（无工具使用、无错误、且成功）不入库——经验库只
        沉淀"做了事"的记录（调用过工具 / 出过错 / 失败），避免闲聊问答
        稀释真正可复用的技能经验。

        （"任务值不值得沉淀"的**策略**判断在 should_record_experience()，
        由调用方在记录前问一句；这里是存储层，保持"给什么存什么"。）
        """
        tool_usage = tool_usage or {}
        errors = errors or []
        has_work = bool(tool_usage) or bool(errors) or not success
        if not has_work:
            # 纯问答且成功：无工具、无错误 → 不构成可复用经验
            return ExperienceEntry(
                goal=goal[:300], plan_steps=plan_steps, success=success,
                total_steps=len(plan_steps), completed_steps=0, failed_steps=0,
                summary=(summary or "")[:500], task_category="general",
                tool_usage={}, errors=[], timestamp=datetime.now().isoformat(),
            )

        completed = self.get_completed_steps()
        failed = self.get_failed_steps()

        task_category = self._classify_task(goal)
        entry = ExperienceEntry(
            goal=goal[:300],
            plan_steps=plan_steps,
            success=success,
            total_steps=len(plan_steps),
            completed_steps=len(completed),
            failed_steps=len(failed),
            summary=summary[:500],
            task_category=task_category,
            tool_usage=tool_usage or {},
            errors=errors or [],
        )

        self.experiences.append(entry)
        self._trim_experiences()

        self._save_json("experiences.json", [
            {
                "goal": e.goal, "plan_steps": e.plan_steps,
                "success": e.success, "total_steps": e.total_steps,
                "completed_steps": e.completed_steps, "failed_steps": e.failed_steps,
                "summary": e.summary, "task_category": e.task_category,
                "tool_usage": e.tool_usage, "errors": e.errors,
                "timestamp": e.timestamp,
            }
            for e in self.experiences
        ])

        return entry

    def _trim_experiences(self) -> None:
        """按 LEARN_MAX_STORE 裁剪经验库（0 = 不限制，文件留全量）。

        早先这里是硬编码的 `[-100:]`：库是滑动窗口，永远超不过 100 条，
        老经验被静默顶掉——而配置项 LEARN_MAX_STORE 定义了却没人读。
        容量与召回预算是两件事：库该留全量，只限制每次注入几条
        （recall_experiences 的 n，见 LEARN_MAX_RECALL）。
        """
        cap = int(getattr(self, "max_experiences", 0) or 0)
        if cap > 0 and len(self.experiences) > cap:
            self.experiences = self.experiences[-cap:]

    def recall_experiences(self, goal: str, n: int = None, llm=None,
                           use_llm_rank: bool = False) -> str:
        """
        根据当前目标，召回最相关的历史经验。

        默认用**轻量关键词相似度**排序（零额外 LLM 调用，快且稳定）：
        - 同类别经验优先
        - 关键词重叠越多排名越靠前
        - 同类同分时按时间倒序（最新优先）

        库容量与召回预算是两件事：库默认留全量（LEARN_MAX_STORE），
        每次只注入 n 条（LEARN_MAX_RECALL）。

        Args:
            goal: 当前任务目标
            n: 返回的经验数量；None = 用 LEARN_MAX_RECALL 配置值
            llm: 可选的 LLM 实例（仅当 use_llm_rank=True 时用于语义匹配）
            use_llm_rank: 是否用 LLM 做语义排序（慢，默认关闭；
                          开启时在主 LLM 慢速模型下会拖慢每次任务启动）

        Returns:
            格式化的经验文本，可直接注入 Planner 上下文。
        """
        if not self.experiences:
            return ""

        n = int(getattr(self, "recall_n", 3) if n is None else n)
        task_category = self._classify_task(goal)
        # 先按类别过滤
        same_category = [e for e in self.experiences if e.task_category == task_category]
        other = [e for e in self.experiences if e.task_category != task_category]

        # 同类优先：**各组内各自限流**，不能写成 (same_category + other)[-30:]。
        # 那个写法是"先拼接、再取末尾 30 条"，而同类别条目排在被拼接串的开头，
        # 于是被整段切掉——实测 3 条 coding + 97 条其它时，候选池里 coding 剩 0 条，
        # 与下面 _score 里"同类别加权"以及 docstring 声明的"同类别经验优先"
        # 完全相反（用真实 experiences.json 复现）。真正的相关度排序交给 _score。
        # 池大小可配（LEARN_RECALL_POOL，0 = 全量参与）——库留全量后由它压住打分成本。
        pool = int(getattr(self, "recall_pool", 0) or 0)
        if pool > 0:
            candidates = same_category[-pool:] + other[-pool:]
        else:
            candidates = same_category + other

        if not candidates:
            return ""

        # 可选：LLM 语义排序（慢，仅在显式开启时用）
        if use_llm_rank and llm and len(candidates) > n:
            try:
                candidates = self._llm_rank_experiences(llm, goal, candidates, n)
            except Exception:
                pass

        # 轻量相似度排序：按与目标的关键词重叠打分（无需 LLM）
        goal_keywords = set(self._extract_keywords(goal))

        def _recency(exp) -> float:
            """近期性：按半衰期指数衰减（0~1）。

            早先 timestamp 只当 tiebreaker，于是"半年前的经验"和"昨天的"在同类
            同分时只差在最后的字符串比较上——经历过一次坑的经验，三个月后仍然
            和刚踩过的等价。主流记忆系统（如 Generative Agents）都把近期性作为
            显式的加权项，这里照做。
            """
            try:
                then = datetime.fromisoformat(getattr(exp, "timestamp", "") or "")
            except (TypeError, ValueError):
                return 0.0
            days = max(0.0, (datetime.now() - then).total_seconds() / 86400.0)
            return 0.5 ** (days / self.RECENCY_HALF_LIFE_DAYS)

        def _goal_overlap(exp) -> int:
            """与经验 **goal** 的关键词重叠数（主信号）。"""
            return len(goal_keywords & set(self._extract_keywords(exp.goal)))

        def _score(exp) -> tuple:
            # goal 重叠是**主信号**，summary 重叠只作次要信号（权重低一档）。
            # 教训（本次实测踩到）：把两者并成一个集合等权算重叠，summary 里那些
            # 处处都有的通用词（"测试""项目""文件"）会把每条经验的 overlap 都抬到 2，
            # 排序反而更糊——等于给无关条目背书。
            sum_overlap = len(goal_keywords & set(
                self._extract_keywords(getattr(exp, "summary", "") or "", limit=60)))
            score = (self.W_OVERLAP * _goal_overlap(exp)
                     + self.W_SUMMARY * sum_overlap
                     + self.W_CATEGORY * (1 if exp.task_category == task_category else 0)
                     + self.W_SUCCESS * (1 if exp.success else 0)
                     + self.W_RECENCY * _recency(exp))
            # 分数相同时按时间倒序（新经验优先）——保留原有的确定性 tiebreak
            return (round(score, 6), exp.timestamp or "")

        if goal_keywords:
            candidates = sorted(candidates, key=_score, reverse=True)

            # 相关性下限（LEARN_MIN_OVERLAP，默认 1）：**只看与 goal 的重叠**，宁缺毋滥。
            # 库里 59/159 条是"视频任务你完成了吗"这类十几字的闲聊，靠一两个通用
            # bigram（"视频""agent"）就能挤进前 3 条，把提示词塞满噪音；一条都不重叠
            # 时**不如不注入**（调用方对空串已有处理）。summary 的通用词不算数——
            # 它证明不了"这件事我做过"。
            min_overlap = int(getattr(self, "min_overlap", 1) or 0)
            if min_overlap > 0:
                candidates = [e for e in candidates if _goal_overlap(e) >= min_overlap]
                if not candidates:
                    return ""

        # 取前 n 条（最相关在前；timestamp 参与打分，同类同分时新经验优先）
        selected = candidates[:n]

        # 格式化为可读文本
        lines = ["\n## 历史经验（来自之前的任务执行记录）\n"]
        for i, exp in enumerate(selected, 1):
            status = "✓ 成功" if exp.success else "✗ 失败"
            lines.append(
                f"### 经验 {i} [{status}] [{exp.task_category}]\n"
                f"- 任务: {exp.goal[:150]}\n"
                f"- 结果: {exp.summary[:200]}\n"
                f"- 完成 {exp.completed_steps}/{exp.total_steps} 步，失败 {exp.failed_steps} 步\n"
                f"- 使用工具: {', '.join(exp.tool_usage.keys()) if exp.tool_usage else '无'}\n"
            )
            if exp.errors:
                lines.append(f"- 遇到的错误: {'; '.join(exp.errors[:3])}\n")

        return "\n".join(lines)

    def _llm_rank_experiences(self, llm, goal: str, candidates: list, n: int) -> list:
        """用 LLM 对候选经验按相关性排序。"""
        if len(candidates) <= n:
            return candidates

        # 构建打分 prompt
        exp_text = "\n".join([
            f"[{i}] {e.goal[:120]} (类别: {e.task_category}, {'成功' if e.success else '失败'})"
            for i, e in enumerate(candidates)
        ])
        prompt = (
            f"当前任务: {goal}\n\n"
            f"以下是历史任务记录。请选出与当前任务最相关的 {n} 条，只输出编号列表，"
            f"如: 3, 7, 12, 1, 5\n\n{exp_text}"
        )
        try:
            messages = [
                {"role": "system", "content": "你是一个任务匹配助手。只输出编号，用逗号分隔。"},
                {"role": "user", "content": prompt},
            ]
            response = llm.chat(messages, max_tokens=50, temperature=0)
            nums = re.findall(r'\d+', response)
            indices = [int(x) for x in nums if 0 <= int(x) < len(candidates)]
            if indices:
                return [candidates[i] for i in indices[:n]]
        except Exception:
            pass
        return candidates

    # ================================================================
    # 失败模式库
    # ================================================================

    def learn_from_failure(self, failed_step: str, error_message: str):
        """
        从失败中学习：自动检测错误类型，更新或新建失败模式。
        多次遇到同一模式时，会自动生成规避策略。
        """
        error_type = self._classify_error(error_message)
        if error_type == "unknown":
            return

        # 查找已有模式
        existing = None
        for fp in self.failure_patterns:
            if fp.error_type == error_type:
                existing = fp
                break

        if existing:
            existing.occurrence_count += 1
            existing.last_seen = datetime.now().isoformat()
            # 更新上下文关键词
            for kw in self._extract_keywords(failed_step):
                if kw not in existing.context_keywords:
                    existing.context_keywords.append(kw)
        else:
            pattern = FailurePattern(
                pattern_id=f"{error_type}_{datetime.now().strftime('%Y%m%d')}",
                error_type=error_type,
                description=error_message[:200],
                context_keywords=self._extract_keywords(failed_step),
                avoidance_strategy=self._generate_avoidance(error_type),
            )
            self.failure_patterns.append(pattern)

        # 安全阀：按 error_type 去重的模式天然有界，这里只防极端情况；
        # 超限时丢"最久未见"的，保留一直在踩的那些（LEARN_MAX_PATTERNS，0 = 不限）
        cap = int(getattr(self, "max_failure_patterns", 0) or 0)
        if cap > 0 and len(self.failure_patterns) > cap:
            self.failure_patterns.sort(key=lambda f: f.last_seen or "")
            self.failure_patterns = self.failure_patterns[-cap:]

        self._save_failure_patterns()

    def get_failure_warnings(self, goal: str) -> str:
        """
        根据当前目标，返回相关的失败模式警告文本。
        可在执行前注入到 Executor 提示中。
        """
        if not self.failure_patterns:
            return ""

        task_kw = self._extract_keywords(goal)
        relevant = []
        for fp in self.failure_patterns:
            # 匹配任务关键词
            if any(kw in goal for kw in fp.context_keywords):
                relevant.append(fp)

        if not relevant:
            return ""

        # 按发生次数排序
        relevant.sort(key=lambda x: x.occurrence_count, reverse=True)

        lines = ["\n## 已知失败模式（避免重复犯错）\n"]
        for fp in relevant[:5]:
            lines.append(
                f"- **{fp.error_type}** (已发生 {fp.occurrence_count} 次)\n"
                f"  描述: {fp.description[:150]}\n"
                f"  规避策略: {fp.avoidance_strategy}\n"
            )

        return "\n".join(lines)

    def _classify_error(self, error_msg: str) -> str:
        """根据错误信息分类。"""
        for pattern, etype in self.ERROR_CLASSIFIERS:
            if re.search(pattern, error_msg, re.IGNORECASE):
                return etype
        return "unknown"

    def _generate_avoidance(self, error_type: str) -> str:
        """根据错误类型生成规避策略。"""
        strategies = {
            "command_failed": "使用 python 工具代替 terminal 命令；或检查命令语法是否正确",
            "file_not_found": "执行前先用 python os.path.exists() 检查文件是否存在",
            "directory_exists": "使用 os.makedirs(path, exist_ok=True) 创建目录，忽略已存在错误",
            "import_error": "先用 terminal 执行 pip install <missing_package> 安装缺失的包",
            "timeout": "增加超时时间；或分步执行，每步操作更小的范围",
            "permission_denied": "检查文件权限；使用管理员权限；或更换目标路径",
            "code_error": "先用 python 工具运行简化版代码测试；检查变量名拼写",
            "browser_not_ready": "在执行浏览器操作前先执行 launch 命令",
            "auth_required": "需要人工介入完成登录验证；或使用已保存的 cookie",
            "encoding_error": "文件读写时显式指定 encoding='utf-8'",
        }
        return strategies.get(error_type, "分析具体原因后调整方案")

    def _save_failure_patterns(self):
        self._save_json("failure_patterns.json", [
            {
                "pattern_id": fp.pattern_id, "error_type": fp.error_type,
                "description": fp.description, "context_keywords": fp.context_keywords,
                "avoidance_strategy": fp.avoidance_strategy,
                "occurrence_count": fp.occurrence_count, "last_seen": fp.last_seen,
            }
            for fp in self.failure_patterns
        ])

    # ================================================================
    # 策略库
    # ================================================================

    def record_strategy(self, task_category: str, approach: str, success: bool):
        """记录一次策略使用结果。"""
        for s in self.strategies:
            if s.task_category == task_category and s.approach == approach:
                if success:
                    s.success_count += 1
                else:
                    s.failure_count += 1
                s.last_used = datetime.now().isoformat()
                self._save_strategies()
                return

        entry = StrategyEntry(
            task_category=task_category,
            approach=approach,
            success_count=1 if success else 0,
            failure_count=0 if success else 1,
        )
        self.strategies.append(entry)
        self._save_strategies()

    def get_best_strategies(self, task_category: str, n: int = 3) -> str:
        """
        获取某类任务的最佳策略建议。
        """
        relevant = [s for s in self.strategies if s.task_category == task_category]
        if not relevant:
            return ""

        relevant.sort(key=lambda x: x.win_rate, reverse=True)
        selected = relevant[:n]

        lines = [f"\n## 推荐策略（{task_category} 类任务）\n"]
        for s in selected:
            lines.append(
                f"- **{s.approach}** "
                f"(成功率 {s.win_rate:.0%}, 共 {s.success_count + s.failure_count} 次)\n"
            )
        return "\n".join(lines)

    def _save_strategies(self):
        self._save_json("strategies.json", [
            {
                "task_category": s.task_category, "approach": s.approach,
                "success_count": s.success_count, "failure_count": s.failure_count,
                "last_used": s.last_used,
            }
            for s in self.strategies
        ])

    # ================================================================
    # 通用辅助方法
    # ================================================================

    @staticmethod
    def _classify_task(goal: str) -> str:
        """按关键词分类任务类型。"""
        goal_lower = goal.lower()
        for keywords, label in Memory.TASK_CLASSIFIERS:
            for kw in keywords:
                if re.search(kw, goal_lower):
                    return label
        return "general"

    #: 出现在 bigram 首/尾就说明它跨了词边界（「的经」「并给」「我优」），丢掉。
    #: ⚠️ 只能放**单字虚词**，而且要避开高频内容词的首/尾字，两类坑都踩过：
    #:   · 「已经」拆成单字会让 `经` 变虚词 → 内容词「经验」被整词丢掉；
    #:   · `用` 会让「用户」被丢掉（`使用` 也会）。
    #: 现在只保留明确无害的虚词；「需要/重要/存在/对比」这类词会被顺带滤掉，
    #: 这是有意的取舍——它们在目标描述里几乎没有区分度。
    #: 由 TestKeywordExtraction.test_content_words_survive_edge_filter 看守。
    _EDGE_STOPCHARS = frozenset("的了吗呢啊吧呀嘛哦和与及或并而但这那我你他她它们"
                                "请帮一下现在先再还也就都是正在到从对把被让给做"
                                "应该怎什为何如已要不想没")

    #: 中文虚词/连接词：它们几乎出现在任何目标里，当关键词只会制造"假重叠"
    _STOPWORDS = frozenset("""
        的 了 吗 呢 啊 吧 呀 和 与 及 或 以及 然后 而且 但是 不过 因为 所以 如果 那么
        这个 那个 我们 你们 他们 请 帮我 一下 现在 先 再 还 也 就 都 有 是 在 到 从
        对 把 被 让 给 用 做 需要 可以 应该 怎么 什么 为什么 如何 一个 进行 已经 没有
        the and for are was were been with that this from have has had not you your
    """.split())

    @staticmethod
    def _extract_keywords(text: str, limit: int = 12) -> list[str]:
        """提取用于相似度打分的关键词——**确定性且对中文有效**。

        早先实现是 `list(set(english + chinese))[:10]`，有两个真问题：
        1. 中文按 `[\\u4e00-\\u9fff]{2,}` 整段切，切出来的是「然后任务断了的情况啊」
           这种连续汉字段，不是词。两个不同任务几乎不可能共享整段——用真实库里
           100 条经验实测，关键词重叠 **100 条全为 0**。于是"相关性"这个主排序键
           等于失效，排序实际退化成 (类别, 成功, 时间)。
        2. `set` 顺序受 Python 哈希随机化影响（每进程随机，未设 PYTHONHASHSEED），
           `[:10]` 会**丢掉不同的词**：同一个长目标两次运行保留的关键词不是同一批，
           召回结果不可复现。

        现在：切段 → 英文小写 → 中文 2-gram（经典廉价 CJK 切分）+ 整段保留
        （≤6 字，让完全相同的短语精确命中）→ 丢掉跨词边界的 bigram 与停用词
        → **保序去重**，因此同一输入在任何进程都得到同一结果。
        """
        if not text:
            return []
        low = str(text).lower()
        tokens = list(re.findall(r"[a-z][a-z0-9_]+", low))
        for run in re.findall(r"[\u4e00-\u9fff]+", low):
            if len(run) <= 2:
                tokens.append(run)
                continue
            if len(run) <= 6:
                tokens.append(run)          # 短词组整段保留，便于精确匹配
            for i in range(len(run) - 1):
                bigram = run[i:i + 2]
                if bigram[0] in Memory._EDGE_STOPCHARS or bigram[1] in Memory._EDGE_STOPCHARS:
                    continue
                tokens.append(bigram)
        out, seen = [], set()
        for t in tokens:
            # 单字符（含中文单字）信息量太低，不参与打分
            if len(t) < 2 or t in seen or t in Memory._STOPWORDS:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= limit:
                break
        return out

    # ================================================================
    # 持久化
    # ================================================================

    def _save_json(self, filename: str, data, chat_dir: str = None):
        """原子保存数据到 JSON 文件（临时文件 + os.replace）。

        必须是原子的：长期记忆/经验库/策略库每次 remember/save_experience 都会
        整份重写，而旧实现直接 `open(filepath, "w")` 就地截断——进程在写入中途
        被 Ctrl+C / 断电打断，文件就只剩半截 JSON。更糟的是 `_load_json` 遇到
        解析失败静默 `return []`，下一次写入又只保留新条目：**历史数据静默全丢，
        且毫无提示**（实测把文件截断一半 → 加载 0 条且不报错）。
        agent/session.py 早已采用同款原子写法，这里补齐。
        """
        base = chat_dir or self.chat_db_path
        os.makedirs(base, exist_ok=True)
        filepath = os.path.join(base, filename)
        tmp = f"{filepath}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, filepath)     # 原子替换，失败时原文件完好
        except BaseException:
            # 必须捕 BaseException：真实的中断是 Ctrl+C（KeyboardInterrupt），
            # 它不是 Exception 的子类，只捕 Exception 会漏掉"最需要清理"的那种情况，
            # 临时文件残留在数据目录里。
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    def _load_json(self, filename: str, chat_dir: str = None) -> list:
        """从 JSON 文件加载数据；损坏时**改名保留**并告警，绝不静默清空。"""
        base = chat_dir or self.chat_db_path
        filepath = os.path.join(base, filename)
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            # 旧实现直接 return []：用户既不知道数据坏了，下一次写入还会把
            # 残存内容覆盖掉。这里保留现场（.corrupt）并明确告警。
            broken = f"{filepath}.corrupt"
            try:
                if os.path.exists(broken):
                    os.remove(broken)
                os.replace(filepath, broken)
            except OSError:
                broken = filepath
            try:
                print(f"[Memory] 警告: {filename} 解析失败（{e}），"
                      f"已保留为 {os.path.basename(broken)} 并以空数据继续。"
                      f"如需恢复请手工修复该文件。")
            except Exception:
                pass
            return []

    def _load_all(self):
        """加载所有持久化数据。"""
        # 长期记忆
        raw = self._load_json("memory.json")
        self.long_term_memory = [
            MemoryEntry(role=e.get("role", "memory"), content=e.get("content", ""),
                        timestamp=e.get("timestamp", ""))
            for e in raw
        ]
        # 经验库
        raw = self._load_json("experiences.json")
        self.experiences = [
            ExperienceEntry(
                goal=e.get("goal", ""), plan_steps=e.get("plan_steps", []),
                success=e.get("success", False), total_steps=e.get("total_steps", 0),
                completed_steps=e.get("completed_steps", 0),
                failed_steps=e.get("failed_steps", 0),
                summary=e.get("summary", ""), task_category=e.get("task_category", "general"),
                tool_usage=e.get("tool_usage", {}), errors=e.get("errors", []),
                timestamp=e.get("timestamp", ""),
            )
            for e in raw
        ]
        # 失败模式库
        raw = self._load_json("failure_patterns.json")
        self.failure_patterns = [
            FailurePattern(
                pattern_id=e.get("pattern_id", ""), error_type=e.get("error_type", ""),
                description=e.get("description", ""),
                context_keywords=e.get("context_keywords", []),
                avoidance_strategy=e.get("avoidance_strategy", ""),
                occurrence_count=e.get("occurrence_count", 1),
                last_seen=e.get("last_seen", ""),
            )
            for e in raw
        ]
        # 策略库
        raw = self._load_json("strategies.json")
        self.strategies = [
            StrategyEntry(
                task_category=e.get("task_category", ""),
                approach=e.get("approach", ""),
                success_count=e.get("success_count", 0),
                failure_count=e.get("failure_count", 0),
                last_used=e.get("last_used", ""),
            )
            for e in raw
        ]
