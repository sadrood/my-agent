"""
斜杠命令解析测试（容忍连写 / 拼写建议）。
"""
from main import _parse_command, build_parser, parse_model_spec


class TestParseModelSpec:
    """`/model 名[@地址[#key]]` 的解析（换端点通常要同时换 key）。"""

    def test_name_only(self):
        assert parse_model_spec("deepseek-flash") == ("deepseek-flash", None, None)

    def test_name_with_endpoint(self):
        assert parse_model_spec("m@http://172.16.10.242:3000/v1") == (
            "m", "http://172.16.10.242:3000/v1", None)

    def test_name_endpoint_and_key(self):
        assert parse_model_spec("m@http://x/v1#sk-abc") == ("m", "http://x/v1", "sk-abc")

    def test_trailing_hash_means_no_key(self):
        assert parse_model_spec("m@http://x/v1#") == ("m", "http://x/v1", None)

    def test_blank_means_no_override(self):
        assert parse_model_spec("") == (None, None, None)
        assert parse_model_spec("   ") == (None, None, None)

    def test_whitespace_is_trimmed(self):
        assert parse_model_spec("  m @ http://x/v1 # sk-1 ") == ("m", "http://x/v1", "sk-1")

    def test_key_without_endpoint_is_part_of_the_name(self):
        """没有 `@` 时不解析 key（`#` 只在地址后面才有意义）。"""
        assert parse_model_spec("m#sk-abc") == ("m#sk-abc", None, None)


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

    def test_article_command(self):
        """文章工坊：/article <主题> [| 写作要求]（要求由 REPL 侧按 | 切分）。"""
        assert _parse_command("/article AI Agent 的记忆机制") == (
            "article", "AI Agent 的记忆机制")
        assert _parse_command("/article") == ("article", "")
        assert _parse_command("/article选题 | 给大众看") == ("article", "选题 | 给大众看")
        assert _parse_command("/articles") == (None, None), "英文连写不算命令"

    def test_article_flag_exists(self):
        parser = build_parser()
        assert parser.parse_args(["--article", "主题"]).article is True

    def test_consent_command(self):
        """Guardian 放行台账：/consent 看被拦待放行与已生效授权。"""
        assert _parse_command("/consent") == ("consent", "")
        assert _parse_command("/consent list") == ("consent", "list")

    def test_supervisor_flags(self):
        """监管者开关：默认 None（读配置=开），可显式开/关。"""
        parser = build_parser()
        assert parser.parse_args(["任务"]).supervisor is None
        assert parser.parse_args(["--no-supervisor", "任务"]).supervisor is False
        assert parser.parse_args(["--supervisor", "任务"]).supervisor is True

    def test_history_command(self):
        """查看当前对话历史（用户反馈"重新打开看不到历史"后新增）。"""
        assert _parse_command("/history") == ("history", "")
        assert _parse_command("/history 50") == ("history", "50")
        assert _parse_command("/historyx") == (None, None)

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

    def test_rename_and_title_commands(self):
        """会话名：/rename 自己指定，/title 让 agent 按对话重新总结。"""
        assert _parse_command("/rename 我的调研") == ("rename", "我的调研")
        assert _parse_command("/rename") == ("rename", "")
        assert _parse_command("/title") == ("title", "")
        assert _parse_command("/renamex") == (None, None), "英文连写不算命令"
        assert _parse_command("/titlex") == (None, None)


class TestSessionTitle:
    """会话名（title）：只改给人看的名字，会话 id 与历史不动。"""

    def _store(self, tmp_path):
        from agent.session import SessionStore
        store = SessionStore(config={"dir": str(tmp_path)})
        store.save_conversation("conv-1", messages=[{"role": "user", "content": "帮我调研向量库"}],
                                title="旧名字")
        return store

    def test_set_title_keeps_history_and_id(self, tmp_path):
        store = self._store(tmp_path)
        assert store.set_title("conv-1", "向量库调研") is True
        conv = store.load_conversation("conv-1")
        assert conv["title"] == "向量库调研"
        assert conv["id"] == "conv-1", "改名不能动会话 id（/open 与 --session 的引用要保住）"
        assert conv["messages"], "历史必须保留"
        listed = {c["id"]: c["title"] for c in store.list_conversations()}
        assert listed["conv-1"] == "向量库调研", "/sessions 列表要显示新名字"

    def test_set_title_rejects_unknown_conversation(self, tmp_path):
        store = self._store(tmp_path)
        assert store.set_title("conv-不存在", "x") is False

    def test_set_title_trims_and_caps(self, tmp_path):
        store = self._store(tmp_path)
        store.set_title("conv-1", "  带空格的  ")
        assert store.load_conversation("conv-1")["title"] == "带空格的"
        store.set_title("conv-1", "长" * 300)
        assert len(store.load_conversation("conv-1")["title"]) == 120

    def test_title_for_prefers_small_model(self):
        from main import _session_title_for

        class _Small:
            def title_for(self, messages):
                return "小模型起的名字"

        class _Agent:
            small_model = _Small()
            llm = None

        assert _session_title_for(_Agent(), [{"role": "user", "content": "x"}]) == "小模型起的名字"

    def test_title_for_falls_back_to_main_model(self):
        from main import _session_title_for

        class _LLM:
            def __init__(self):
                self.calls = []

            def chat(self, messages, **kw):
                self.calls.append(messages)
                return '"回退起的名字"\n多余的一行'

        class _Agent:
            small_model = None
            llm = _LLM()

        agent = _Agent()
        title = _session_title_for(agent, [{"role": "user", "content": "聊聊向量库"}])
        assert title == "回退起的名字", "取首行并去掉引号"
        assert agent.llm.calls, "回退路径必须真的问过模型"

    def test_title_for_returns_empty_when_nothing_works(self):
        from main import _session_title_for

        class _Boom:
            def title_for(self, messages):
                raise RuntimeError("小模型坏了")

        class _Agent:
            small_model = _Boom()
            llm = None

        assert _session_title_for(_Agent(), [{"role": "user", "content": "x"}]) == ""
        assert _session_title_for(_Agent(), []) == "", "没有对话内容时不该编名字"


class TestPreflight:
    """入口预检：缺密钥立刻停下（元命令必须放行，环境没配好时人先跑的就是 --doctor）。"""

    class _Args:
        doctor = upgrade = upgrade_check = upgrade_stash = llm_health = list_tools = False

    def test_missing_key_gives_actionable_message(self, monkeypatch):
        from config import LLM_CONFIG
        from main import _preflight_error
        monkeypatch.setitem(LLM_CONFIG, "api_key", "")
        msg = _preflight_error(self._Args())
        assert "LLM_API_KEY" in msg and ".env" in msg and "--doctor" in msg

    def test_key_present_passes(self, monkeypatch):
        from config import LLM_CONFIG
        from main import _preflight_error
        monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")
        assert _preflight_error(self._Args()) == ""

    def test_meta_commands_are_never_blocked(self, monkeypatch):
        from config import LLM_CONFIG
        from main import _preflight_error
        monkeypatch.setitem(LLM_CONFIG, "api_key", "")
        for flag in ("doctor", "upgrade", "upgrade_check", "upgrade_stash",
                     "llm_health", "list_tools"):
            args = self._Args()
            setattr(args, flag, True)
            assert _preflight_error(args) == "", f"--{flag} 不该被预检拦住"


class TestPrintAndJsonOutput:
    """一行式与机读输出：脚本/cron/CI 要靠它们消费结果。"""

    def _run(self, monkeypatch, capsys, argv, result):
        import main
        from config import LLM_CONFIG
        monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")
        monkeypatch.setattr(main, "run_once", lambda **kw: result)
        monkeypatch.setattr(main.sys, "argv", ["main.py"] + argv)
        code = main.main()
        return code, capsys.readouterr().out

    def test_print_flag_becomes_the_goal(self, monkeypatch, capsys):
        import main
        seen = {}

        class _R:
            success = True
            output = "干完了"
            error = ""

        from config import LLM_CONFIG
        monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")
        monkeypatch.setattr(main, "run_once", lambda **kw: seen.update(kw) or _R())
        monkeypatch.setattr(main.sys, "argv", ["main.py", "-p", "把日志清一下"])
        main.main()
        assert seen.get("goal") == "把日志清一下", "-p 的文本要当成任务"

    def test_json_output_is_machine_readable(self, monkeypatch, capsys):
        import json as _json

        class _R:
            success = True
            output = "结果正文"
            error = ""

        code, out = self._run(monkeypatch, capsys, ["-p", "任务", "--output", "json"], _R())
        assert code == 0
        payload = _json.loads(out.strip().splitlines()[-1])
        assert payload["ok"] is True and payload["output"] == "结果正文"
        assert set(payload) == {"ok", "session", "output", "error"}

    def test_json_output_reports_failure_and_exit_code(self, monkeypatch, capsys):
        import json as _json

        class _R:
            success = False
            output = ""
            error = "上游 401"

        code, out = self._run(monkeypatch, capsys, ["-p", "任务", "--output", "json"], _R())
        assert code == 1, "失败要给非零退出码，脚本才能判断"
        payload = _json.loads(out.strip().splitlines()[-1])
        assert payload["ok"] is False and payload["error"] == "上游 401"

    def test_json_mode_is_quiet(self, monkeypatch, capsys):
        """机读模式不能往 stdout 混富文本（否则调用方解析不了）。"""
        import main
        from config import LLM_CONFIG

        class _R:
            success = True
            output = "x"
            error = ""

        seen = {}
        monkeypatch.setitem(LLM_CONFIG, "api_key", "sk-test")
        monkeypatch.setattr(main, "run_once", lambda **kw: seen.update(kw) or _R())
        monkeypatch.setattr(main.sys, "argv", ["main.py", "-p", "任务", "--output", "json"])
        main.main()
        assert seen.get("verbose") is False, "json 模式必须关掉富文本渲染"

    def test_json_flag_is_registered(self):
        from main import build_parser
        args = build_parser().parse_args(["-p", "x", "--output", "json"])
        assert args.print_prompt == "x" and args.output == "json"
        assert build_parser().parse_args(["x"]).output == "text"


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

    def test_upgrade_flags(self):
        parser = build_parser()
        args = parser.parse_args(["--upgrade"])
        assert args.upgrade is True and args.upgrade_check is False
        args2 = parser.parse_args(["--upgrade-check"])
        assert args2.upgrade_check is True and args2.upgrade is False
        args3 = parser.parse_args(["--upgrade", "--upgrade-stash"])
        assert args3.upgrade_stash is True

    def test_upgrade_slash_command(self):
        assert _parse_command("/upgrade") == ("upgrade", "")
        assert _parse_command("/upgrade check") == ("upgrade", "check")

    def test_permission_slash_command(self):
        assert _parse_command("/permission") == ("permission", "")
        assert _parse_command("/permission auto") == ("permission", "auto")

    def test_every_handled_command_is_parseable(self):
        """有处理分支的命令必须在解析白名单里，否则它会变成一条普通消息发给模型。"""
        import re
        src = open("main.py", encoding="utf-8").read()
        registered = set(re.findall(r'"([a-z_]+)"', re.search(r"for cmd in \((.*?)\):", src, re.S).group(1)))
        handled = set(re.findall(r'if cmd == "([a-z_]+)"', src))
        assert handled, "没解析到任何命令分支，测试本身失效了"
        missing = sorted(handled - registered)
        assert not missing, f"这些命令有分支但解析不到: {missing}"
        for cmd in sorted(handled):
            assert _parse_command(f"/{cmd}") == (cmd, ""), f"/{cmd} 解析失败"
