"""
会话持久化测试（thread/会话概念）。
"""
import os

from agent.session import (
    PROJECT_ROOT,
    SessionStore,
    _resolve_session_dir,
    sanitize_name,
    generate_conversation_id,
)


def test_sanitize_name():
    # 清理无改动的名字：原样返回，不带哈希
    assert sanitize_name("demo") == "demo"
    assert sanitize_name("conv-20260922-a1b2c3") == "conv-20260922-a1b2c3"
    assert sanitize_name("///") == "session"


def test_sanitize_name_disambiguates_after_cleaning():
    """清理会改变原名时补短哈希，否则不同的会话名会折叠成同一个文件。

    实测故障（2026-09-22 审计）：`调研: 2026` 与 `调研 2026`（以及 `a/b` 与 `a_b`）
    都映射到 `调研_2026.json`，而文件里存的 id 是**原始名** —— 后开的会话静默覆盖
    前一个，`/open` 打开的是别的内容。
    """
    a = sanitize_name("调研: 2026")
    b = sanitize_name("调研 2026")
    c = sanitize_name("a/b")
    d = sanitize_name("a_b")
    assert a != b, "不同的会话名不能映射到同一个文件名"
    assert c != d
    # 仍然是以清理结果为前缀的可读名字
    assert a.startswith("调研_2026") and b.startswith("调研_2026")
    # 稳定：同一名字多次调用结果一致（否则每次保存都会新建文件）
    assert sanitize_name("调研: 2026") == a


def test_generate_conversation_id():
    id1 = generate_conversation_id()
    id2 = generate_conversation_id()
    assert id1.startswith("conv-")
    assert id1 != id2


class TestCwdAnchor:
    """默认存储路径锚定项目根，cwd 漂移（如运行在 output/ 子目录）不再丢目录。"""

    def test_default_dir_anchored_to_project_root(self):
        d = _resolve_session_dir(None)
        assert os.path.isabs(d)
        assert d == os.path.join(PROJECT_ROOT, "memory", "sessions")

    def test_default_store_usable_when_cwd_drifted(self, tmp_path, monkeypatch):
        # 模拟进程 cwd 漂移到任意位置（tmp_path），默认配置下保存不失败
        monkeypatch.delenv("SESSION_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        store = SessionStore(config={"dir": "./memory/sessions"})
        assert os.path.isabs(store.dir)
        assert store.dir == os.path.join(PROJECT_ROOT, "memory", "sessions")
        path = store.save_conversation(
            "conv-cwd-drift", messages=[{"role": "user", "content": "hi"}]
        )
        assert os.path.exists(path)
        store.delete_conversation("conv-cwd-drift")

    def test_absolute_config_dir_respected(self, tmp_path):
        d = _resolve_session_dir({"dir": str(tmp_path)})
        assert d == str(tmp_path)

    def test_missing_dir_auto_created_on_save(self, tmp_path):
        # 目录不存在（甚至父目录也没有）时保存也应成功（写入前兜底 makedirs）
        store = SessionStore(config={"dir": str(tmp_path)})
        nested = os.path.join(str(tmp_path), "a", "b")
        store.dir = nested  # 模拟目录被删/从未创建
        path = store.save_conversation(
            "conv-mkdir", messages=[{"role": "user", "content": "hi"}]
        )
        assert os.path.exists(path)


class TestConversations:
    def test_save_load_full_history(self, tmp_path):
        """对话保存**全量**记录：60 条全部保留。"""
        store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 10})
        conv_id = "conv-20260824-abc123"
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
            for i in range(60)
        ]
        store.save_conversation(conv_id, messages=messages, last_summary="摘要")

        data = store.load_conversation(conv_id)
        assert data["id"] == conv_id
        assert len(data["messages"]) == 60      # 不截断
        assert data["messages"][0]["content"] == "消息0"
        assert data["messages"][-1]["content"] == "消息59"
        assert data["last_summary"] == "摘要"

    def test_title_from_first_user_message(self, tmp_path):
        store = SessionStore(config={"dir": str(tmp_path)})
        store.save_conversation("conv-x", messages=[
            {"role": "user", "content": "帮我写一个报告"},
            {"role": "assistant", "content": "好的"},
        ])
        data = store.load_conversation("conv-x")
        assert data["title"] == "帮我写一个报告"

    def test_title_preserved_on_update(self, tmp_path):
        store = SessionStore(config={"dir": str(tmp_path)})
        store.save_conversation("conv-x", messages=[{"role": "user", "content": "第一个问题"}])
        store.save_conversation("conv-x", messages=[
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "第二个问题"},
        ])
        data = store.load_conversation("conv-x")
        assert data["title"] == "第一个问题"     # 标题不被后续消息覆盖

    def test_model_binding_saved_and_preserved(self, tmp_path):
        """对话绑定模型：保存时记录，更新时保留（一对话一模型）。"""
        store = SessionStore(config={"dir": str(tmp_path)})
        store.save_conversation("conv-m", messages=[{"role": "user", "content": "hi"}],
                                model="model-a", base_url="http://a/v1")
        data = store.load_conversation("conv-m")
        assert data["model"] == "model-a"
        assert data["base_url"] == "http://a/v1"

        # 后续保存不带 model → 保留原绑定
        store.save_conversation("conv-m", messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "你好"},
        ])
        data2 = store.load_conversation("conv-m")
        assert data2["model"] == "model-a"

        # 显式切换 → 更新绑定
        store.save_conversation("conv-m", messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "你好"},
        ], model="model-b")
        assert store.load_conversation("conv-m")["model"] == "model-b"

    def test_list_and_delete(self, tmp_path):
        store = SessionStore(config={"dir": str(tmp_path)})
        store.save_conversation("conv-a", messages=[{"role": "user", "content": "A"}])
        store.save_conversation("conv-b", messages=[{"role": "user", "content": "B"}])
        convs = store.list_conversations()
        ids = {cv["id"] for cv in convs}
        assert ids == {"conv-a", "conv-b"}
        assert convs[0]["count"] == 1

        assert store.delete_conversation("conv-a") is True
        assert store.load_conversation("conv-a") is None
        assert store.delete_conversation("conv-a") is False


class TestConversationNameIsolation:
    """只差一个标点的会话名不能互相覆盖（用户可见的症状）。"""

    def test_similar_names_do_not_collide(self, tmp_path):
        store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 50})
        store.save_conversation("调研: 2026", [{"role": "user", "content": "冒号版"}])
        store.save_conversation("调研 2026", [{"role": "user", "content": "空格版"}])
        assert store.load_conversation("调研: 2026")["messages"][0]["content"] == "冒号版"
        assert store.load_conversation("调研 2026")["messages"][0]["content"] == "空格版"


class TestConcurrentSave:
    """并发保存同一会话不能互相踩（原子写的临时文件名必须每个写入者唯一）。

    实测故障（2026-09-22 审计）：临时文件名固定为 `path + ".tmp"`，而主 Agent 每轮
    整份重写会话、dashboard 侧任务同时做"读→追加→写回"，两者共用同一个临时文件 ——
    互相截断会产生两段 JSON 混杂的损坏文件，`_read_raw` 解析失败返回 None，
    `list_conversations` 跳过它，用户看到的是"整个对话凭空消失"。
    """

    def test_concurrent_save_keeps_file_readable(self, tmp_path):
        import threading

        store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 50})
        errors = []

        def writer(tag):
            try:
                for i in range(15):
                    store.save_conversation(
                        "conv-shared",
                        messages=[{"role": "user", "content": f"{tag}-{i}"}])
            except Exception as e:          # noqa: BLE001
                errors.append(f"{tag}: {e}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发保存抛异常: {errors}"
        # 关键：文件必须仍是可解析的合法 JSON —— 旧实现最坏就坏在这里
        data = store.load_conversation("conv-shared")
        assert data is not None and data["messages"], "会话文件损坏或为空"
        assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == [], "残留临时文件"


def test_save_load(tmp_path):
    store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 10})
    path = store.save("demo", {"goal": "测试", "messages": [{"role": "user", "content": "hi"}]})
    assert path.endswith("demo.json")

    data = store.load("demo")
    assert data["goal"] == "测试"
    assert data["messages"][0]["content"] == "hi"


def test_load_missing(tmp_path):
    store = SessionStore(config={"dir": str(tmp_path)})
    assert store.load("nope") is None


def test_list_and_delete(tmp_path):
    store = SessionStore(config={"dir": str(tmp_path)})
    store.save("a", {"x": 1})
    store.save("b", {"x": 2})
    names = [s["name"] for s in store.list_sessions()]
    assert set(names) == {"a", "b"}

    assert store.delete("a") is True
    assert store.load("a") is None
    assert store.delete("a") is False


def test_cleanup_caps_sessions(tmp_path):
    store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 3})
    for i in range(6):
        store.save(f"s{i}", {"i": i})
    # 每次保存后清理 → 最多保留 3 个
    assert len(store.list_sessions()) <= 3


class TestLoadOnceIdempotent:
    """回归：_load_session_if_requested 只加载一次且加载前清空，
    防止每轮 run 反复 append 历史 → conversation_history 指数膨胀
    （曾出现 540 万条消息 / 7.6GB 内存的雪崩）。"""

    def _make_agent(self, tmp_path):
        from agent.agent import Agent, AgentConfig
        from agent.memory import Memory

        cfg = AgentConfig(verbose=False)
        cfg.session_name = "conv-loadonce"
        cfg.rollout_enabled = False
        cfg.stream_enabled = False
        agent = Agent(config=cfg)
        # 用临时目录的 SessionStore，不触碰真实会话文件
        from agent.session import SessionStore
        agent.session_store = SessionStore(
            config={"dir": str(tmp_path), "max_sessions": 10}
        )
        agent.memory = Memory(db_path=str(tmp_path))
        return agent

    def test_loads_only_once(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.session_store.save_conversation(
            "conv-loadonce",
            messages=[
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
            last_summary="摘要",
        )
        agent._load_session_if_requested()
        first_count = len(agent.memory.conversation_history)
        assert first_count == 2

        # 第二次调用（模拟下一轮 run）→ 不再重复 append
        agent._load_session_if_requested()
        assert len(agent.memory.conversation_history) == first_count

    def test_clears_before_load(self, tmp_path):
        agent = self._make_agent(tmp_path)
        agent.session_store.save_conversation(
            "conv-loadonce",
            messages=[{"role": "user", "content": "历史消息"}],
        )
        # 模拟调用方（main.py 启动时）已手动加载过
        agent.memory.add_message("user", "main.py 预加载")
        agent._load_session_if_requested()
        contents = [m.content for m in agent.memory.conversation_history]
        assert "main.py 预加载" not in contents      # 加载前已清空
        assert contents == ["历史消息"]


class TestRestoreModelRespectsExplicitConfig:
    """回归：用户显式配置默认模型后，恢复对话不再被绑定模型覆盖
    （曾出现 .env 设 glm-5.2 却被旧对话绑定的 sensenova 覆盖）。"""

    def _make_agent(self, tmp_path, monkeypatch, model_env="glm-5.2"):
        from agent.agent import Agent, AgentConfig
        from agent.memory import Memory
        from agent.session import SessionStore

        # 注意：config.LLM_CONFIG 在模块导入时已缓存环境变量，
        # 此处 setenv 只为 _user_explicitly_set_default_model() 生效；
        # 实际默认模型通过 AgentConfig.llm_model 显式注入，避免缓存干扰。
        monkeypatch.setenv("LLM_DEFAULT_MODEL", model_env)
        monkeypatch.setenv("LLM_BASE_URL", "https://token.sensenova.cn/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")

        cfg = AgentConfig(verbose=False, llm_model=model_env)
        cfg.session_name = "conv-bound"
        cfg.rollout_enabled = False
        cfg.stream_enabled = False
        agent = Agent(config=cfg)
        agent.session_store = SessionStore(config={"dir": str(tmp_path), "max_sessions": 10})
        agent.memory = Memory(db_path=str(tmp_path))
        return agent

    def test_explicit_config_not_overridden(self, tmp_path, monkeypatch):
        agent = self._make_agent(tmp_path, monkeypatch)
        agent.session_store.save_conversation(
            "conv-bound",
            messages=[{"role": "user", "content": "hi"}],
            model="sensenova-6.8-flash-lite",
            base_url="https://token.sensenova.cn/v1",
        )
        data = agent.session_store.load_conversation("conv-bound")
        agent._restore_conversation_model(data)
        # 显式配置了 glm-5.2 → 不被绑定模型覆盖
        assert agent.llm.default_model == "glm-5.2"

    def test_no_explicit_config_restores_bound_model(self, tmp_path, monkeypatch):
        # 用户没显式配模型（清空全部模型 env 变量）→ 恢复对话自动切回绑定模型
        agent = self._make_agent(tmp_path, monkeypatch)
        for k in ("MY_AGENT_MODEL", "ANTHROPIC_MODEL", "LLM_DEFAULT_MODEL"):
            monkeypatch.delenv(k, raising=False)
        agent.session_store.save_conversation(
            "conv-bound",
            messages=[{"role": "user", "content": "hi"}],
            model="sensenova-6.8-flash-lite",
            base_url="https://token.sensenova.cn/v1",
        )
        data = agent.session_store.load_conversation("conv-bound")
        agent._restore_conversation_model(data)
        assert agent.llm.default_model == "sensenova-6.8-flash-lite"
