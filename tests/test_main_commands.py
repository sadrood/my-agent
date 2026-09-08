"""
斜杠命令解析测试（容忍连写 / 拼写建议）。
"""
from main import _parse_command, build_parser


class TestCliFlags:
    def test_dangerously_skip_permissions_alias(self):
        parser = build_parser()
        args = parser.parse_args(["--dangerously-skip-permissions", "任务"])
        assert args.approval == "never"

    def test_resume_flag(self):
        parser = build_parser()
        args = parser.parse_args(["-r"])
        assert args.resume is True

    def test_model_flags(self):
        parser = build_parser()
        args = parser.parse_args(["--model", "m1", "--base-url", "http://b/v1", "--api-key", "k"])
        assert args.model == "m1"
        assert args.base_url == "http://b/v1"
        assert args.api_key == "k"


class TestParseCommand:
    def test_space_separated(self):
        assert _parse_command("/team 分析一下代码") == ("team", "分析一下代码")
        assert _parse_command("/research AI 趋势") == ("research", "AI 趋势")
        assert _parse_command("/tools") == ("tools", "")

    def test_command_args_concatenated_chinese(self):
        # /team任务（无空格，中文连写）——用户实际输入场景
        assert _parse_command("/team你觉得你自身还有什么需要升级的") == (
            "team", "你觉得你自身还有什么需要升级的"
        )
        assert _parse_command("/research研究一下") == ("research", "研究一下")

    def test_english_concat_not_command(self):
        # /teamwork 是英文连写，不当作命令
        assert _parse_command("/teamwork") == (None, None)
        assert _parse_command("/researchpaper") == (None, None)

    def test_plain_goal(self):
        assert _parse_command("普通任务描述") == (None, None)
        assert _parse_command("") == (None, None)

    def test_unknown_command(self):
        assert _parse_command("/reasearch x") == (None, None)
        assert _parse_command("/resrarch x") == (None, None)

    def test_conversation_commands(self):
        assert _parse_command("/sessions") == ("sessions", "")
        assert _parse_command("/open conv-20260824-abc123") == ("open", "conv-20260824-abc123")
        assert _parse_command("/new") == ("new", "")
        # /newbie 英文连写不算命令
        assert _parse_command("/newbie") == (None, None)

    def test_model_config_commands(self):
        assert _parse_command("/model") == ("model", "")
        assert _parse_command("/model deepseek-v4-pro") == ("model", "deepseek-v4-pro")
        assert _parse_command("/config") == ("config", "")

    def test_image_command(self):
        assert _parse_command("/image 一只橘猫在窗台晒太阳") == ("image", "一只橘猫在窗台晒太阳")
        # /imagegen 英文连写不算命令
        assert _parse_command("/imagegen x") == (None, None)

    def test_memory_command(self):
        assert _parse_command("/memory") == ("memory", "")
        assert _parse_command("/memory prune 50") == ("memory", "prune 50")
        # /memoryx 英文连写不算命令
        assert _parse_command("/memoryx") == (None, None)

    def test_help_command(self):
        assert _parse_command("/help") == ("help", "")
        # /helpx 英文连写不算命令
        assert _parse_command("/helpx") == (None, None)


class TestAutoModeRouting:
    def test_research_keywords(self):
        from agent.mode_router import route_goal
        assert route_goal("帮我做一份 AI 行业调研") == "research"
        assert route_goal("深度研究一下大模型趋势") == "research"
        assert route_goal("写一份竞品分析报告") == "research"

    def test_team_keywords(self):
        from agent.mode_router import route_goal
        assert route_goal("团队协作完成前后端开发") == "team"
        assert route_goal("并行完成三个模块的实现") == "team"

    def test_plain_goals_stay_single(self):
        from agent.mode_router import route_goal
        assert route_goal("写一个排序脚本") == "single"
        assert route_goal("打开百度搜索天气") == "single"
        assert route_goal("") == "single"

    def test_auto_mode_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--auto-mode", "任务"])
        assert args.auto_mode is True
        args2 = parser.parse_args(["任务"])
        assert args2.auto_mode is False

    def test_desktop_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--desktop"])
        assert args.desktop is True
        args2 = parser.parse_args(["--dashboard"])
        assert args2.desktop is False and args2.dashboard is True
