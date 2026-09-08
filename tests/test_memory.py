"""测试记忆模块（Memory v2）。"""
import os
import shutil
import tempfile

from agent.memory import (
    Memory, MemoryEntry, ExperienceEntry,
    FailurePattern, StrategyEntry,
)


def _make_memory():
    """创建带独立临时目录的 Memory 实例。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    return Memory(db_path=tmp), tmp


def test_add_and_get_messages():
    m, tmp = _make_memory()
    m.add_message("user", "你好")
    m.add_message("assistant", "你好！")
    msgs = m.get_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["content"] == "你好！"
    shutil.rmtree(tmp)


def test_get_recent_messages():
    m, tmp = _make_memory()
    for i in range(10):
        m.add_message("user", f"消息{i}")
    recent = m.get_recent_messages(3)
    assert len(recent) == 3
    assert recent[0]["content"] == "消息7"
    shutil.rmtree(tmp)


def test_add_step_result():
    m, tmp = _make_memory()
    m.add_step_result({"step": "安装依赖", "status": "completed", "result": "成功"})
    m.add_step_result({"step": "启动服务", "status": "failed", "result": "端口被占用"})
    assert len(m.get_completed_steps()) == 1
    assert len(m.get_failed_steps()) == 1
    shutil.rmtree(tmp)


def test_get_step_history_summary():
    m, tmp = _make_memory()
    summary = m.get_step_history_summary()
    assert "暂无" in summary
    m.add_step_result({"step": "下载文件", "status": "completed", "result": "ok"})
    summary = m.get_step_history_summary()
    assert "下载文件" in summary
    assert "completed" in summary
    shutil.rmtree(tmp)


def test_clear_session():
    m, tmp = _make_memory()
    m.add_message("user", "hello")
    m.add_step_result({"step": "s1", "status": "completed"})
    m.clear_session()
    assert len(m.conversation_history) == 0
    assert len(m.step_history) == 0
    shutil.rmtree(tmp)


def test_remember_and_recall():
    m, tmp = _make_memory()
    m.remember("用户偏好使用 Python")
    m.remember("项目路径在 /home/user")
    results = m.recall(n=5)
    assert len(results) == 2
    kw_results = m.recall(keyword="Python")
    assert len(kw_results) == 1
    assert "Python" in kw_results[0].content
    shutil.rmtree(tmp)


def test_remember_size_cap():
    m, tmp = _make_memory()
    m.MAX_LONG_TERM = 5
    for i in range(10):
        m.remember(f"记忆{i}")
    assert len(m.long_term_memory) == 5
    # 确认保留的是最新的 5 条
    assert m.long_term_memory[0].content == "记忆5"
    shutil.rmtree(tmp)


def test_prune_long_term():
    m, tmp = _make_memory()
    for i in range(20):
        m.remember(f"记忆{i}")
    m.MAX_LONG_TERM = 20  # 不触发自动截断
    removed = m.prune_long_term(keep=10)
    assert removed == 10
    assert len(m.long_term_memory) == 10
    assert m.long_term_memory[0].content == "记忆10"
    # 再次 prune 时不应再移除
    removed2 = m.prune_long_term(keep=10)
    assert removed2 == 0
    shutil.rmtree(tmp)


def test_prune_long_term_keep_zero_or_negative():
    # keep=0 必须清空（回归：[-0:] 切片等于 [0:]，曾导致"声称清了实际全留"）
    m, tmp = _make_memory()
    for i in range(10):
        m.remember(f"记忆{i}")
    removed = m.prune_long_term(keep=0)
    assert removed == 10
    assert len(m.long_term_memory) == 0

    # keep 为负同样按清空处理，且 removed 不能超过实际条数
    for i in range(5):
        m.remember(f"记忆{i}")
    removed = m.prune_long_term(keep=-3)
    assert removed == 5
    assert len(m.long_term_memory) == 0
    shutil.rmtree(tmp)


def test_save_and_recall_experience():
    m, tmp = _make_memory()
    m.add_step_result({"step": "s1", "status": "completed"})
    entry = m.save_experience(
        goal="写一个 Excel 报表",
        plan_steps=["读取数据", "写入 Excel"],
        success=True,
        summary="成功生成报表",
        tool_usage={"python": 3},
        errors=[],
    )
    assert entry.success is True
    assert entry.task_category == "file_excel"
    assert len(m.experiences) == 1
    shutil.rmtree(tmp)


def test_recall_experiences():
    m, tmp = _make_memory()
    m.save_experience(goal="用 Python 生成 Excel", plan_steps=["s1"], success=True,
                      summary="ok", tool_usage={"python": 2})
    m.save_experience(goal="用浏览器搜索网页", plan_steps=["s1"], success=True,
                      summary="ok", tool_usage={"browser": 1})
    ctx = m.recall_experiences("生成一个 Excel 表格", n=3)
    assert "Excel" in ctx or "excel" in ctx.lower()
    assert "历史经验" in ctx
    shutil.rmtree(tmp)


def test_experience_size_limit():
    m, tmp = _make_memory()
    for i in range(110):
        m.save_experience(goal=f"任务{i}", plan_steps=["s1"], success=True,
                          tool_usage={"terminal": 1})
    assert len(m.experiences) == 100
    shutil.rmtree(tmp)


def test_learn_from_failure():
    m, tmp = _make_memory()
    m.learn_from_failure("执行 pip install", "命令返回码: 1")
    assert len(m.failure_patterns) == 1
    assert m.failure_patterns[0].error_type == "command_failed"

    # 再次遇到同类错误应累加计数
    m.learn_from_failure("执行 pip install 2", "命令返回码: 1")
    assert m.failure_patterns[0].occurrence_count == 2
    shutil.rmtree(tmp)


def test_get_failure_warnings():
    m, tmp = _make_memory()
    m.learn_from_failure("download webpage content", "timeout: connection timeout")
    warnings = m.get_failure_warnings("download webpage")
    assert "timeout" in warnings
    shutil.rmtree(tmp)


def test_record_and_get_strategies():
    m, tmp = _make_memory()
    m.record_strategy("file_excel", "python+openpyxl", True)
    m.record_strategy("file_excel", "python+openpyxl", True)
    m.record_strategy("file_excel", "启动 Excel GUI", False)
    best = m.get_best_strategies("file_excel", n=3)
    assert "python+openpyxl" in best
    assert "100%" in best
    shutil.rmtree(tmp)


def test_strategy_win_rate():
    m, tmp = _make_memory()
    for _ in range(3):
        m.record_strategy("coding", "python", True)
    for _ in range(1):
        m.record_strategy("coding", "python", False)
    relevant = [s for s in m.strategies if s.approach == "python"]
    assert relevant[0].win_rate == 0.75
    shutil.rmtree(tmp)


def test_classify_task():
    assert Memory._classify_task("安装 pandas") == "setup"
    assert Memory._classify_task("生成 Excel 报表") == "file_excel"
    assert Memory._classify_task("用浏览器搜索") == "web_browse"
    assert Memory._classify_task("写一个函数") == "coding"
    assert Memory._classify_task("随便做点什么") == "general"


class _NoLLM:
    """若被调用则抛错：证明轻量召回不依赖 LLM。"""

    def chat(self, *a, **k):
        raise AssertionError("轻量召回不应调用 LLM")


def test_recall_without_llm_uses_lightweight_rank():
    """默认召回不调用 LLM（回归：旧实现每次任务启动都用主 LLM 排序，
    glm-5.2 单轮 25-50s 拖慢启动且结果不稳定）。"""
    m, tmp = _make_memory()
    m.save_experience(goal="用 Python 生成 Excel 报表", plan_steps=["s1"], success=True,
                      summary="用 openpyxl", tool_usage={"python": 2})
    m.save_experience(goal="用浏览器搜索网页", plan_steps=["s1"], success=True,
                      summary="用 browser", tool_usage={"browser": 1})
    ctx = m.recall_experiences("生成一个 Excel 表格", n=3, llm=_NoLLM())
    assert "历史经验" in ctx
    assert "Excel" in ctx or "excel" in ctx.lower()
    shutil.rmtree(tmp)


def test_recall_ranks_related_experience_first():
    """轻量相似度排序：与目标关键词重叠多的经验应排最前。"""
    m, tmp = _make_memory()
    m.save_experience(goal="帮我把数据整理成 Excel 表格", plan_steps=["s1"], success=True,
                      summary="openpyxl 生成", tool_usage={"python": 1})
    m.save_experience(goal="部署服务到服务器", plan_steps=["s1"], success=True,
                      summary="ssh 部署", tool_usage={"terminal": 1})
    ctx = m.recall_experiences("整理数据生成 Excel", n=2)
    # Excel 经验应在部署经验之前
    excel_pos = ctx.find("Excel")
    deploy_pos = ctx.find("部署")
    assert 0 <= excel_pos < deploy_pos
    shutil.rmtree(tmp)


def test_classify_error():
    m, tmp = _make_memory()
    assert m._classify_error("找不到文件 foo.txt") == "file_not_found"
    assert m._classify_error("ModuleNotFoundError: No module named 'xxx'") == "import_error"
    assert m._classify_error("timeout: 连接超时") == "timeout"
    assert m._classify_error("permission denied") == "permission_denied"
    assert m._classify_error("something weird happened") == "unknown"
    shutil.rmtree(tmp)


def test_extract_keywords():
    kws = Memory._extract_keywords("write a Python Excel report script")
    assert "Python" in kws or "python" in [k.lower() for k in kws]
    assert len(kws) <= 10


def test_persistence_roundtrip():
    tmp = tempfile.mkdtemp(prefix="mem_persist_")
    m = Memory(db_path=tmp)
    m.remember("持久化测试")
    m.save_experience(goal="测试持久化", plan_steps=["s1"], success=True,
                      tool_usage={"terminal": 1})
    m.learn_from_failure("某步骤", "命令返回码: 1")
    m.record_strategy("general", "test", True)

    # 重新加载
    m2 = Memory(db_path=tmp)
    assert len(m2.long_term_memory) == 1
    assert len(m2.experiences) == 1
    assert len(m2.failure_patterns) == 1
    assert len(m2.strategies) == 1

    shutil.rmtree(tmp)


def test_clear_all():
    m, tmp = _make_memory()
    m.remember("test")
    m.save_experience(goal="g", plan_steps=[], success=True, tool_usage={"terminal": 1})
    m.learn_from_failure("s", "error")
    m.record_strategy("x", "y", True)
    m.clear_all()
    assert len(m.long_term_memory) == 0
    assert len(m.experiences) == 0
    assert len(m.failure_patterns) == 0
    assert len(m.strategies) == 0
    # 文件也应被删除
    for fname in ["memory.json", "experiences.json", "failure_patterns.json", "strategies.json"]:
        assert not os.path.exists(os.path.join(tmp, fname))
    shutil.rmtree(tmp)


def test_summary():
    m, tmp = _make_memory()
    m.add_message("user", "hi")
    m.add_step_result({"step": "s1", "status": "completed"})
    m.remember("r1")
    m.save_experience(goal="g", plan_steps=["s1"], success=True, tool_usage={"terminal": 1})
    m.learn_from_failure("some step", "command return code: 1")
    m.record_strategy("x", "y", True)

    s = m.summary()
    assert s["conversation_history"] == 1
    assert s["step_history"] == 1
    assert s["long_term_memory"] == 1
    assert s["experiences"] == 1
    assert s["failure_patterns"] == 1
    assert s["strategies"] == 1
    assert s["completed_steps"] == 1
    assert s["failed_steps"] == 0
    shutil.rmtree(tmp)


def test_failure_pattern_keywords_accumulate():
    m, tmp = _make_memory()
    m.learn_from_failure("安装 pandas 包", "命令返回码: 1")
    m.learn_from_failure("安装 numpy 库", "命令返回码: 1")
    fp = m.failure_patterns[0]
    # 两次不同上下文，关键词应被合并
    assert len(fp.context_keywords) >= 2
    shutil.rmtree(tmp)


# ================================================================
# 记忆嵌套测试（v3）
# ================================================================

def test_chat_id_isolation():
    """不同 chat_id 的记忆数据应隔离存储。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m1 = Memory(db_path=tmp, chat_id="chat-A")
        m2 = Memory(db_path=tmp, chat_id="chat-B")

        m1.remember("A 的记忆")
        m2.remember("B 的记忆")

        assert len(m1.long_term_memory) == 1
        assert m1.long_term_memory[0].content == "A 的记忆"
        assert len(m2.long_term_memory) == 1
        assert m2.long_term_memory[0].content == "B 的记忆"

        # 确认文件分目录
        assert os.path.isdir(os.path.join(tmp, "chat-A"))
        assert os.path.isdir(os.path.join(tmp, "chat-B"))
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_long_term():
    """load_chat_memory 能读取另一个对话的长期记忆。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        # 先创建一个对话的记忆
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.remember("other 的偏好：用 Python")
        m_other.remember("other 的路径：/data")

        # 当前对话
        m_current = Memory(db_path=tmp, chat_id="current-chat")
        m_current.remember("current 自己的记忆")

        # 加载另一个对话的记忆
        result = m_current.load_chat_memory("other-chat", sources=["long_term"])

        assert result["chat_id"] == "other-chat"
        assert result["long_term_count"] == 2
        # 当前对话现在应该有 3 条长期记忆（1 条自己的 + 2 条加载的）
        assert len(m_current.long_term_memory) == 3
        assert "other 的偏好" in m_current.long_term_memory[1].content
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_nonexistent():
    """加载不存在的对话应返回 error。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m = Memory(db_path=tmp, chat_id="current")
        result = m.load_chat_memory("nonexistent-chat")
        assert "error" in result
        assert result["context_text"] == ""
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_experiences():
    """load_chat_memory 能读取另一个对话的经验库。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.save_experience(
            goal="用 Python 生成 Excel 报表",
            plan_steps=["读取数据", "写入 Excel"],
            success=True,
            summary="成功",
            tool_usage={"python": 2},
        )
        m_other.save_experience(
            goal="用浏览器搜索网页",
            plan_steps=["打开浏览器", "搜索"],
            success=True,
            summary="成功",
            tool_usage={"browser": 1},
        )

        m_current = Memory(db_path=tmp, chat_id="current-chat")
        result = m_current.load_chat_memory("other-chat", sources=["experiences"])

        assert result["experiences_count"] == 2
        assert len(m_current.experiences) == 2
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_dedup_experiences():
    """加载经验时按 goal 前 50 字符去重。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.save_experience(
            goal="用 Python 生成 Excel 报表",
            plan_steps=["s1"], success=True,
            tool_usage={"python": 1},
        )

        m_current = Memory(db_path=tmp, chat_id="current-chat")
        m_current.save_experience(
            goal="用 Python 生成 Excel 报表",  # 相同 goal
            plan_steps=["s1"], success=True,
            tool_usage={"python": 1},
        )

        result = m_current.load_chat_memory("other-chat", sources=["experiences"])
        # 去重后应该只有 1 条
        assert len(m_current.experiences) == 1
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_failure_patterns():
    """load_chat_memory 能读取另一个对话的失败模式。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.learn_from_failure("执行 pip install", "命令返回码: 1")
        m_other.learn_from_failure("执行 git clone", "timeout")

        m_current = Memory(db_path=tmp, chat_id="current-chat")
        result = m_current.load_chat_memory("other-chat", sources=["failure_patterns"])

        assert result["failure_patterns_count"] == 2
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_strategies():
    """load_chat_memory 能读取另一个对话的策略库。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.record_strategy("coding", "python脚本", True)
        m_other.record_strategy("coding", "terminal命令", False)

        m_current = Memory(db_path=tmp, chat_id="current-chat")
        result = m_current.load_chat_memory("other-chat", sources=["strategies"])

        assert result["strategies_count"] == 2
    finally:
        shutil.rmtree(tmp)


def test_load_chat_memory_all_sources():
    """load_chat_memory 默认加载所有来源。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m_other = Memory(db_path=tmp, chat_id="other-chat")
        m_other.remember("some memory")
        m_other.save_experience(goal="test task", plan_steps=["s1"], success=True,
                                tool_usage={"terminal": 1})
        m_other.learn_from_failure("step", "command failed")
        m_other.record_strategy("coding", "python", True)

        m_current = Memory(db_path=tmp, chat_id="current-chat")
        result = m_current.load_chat_memory("other-chat")

        assert result["long_term_count"] == 1
        assert result["experiences_count"] == 1
        assert result["failure_patterns_count"] == 1
        assert result["strategies_count"] == 1
        assert "来自对话" in result["context_text"]
    finally:
        shutil.rmtree(tmp)


def test_summary_includes_chat_id():
    """summary() 应包含 chat_id 字段。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m = Memory(db_path=tmp, chat_id="conv-20260825-abc")
        m.remember("test")
        s = m.summary()
        assert s["chat_id"] == "conv-20260825-abc"
        assert s["long_term_memory"] == 1
    finally:
        shutil.rmtree(tmp)


def test_default_chat_id_backward_compat():
    """不传 chat_id 时应使用 "default" 兼容旧行为。"""
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    try:
        m = Memory(db_path=tmp)
        assert m.chat_id == "default"
        assert m.chat_db_path == os.path.join(tmp, "default")
        m.remember("old style memory")
        assert len(m.long_term_memory) == 1
    finally:
        shutil.rmtree(tmp)
