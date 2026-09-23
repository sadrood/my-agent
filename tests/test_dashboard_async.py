"""静态守卫：async 路由里不许直接调用同步阻塞函数。

背景（2026-09-23 审计）：`dashboard/server.py` 里多个 `async def` 路由直接做同步
阻塞调用 —— `subprocess.run(git …)`（最长 15s）、`fut.result(timeout=60)`、
真实 LLM / 视觉模型调用（**原本没有任何超时**）、递归遍历整个工作区。

后果不是"慢一点"：uvicorn 是单事件循环，一次阻塞会把 **`/ws` 的事件推送**和
**`/api/stop`、`/api/approve`** 一起排住 —— 前端表现为"任务卡住、点停止没反应"。

修法是把它们挪进 `asyncio.to_thread(...)`。这条测试把"以后新加的路由别再犯"
钉住：它按 AST 找 async 函数里的已知阻塞调用，豁免两种写法 ——
  · `await asyncio.to_thread(fn, ...)` 里直接包的；
  · `await asyncio.to_thread(_nested_def)`，阻塞调用在那个嵌套函数体里的。

匹配分两类，**都按"限定名"判定而不是只看尾名**（否则误报会把守卫变成噪声，
最后没人看）：
  · 尾名类（`BLOCKING`）：`run` / `chat` / `analyze` 这些尾名本身就说明问题；
  · 限定类：裸 `open()`、`os.makedirs` 等（`BLOCKING_OS`，接收者必须是 `os`）、
    `p.read_text()` 等 pathlib 读写（`BLOCKING_PATHLIB`）、
    `json.load` / `_json.dump`（`BLOCKING_JSON`，接收者以 `json` 结尾，认别名）。

**故意豁免**（写在这里，免得以后有人问"为什么它没报"）：
  · `os.path.exists/isfile/isdir` 之类的单次 stat —— 一次元数据系统调用，
    量级是微秒，挪线程的调度开销比它本身还大；
  · `img.save(BytesIO)`（api_config_test_vision）—— 写到内存缓冲，不碰磁盘。
"""
import ast
import os

import pytest

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "dashboard", "server.py")

#: 已知会阻塞的调用名（同步 IO / 子进程 / 网络 / 全盘遍历）—— 按尾名匹配
BLOCKING = {
    "run", "check_output", "call", "Popen",   # subprocess
    "result",                                  # concurrent Future
    "chat", "create",                          # LLM / OpenAI 客户端
    "analyze",                                 # 视觉模型
    "search_files", "search_sessions",         # 文件系统
    "_build_tree",                             # 递归遍历工作区
    "urlopen", "urlretrieve",                  # urllib
}

#: `os.*` 里的文件**内容**读写（`os.path.*` 是元数据 stat，故意不收）
BLOCKING_OS = {"makedirs", "remove", "unlink", "rename", "replace"}

#: pathlib 的文本/字节读写 —— 接收者是变量（`p.read_text()`）而不是 `os`，
#: 没法限定在 `os` 上；这几个名字本身足够特异，按尾名收即可。
BLOCKING_PATHLIB = {"read_text", "write_text", "read_bytes", "write_bytes"}

#: `json.load` / `json.dump`（接收者以 json 结尾，所以 `_json` 这类别名也认）
BLOCKING_JSON = {"load", "dump"}


def _is_to_thread(node) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread")


def _receiver(func) -> str:
    """把 `a.b.c(…)` 的接收者还原成 `a.b`；裸函数名（如 `open(…)`）返回 ""。"""
    if not isinstance(func, ast.Attribute):
        return ""
    parts, node = [], func.value
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _blocking_name(call) -> str:
    """这个调用是否阻塞？是则返回"钉在报错里的名字"，否则返回 ""。"""
    func = call.func
    attr = getattr(func, "attr", None)
    if attr is None:                       # 裸函数名：只认内置 open()
        return "open" if getattr(func, "id", None) == "open" else ""
    if attr in BLOCKING:
        return attr
    recv = _receiver(func)
    if attr in BLOCKING_OS and recv == "os":
        return f"os.{attr}"
    if attr in BLOCKING_PATHLIB:
        return f"{recv}.{attr}"
    if attr in BLOCKING_JSON and recv.rsplit(".", 1)[-1].lower().endswith("json"):
        return f"{recv}.{attr}"
    return ""


def _exempted(fn):
    """收集 async 函数体里"已交给线程"的节点 id。

    两种写法都豁免：`to_thread(f())` 里直接包的表达式；`to_thread(_nested)`
    把整个嵌套函数体交给线程的。
    """
    safe, deferred = set(), set()
    for sub in ast.walk(fn):
        if not _is_to_thread(sub):
            continue
        for arg in sub.args:
            if isinstance(arg, ast.Name):
                deferred.add(arg.id)          # to_thread(_ping)
            else:
                for x in ast.walk(arg):       # to_thread(lambda: f())
                    safe.add(id(x))
    for sub in ast.walk(fn):
        if isinstance(sub, ast.FunctionDef) and sub.name in deferred:
            for x in ast.walk(sub):
                safe.add(id(x))
    return safe


def _scan(tree):
    """返回 [(函数名, 行号, 调用名)] —— 未被 to_thread 豁免的阻塞调用。"""
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        safe = _exempted(fn)
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Call):
                continue
            name = _blocking_name(sub)
            if not name or id(sub) in safe:
                continue
            # `result` 有两副面孔：
            #   · concurrent.futures.Future.result(timeout=…) —— 真阻塞；
            #   · asyncio Future.result()（在 done 集合里的）—— 不阻塞。
            # 用"有没有传参"区分（阻塞那个必须给 timeout）。
            if name == "result" and not sub.args and not sub.keywords:
                continue
            bad.append((fn.name, sub.lineno, name))
    return bad


class TestNoSyncBlockingInAsyncRoutes:
    def test_source_parses(self):
        assert os.path.isfile(SERVER), SERVER
        ast.parse(open(SERVER, encoding="utf-8").read())

    def test_no_blocking_calls_outside_to_thread(self):
        tree = ast.parse(open(SERVER, encoding="utf-8").read())
        bad = _scan(tree)
        assert not bad, (
            "以下 async 路由里有未包进 asyncio.to_thread 的同步阻塞调用 —— 会冻住"
            "事件循环（/ws 推送、/api/stop、/api/approve 全部排队）：\n"
            + "\n".join(f"  server.py:{ln} {fn}() 里的 {name}()" for fn, ln, name in bad)
        )

    def test_scanner_actually_catches_violations(self):
        """反向验证：扫描器对一段**故意写成阻塞**的代码必须报出来。

        没有这条，扫描器一旦因为 AST 结构变化而失灵，上面那条会永远"通过"。
        """
        bad_src = (
            "import asyncio, subprocess\n"
            "async def route():\n"
            "    return subprocess.run(['git', 'status'])\n"
        )
        assert _scan(ast.parse(bad_src)), "扫描器漏掉了明显的阻塞调用"

        ok_src = (
            "import asyncio, subprocess\n"
            "async def route():\n"
            "    return await asyncio.to_thread(subprocess.run, ['git', 'status'])\n"
        )
        assert not _scan(ast.parse(ok_src)), "扫描器把正确的 to_thread 写法误报了"


class TestFileIoBoundary:
    """文件**内容** IO 也算阻塞；但只认限定形状，不误伤同名的纯内存方法。

    这一组钉住的是"匹配精度"：放宽了守卫会变成噪声（`str.replace` 满屏误报），
    收得太紧又会漏掉真的读写（`open` / `os.replace` / `json.load`）。
    """

    def _scan_src(self, body: str):
        return _scan(ast.parse("import asyncio, os, json as _json\n"
                               "async def route():\n" + body))

    @pytest.mark.parametrize("body, expected", [
        ("    with open('f') as fh:\n        return fh.read()", "open"),
        ("    os.makedirs('d', exist_ok=True)", "os.makedirs"),
        ("    os.replace('a', 'b')", "os.replace"),
        ("    return _json.load(open('f'))", "open"),
        ("    _json.dump({}, open('f', 'w'))", "open"),
        ("    return p.read_text(encoding='utf-8')", "read_text"),
        ("    p.write_bytes(b'x')", "write_bytes"),
    ])
    def test_content_io_is_flagged(self, body, expected):
        names = [n for _, _, n in self._scan_src(body)]
        assert names, f"漏报了文件内容 IO：{body.strip()}"
        # 用"包含"而不是相等：os.replace 报 `os.replace`，pathlib 报 `p.read_text`
        assert any(expected in n for n in names), f"{expected} 没报出来，实际={names}"

    @pytest.mark.parametrize("body", [
        "    return text.replace('\\r\\n', '\\n')",      # str.replace：api_diff 的换行归一化
        "    return os.path.exists('f')",                 # 单次 stat：故意豁免
        "    return os.path.isfile('f')",
        "    return img.save(buf, format='PNG')",         # BytesIO：不碰磁盘
    ])
    def test_non_fs_shapes_are_not_flagged(self, body):
        assert not self._scan_src(body), f"误报（会把守卫变成噪声）：{body.strip()}"

    def test_json_alias_is_recognized(self):
        """`import json as _json` 的别名写法必须照样认出来（api_feedback 就是这么写的）。"""
        assert self._scan_src("    return _json.load(open('f', 'r'))")
