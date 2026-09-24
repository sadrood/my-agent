"""运行环境提示按平台分叉。

背景（2026-09-24 审计）：这份提示词原先只有 Windows cmd 一份，而
`agent/agent.py` 在 Linux 上会把 `shell=sh` 填进去 —— 结果提示词整段在教模型用
`findstr`/`dir`/`type`/`Get-Content`，**主动把模型引向 Linux 上不存在的命令**。
修法：平台无关的部分抽成共享段，两平台各写差异，再由 `platform_notice()` 分派。
"""
from models.prompts import (PLATFORM_NOTICE_TEMPLATE, PLATFORM_NOTICE_WINDOWS,
                            platform_notice)

#: Windows 专有的命令/工具名 —— 出现在 POSIX 提示里就是把人往沟里带
WINDOWS_ONLY_WORDS = ("findstr", "Get-Content", "Start-Sleep", "powershell",
                      "cmd.exe", "dir ")


class TestWindowsNotice:
    def test_keeps_cmd_specific_guidance(self):
        text = platform_notice("Windows", "cmd.exe")
        assert "Windows cmd" in text
        assert "findstr" in text          # 文本搜索
        assert "Get-Content" in text      # 看文件尾部
        assert "Start-Sleep" in text      # 没有 sleep
        assert "ls" in text               # 明确告知没有这些命令

    def test_backward_compatible_alias_still_points_at_windows(self):
        """旧名字不能变语义 —— 历史调用方与 tests/test_ui.py 仍在用它。"""
        assert PLATFORM_NOTICE_TEMPLATE is PLATFORM_NOTICE_WINDOWS
        assert "findstr" in PLATFORM_NOTICE_TEMPLATE.format(
            system="Windows", shell="cmd.exe")


class TestPosixNotice:
    def test_does_not_teach_windows_commands(self):
        """核心回归：Linux 上绝不能出现 Windows 专有命令。"""
        text = platform_notice("Linux", "/bin/bash")
        for word in WINDOWS_ONLY_WORDS:
            assert word not in text, f"POSIX 提示里混进了 Windows 专有词：{word!r}"

    def test_says_unix_commands_are_available(self):
        text = platform_notice("Linux", "/bin/bash")
        assert "Linux" in text and "/bin/bash" in text
        assert "POSIX" in text
        assert "tail -N" in text          # 管道分页在 POSIX 上是可用的
        assert "grep" in text and "sleep" in text

    def test_darwin_is_treated_as_posix(self):
        text = platform_notice("Darwin", "/bin/zsh")
        assert "POSIX" in text
        for word in WINDOWS_ONLY_WORDS:
            assert word not in text


class TestSharedPartsOnBothPlatforms:
    """平台无关的部分两边都要有 —— 抽共享段的意义就在这里。"""

    def test_bg_subcommand_protocol(self):
        for system, shell in (("Windows", "cmd.exe"), ("Linux", "bash")):
            text = platform_notice(system, shell)
            assert "bg output job-xxx 30" in text, system
            assert "background=true" in text, system

    def test_long_task_protocol_and_file_tool_preference(self):
        for system, shell in (("Windows", "cmd.exe"), ("Linux", "bash")):
            text = platform_notice(system, shell)
            assert "长任务" in text, system
            assert "file 工具" in text, system

    def test_loopback_binding_warning(self):
        """预览服务必须绑回环 —— 这条与平台无关，但漏掉一边就等于漏掉。"""
        for system, shell in (("Windows", "cmd.exe"), ("Linux", "bash")):
            assert "127.0.0.1" in platform_notice(system, shell), system

    def test_format_placeholders_all_filled(self):
        for system, shell in (("Windows", "cmd.exe"), ("Linux", "bash")):
            text = platform_notice(system, shell)
            assert "{" not in text and "}" not in text, f"有没填上的占位符：{system}"
