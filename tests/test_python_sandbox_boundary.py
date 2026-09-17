"""
批次4/5 回归：python 工具"护栏而非沙箱"的边界。

实测故障（2026-09-17 审计）：
- `os.system('echo ...')` 与 `os.popen(...)` 能**直接执行系统命令**——它们是属性
  调用而非 import，BLOCKED_IMPORTS 那套钩子完全拦不住；终端工具的黑名单
  （格式化磁盘、del /s、git push -f…）在这里等于不存在。
- 子进程输出直接写真实 fd 1，连工具的 stdout 捕获都被绕过。
- 而模块文档/描述一直声称它"防止绕过 terminal 审批门执行系统命令"。

同时用测试固定住**诚实的边界说明**：Python 层面不是真隔离，
`().__class__.__base__.__subclasses__()` 仍可达 Popen（已实测），
真正的隔离靠 OS 级沙箱与审批策略。
"""
import pytest

from tools.python import PythonTool


@pytest.fixture
def tool():
    return PythonTool()


def run(tool, code):
    return tool.execute(code)


class TestOSExecutionBlocked:
    def test_preimported_os_system_blocked(self, tool):
        r = run(tool, "print(os.system('echo PWNED'))")
        assert r.success is False
        assert "PWNED" not in (r.output or "")

    def test_reimported_os_system_blocked(self, tool):
        """关键：`import os` 也必须拿到受限代理，否则再 import 一次就绕过封锁。"""
        r = run(tool, "import os\nprint(os.system('echo PWNED2'))")
        assert r.success is False
        assert "PWNED2" not in (r.output or "")

    def test_os_popen_blocked(self, tool):
        r = run(tool, "import os\nprint(os.popen('echo x').read())")
        assert r.success is False

    @pytest.mark.parametrize("attr", ["system", "popen", "execv", "spawnv",
                                      "startfile", "kill", "fork"])
    def test_dangerous_attrs_denied(self, tool, attr):
        r = run(tool, f"import os\nprint(os.{attr})")
        assert r.success is False, f"os.{attr} 不应可达"

    def test_error_message_points_to_right_tools(self, tool):
        r = run(tool, "import os\nos.system('echo hi')")
        msg = f"{r.output}\n{r.error}"
        assert "terminal" in msg, "应告诉模型改用 terminal 工具"
        assert "file" in msg, "应告诉模型复制文件改用 file 工具"


class TestNormalOSOpsStillWork:
    def test_path_helpers_available(self, tool):
        r = run(tool, "import os\nprint(os.path.join('a', 'b'))")
        assert r.success is True and "a" in r.output

    def test_makedirs_still_works(self, tool, tmp_path):
        target = str(tmp_path / "sub" / "dir").replace("\\", "/")
        r = run(tool, f"import os\nos.makedirs({target!r}, exist_ok=True)\nprint('ok')")
        assert r.success is True, r.output
        assert (tmp_path / "sub" / "dir").is_dir()

    def test_read_write_files(self, tool, tmp_path):
        p = str(tmp_path / "f.txt").replace("\\", "/")
        assert run(tool, f"open({p!r}, 'w', encoding='utf-8').write('hi')").success
        r = run(tool, f"print(open({p!r}, encoding='utf-8').read())")
        assert r.success and "hi" in r.output

    def test_no_attribute_setattr_on_os(self, tool):
        r = run(tool, "import os\nos.system = print")
        assert r.success is False


class TestHonestBoundaryClaim:
    def test_module_does_not_claim_to_be_a_sandbox(self):
        """文档必须说清"护栏 ≠ 沙箱"，否则会给人错误的安全感。"""
        import tools.python as py
        doc = py.__doc__ or ""
        assert "不是沙箱" in doc or "护栏" in doc

    def test_description_mentions_os_system_disabled(self, tool):
        desc = tool.description
        assert "os.system" in desc or "os.popen" in desc


class TestCappedCapture:
    def test_huge_output_is_truncated_with_note(self, tool):
        r = run(tool, "for i in range(50000):\n    print('x' * 100)")
        assert r.success is True
        assert "已丢弃" in r.output, "超量输出应明确告知被丢弃了多少"
        assert len(r.output) < 300_000, "捕获缓冲必须有上限"

    def test_normal_output_untouched(self, tool):
        r = run(tool, "print('hello capped world')")
        assert r.success and "hello capped world" in r.output
        assert "已丢弃" not in r.output
