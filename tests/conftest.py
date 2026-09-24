"""pytest 全局隔离配置。

单元测试必须与外部运行时状态隔离，否则会变慢或产生副作用：

1. **`.env` 的 `EDIT_PREFLIGHT=true`**：会让测试里任何 `edit` 调用真的去跑
   一遍测试命令（实测单个测试被拖慢 30s+，且耗时随机器负载波动）。
2. **内嵌浏览器桥探测**：`ToolManager()` 每次创建都会 HTTP 探测本地桥，
   离线时要等连接超时（实测 1.7s/次）——几十个测试累积到分钟级。
3. **快照 / 逐操作 checkpoint**：可能在测试中产生 git 提交，污染仓库历史。

这里用 autouse fixture 统一关掉这些"会执行子进程 / 发网络请求 / 写仓库"
的重开关。需要验证这些功能本身的测试，自行 monkeypatch 开启即可
（`tests/test_patch_snapshot.py` 中的 preflight 测试就是这么做的）。
"""
import pytest

from config import TOOL_CONFIG


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    """放行 TestClient 的默认 Host（testserver）。

    dashboard 加了 Host 白名单来挡 DNS rebinding（只允许回环地址），而
    `TestClient(srv.app)` 默认发的是 `Host: testserver` → 全部 403。
    在测试里显式放行，生产默认仍然只认回环地址——不为测试方便而放宽生产策略。
    """
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "testserver")


def pytest_configure(config):
    """注册自定义标记。"""
    config.addinivalue_line(
        "markers",
        "real_paths: 该用例专门验证**真实路径锚定**（需要默认存储目录保持原样），"
        "跳过 _isolate_storage_dirs 的重定向",
    )


@pytest.fixture(autouse=True)
def _isolate_storage_dirs(request, monkeypatch, tmp_path):
    """把默认存储目录整体重定向到临时目录——测试**绝不能**写真实数据。

    实测出过两次事故：一次是会话测试裸用 `SessionStore()`（默认目录 = 真实
    memory/sessions），写进 5 个垃圾会话；另一次是冒烟脚本覆盖了
    memory/tasks.json。数据目录必须由 fixture 统一兜住，而不是指望每个测试
    自己记得传 tmp_path。

    做法：改 `SESSION_CONFIG["dir"]`（SessionStore 默认就读它）而不是设 `SESSION_DIR`
    环境变量——环境变量会被**子进程**继承，而 `test_path_anchoring` 里有一个用例
    专门起子进程验证"漂移启动也锚定项目根"，被继承了就测不到了。
    Memory 的默认根是模块级 PROJECT_ROOT，把它指到 tmp 即可（个别测试自己再
    patch 会覆盖本 fixture，互不影响）。

    验证"默认路径确实锚定项目根"的用例要加 `@pytest.mark.real_paths`
    ——那类断言看的就是真实根目录，被重定向反而测不到东西。
    """
    if request.node.get_closest_marker("real_paths"):
        yield
        return
    root = tmp_path / "store"

    try:
        from config import SESSION_CONFIG
        monkeypatch.setitem(SESSION_CONFIG, "dir", str(root / "sessions"))
    except Exception:
        pass

    try:
        import agent.memory as _mem
        monkeypatch.setattr(_mem, "PROJECT_ROOT", str(root))
    except Exception:
        pass

    try:
        import agent.tasks as _tasks
        monkeypatch.setattr(_tasks, "_MEM_DIR", str(root / "memory"))
        monkeypatch.setattr(_tasks, "_TASKS_FILE", str(root / "memory" / "tasks.json"))
        monkeypatch.setattr(_tasks, "_THOUGHTS_FILE", str(root / "memory" / "thoughts.json"))
    except Exception:
        pass

    try:
        import tools.todo as _todo
        monkeypatch.setattr(_todo, "_TODO_DIR", str(root / "memory" / "todos"))
    except Exception:
        pass

    yield


@pytest.fixture(autouse=True)
def _isolate_heavy_runtime_switches(monkeypatch):
    """默认关闭会触发真实副作用的运行时开关（测试可自行覆盖）。"""
    # edit 后不真跑测试：preflight 只应由 test_patch_snapshot 显式开启验证
    monkeypatch.setitem(TOOL_CONFIG, "edit_preflight", False)

    # 不探测内嵌浏览器桥（ToolManager 内是函数级导入，patch 模块属性即可生效）
    try:
        import tools.embedded_browser as _eb
        monkeypatch.setattr(_eb, "probe_bridge", lambda *a, **kw: False)
    except Exception:
        pass

    # 关掉监管者：它默认走**真实 LLM 端点** —— SUPERVISOR_CONFIG["model"] 留空时
    # 回退到硬编码的默认模型名，key/base 再回退 GUARDIAN_API_KEY /
    # GUARDIAN_BASE_URL。于是任何建 Agent 的测试都会真打外部 API（12 个测试文件
    # 都没关它），直接违反 AGENTS.md「不依赖网络的测试优先（FakeLLM 脚本化）」，
    # 还会烧真实额度。
    #
    # 更隐蔽的后果：监管者的 verdict 会决定主循环要不要多跑一轮 —— 真模型回
    # {"verdict": "continue"} 时循环继续，FakeLLM 的脚本已被前面两轮耗尽，
    # 于是 test_agent_loop 拿到 "（脚本耗尽）" 当最终答案而间歇失败（实测 ~25%，
    # 2026-09-23 定位）。
    #
    # 要验证监管者本身的测试，自行 monkeypatch 打开（test_supervisor.py 直接
    # 构造 Supervisor，不经过这里）。
    try:
        from config import SUPERVISOR_CONFIG
        monkeypatch.setitem(SUPERVISOR_CONFIG, "enabled", False)
    except Exception:
        pass

    # 同款模式的两处，一并关掉 —— 它们的构造器都是"配置里有 model 就建真 LLM"：
    #   · build_small_llm：SMALL_MODEL_CONFIG["model"] 非空即建真端点，被**会话标题**
    #     用到（`_save_session_if_requested` 的 title_fn）；
    #   · _build_guardian：GUARDIAN_* 与主 LLM 端点不同就建独立客户端，中高风险工具
    #     调用会真打出去。
    # 关掉之后测试套件才是真正的离线（AGENTS.md：不依赖网络的测试优先）。
    # 验证它们本身的测试自行覆盖打开（test_supervisor.py / test_small_model.py 等）。
    try:
        from config import SMALL_MODEL_CONFIG
        monkeypatch.setitem(SMALL_MODEL_CONFIG, "enabled", False)
    except Exception:
        pass
    try:
        from config import GUARDIAN_CONFIG
        monkeypatch.setitem(GUARDIAN_CONFIG, "enabled", False)
    except Exception:
        pass
    yield
