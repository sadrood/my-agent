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
    yield
