"""
Skills 技能包机制测试（技能包式能力扩展）。

覆盖：frontmatter 解析/降级/损坏容错、关键词命中、多目录发现与去重排序、
max_chars 截断、enabled=false 与目录不存在、路径安全（.. 跳过）、
脚本发现与提示（v2 进阶：脚本登记 / 排序 / 隐藏与子目录跳过 / 渲染含
绝对路径清单与计数）、agent.py 集成（system prompt 注入）、
技能包安装/更新（SkillPackManager：signed 校验 / unsigned 放行 / 冲突与
overwrite / 路径穿越与符号链接 / 非技能目录目标拒绝）。
全部不依赖网络（FakeLLM，无需真调用）。
"""
import hashlib
import os

import pytest

from agent import Agent, AgentConfig
from agent.memory import Memory
from agent.skills import SkillManager, SkillPackManager
from models.llm import LLMToolResponse
from tools.tool_manager import ToolManager


# ----------------------------------------------------------------------
# 工具函数：在 tmp_path 下构造技能目录
# ----------------------------------------------------------------------
def write_skill(base_dir, skill_name, content, md_name="SKILL.md"):
    """在 base_dir/<skill_name>/ 下写入 SKILL.md，返回技能目录路径。"""
    d = base_dir / skill_name
    d.mkdir(parents=True, exist_ok=True)
    (d / md_name).write_text(content, encoding="utf-8")
    return d


def make_manager(proj_dir, user_dir=None, max_chars=6000):
    return SkillManager(project_dir=str(proj_dir), user_dir=str(user_dir), max_chars=max_chars)


# ----------------------------------------------------------------------
# 1. frontmatter 解析
# ----------------------------------------------------------------------
class TestFrontmatter:
    def test_parse_normal(self, tmp_path):
        """正常 frontmatter：name/description/triggers 解析，正文从闭合行后开始。"""
        proj = tmp_path / "skills"
        write_skill(proj, "excel", (
            "---\n"
            "name: excel\n"
            "description: 生成 Excel 报表\n"
            "triggers: excel, 报表, 表格\n"
            "---\n"
            "使用 openpyxl 生成 xlsx 文件。\n"
            "步骤：1. 写数据 2. 保存。"
        ))
        mgr = make_manager(proj, tmp_path / "no_user")
        skills = mgr.discover()
        assert len(skills) == 1
        s = skills[0]
        assert s.name == "excel"
        assert s.description == "生成 Excel 报表"
        assert s.triggers == ["excel", "报表", "表格"]
        # 正文从闭合行之后开始，不含 frontmatter
        assert s.body.startswith("使用 openpyxl 生成 xlsx 文件。")
        assert "---" not in s.body
        assert "步骤：1. 写数据 2. 保存。" in s.body

    def test_missing_frontmatter_fallback(self, tmp_path):
        """frontmatter 缺失：name 用目录名，description 空，正文为全文。"""
        proj = tmp_path / "skills"
        write_skill(proj, "my-skill", "直接是正文，没有 frontmatter。\n第二行。")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.name == "my-skill"
        assert s.description == ""
        assert s.triggers == []
        # 正文从第一个非 frontmatter 行开始（即全文）
        assert s.body.startswith("直接是正文，没有 frontmatter。")

    def test_broken_lines_tolerated(self, tmp_path):
        """frontmatter 内损坏行（无冒号）静默跳过，其余字段正常解析。"""
        proj = tmp_path / "skills"
        write_skill(proj, "broken", (
            "---\n"
            "name: broken-skill\n"
            "这一行没有冒号，应被跳过\n"
            "description: 容错描述\n"
            "triggers: 容错, tol\n"
            "---\n"
            "正文 OK。"
        ))
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.name == "broken-skill"
        assert s.description == "容错描述"
        assert s.triggers == ["容错", "tol"]
        assert s.body == "正文 OK。"

    def test_unclosed_frontmatter_fallback(self, tmp_path):
        """frontmatter 未闭合：解析失败，降级用目录名，正文从首行开始。"""
        proj = tmp_path / "skills"
        write_skill(proj, "unclosed", (
            "---\n"
            "name: should-not-apply\n"
            "description: 不应生效\n"
            "正文混在里面"
        ))
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.name == "unclosed"            # 降级用目录名
        assert s.description == ""             # 描述置空
        # 降级路径下正文从第一行开始（视为无 frontmatter）
        assert s.body.startswith("---")

    def test_partial_meta_defaults(self, tmp_path):
        """frontmatter 只给了部分键：缺 name 用目录名，缺 description 为空。"""
        proj = tmp_path / "skills"
        write_skill(proj, "partial", "---\ntriggers: aaa, bbb\n---\n正文。")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.name == "partial"
        assert s.description == ""
        assert s.triggers == ["aaa", "bbb"]
        assert s.body == "正文。"


# ----------------------------------------------------------------------
# 2. 关键词命中
# ----------------------------------------------------------------------
class TestMatch:
    @pytest.fixture()
    def mgr(self, tmp_path):
        proj = tmp_path / "skills"
        write_skill(proj, "excel", (
            "---\nname: excel\n"
            "description: 生成 Excel 报表\n"
            "triggers: 表格, spreadsheet\n"
            "---\n正文-excel"
        ))
        write_skill(proj, "poetry", (
            "---\nname: poetry\n"
            "description: 写诗与对联\n"
            "triggers: 诗, 对联\n"
            "---\n正文-poetry"
        ))
        return make_manager(proj, tmp_path / "no_user")

    def test_hit_by_trigger(self, mgr):
        assert [s.name for s in mgr.match("帮我做个表格")] == ["excel"]
        assert [s.name for s in mgr.match("写一首诗")] == ["poetry"]

    def test_hit_by_description_word(self, mgr):
        # description 分词 "excel" / "报表" 命中（大小写不敏感）
        assert "excel" in [s.name for s in mgr.match("处理 EXCEL 数据")]
        assert "excel" in [s.name for s in mgr.match("生成报表")]

    def test_hit_by_name_word(self, tmp_path):
        proj = tmp_path / "skills"
        write_skill(proj, "git-helper", "---\ndescription: 常用 git 命令助手\n---\n正文-git")
        mgr = make_manager(proj, tmp_path / "no_user")
        assert [s.name for s in mgr.match("怎么用 git 提交")] == ["git-helper"]
        assert [s.name for s in mgr.match("githelper")] == ["git-helper"]  # 整段包含

    def test_no_match(self, mgr):
        assert mgr.match("帮我订个外卖") == []
        assert mgr.match("") == []

    def test_multi_hit(self, mgr):
        # goal 同时命中两个技能
        names = [s.name for s in mgr.match("写诗 表格 报表 对联 全都要")]
        assert set(names) == {"excel", "poetry"}


# ----------------------------------------------------------------------
# 3. 多目录发现 / 去重 / 排序 / 路径安全
# ----------------------------------------------------------------------
class TestDiscover:
    def test_project_and_user_dirs(self, tmp_path):
        proj = tmp_path / "skills"
        user = tmp_path / "user_skills"
        write_skill(proj, "b-skill", "---\ndescription: 项目级 B\n---\n正文-B")
        write_skill(user, "a-skill", "---\ndescription: 用户级 A\n---\n正文-A")
        mgr = make_manager(proj, user)
        names = [s.name for s in mgr.discover()]
        assert names == ["a-skill", "b-skill"]     # 按技能名排序稳定

    def test_dedup_project_wins(self, tmp_path):
        """同名技能：项目级优先于用户级（内容取项目级）。"""
        proj = tmp_path / "skills"
        user = tmp_path / "user_skills"
        write_skill(proj, "dup", "---\ndescription: 项目级版本\n---\n项目正文")
        write_skill(user, "dup", "---\ndescription: 用户级版本\n---\n用户正文")
        mgr = make_manager(proj, user)
        skills = mgr.discover()
        assert len(skills) == 1                    # 去重
        assert skills[0].description == "项目级版本"

    def test_discover_cached_stable(self, tmp_path):
        proj = tmp_path / "skills"
        write_skill(proj, "z-skill", "---\ndescription: Z\n---\n正文")
        write_skill(proj, "a-skill", "---\ndescription: A\n---\n正文")
        mgr = make_manager(proj, tmp_path / "no_user")
        first = [s.name for s in mgr.discover()]
        # 再次调用结果一致（缓存）
        assert [s.name for s in mgr.discover()] == first
        assert first == ["a-skill", "z-skill"]

    def test_missing_dirs_skipped(self, tmp_path):
        """目录不存在：静默跳过，discover 返回空。"""
        mgr = make_manager(tmp_path / "nope", tmp_path / "nope2")
        assert mgr.discover() == []

    def test_dotdot_name_skipped(self, tmp_path):
        """技能名含 '..' 被跳过；不含分隔符的正常技能保留。"""
        proj = tmp_path / "skills"
        write_skill(proj, "evil..name", "---\ndescription: 危险\n---\n正文")
        write_skill(proj, "normal", "---\ndescription: 正常\n---\n正文")
        # 静态安全检查
        assert not SkillManager._is_safe_name("..")
        assert not SkillManager._is_safe_name("a..b")
        assert not SkillManager._is_safe_name("a/b")
        assert not SkillManager._is_safe_name("a\\b")
        assert SkillManager._is_safe_name("normal")
        # 发现时跳过危险目录
        mgr = make_manager(proj, tmp_path / "no_user")
        names = [s.name for s in mgr.discover()]
        assert names == ["normal"]
        assert "evil..name" not in names

    def test_dir_without_skillmd_skipped(self, tmp_path):
        """目录下没有 SKILL.md 不算技能。"""
        proj = tmp_path / "skills"
        (proj / "no-md").mkdir(parents=True)
        (proj / "no-md" / "README.md").write_text("x", encoding="utf-8")
        write_skill(proj, "real", "---\ndescription: 真技能\n---\n正文")
        mgr = make_manager(proj, tmp_path / "no_user")
        assert [s.name for s in mgr.discover()] == ["real"]


# ----------------------------------------------------------------------
# 3.5 脚本发现与提示（v2 进阶）
# ----------------------------------------------------------------------
class TestScripts:
    def test_scripts_discovered_sorted(self, tmp_path):
        """第一层普通文件登记进 scripts，按文件名排序。"""
        proj = tmp_path / "skills"
        d = write_skill(proj, "excel", "---\ndescription: 报表\n---\n正文")
        (d / "run.py").write_text("print(1)", encoding="utf-8")
        (d / "data.csv").write_text("a,b", encoding="utf-8")
        (d / "setup.bat").write_text("echo hi", encoding="utf-8")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.scripts == ["data.csv", "run.py", "setup.bat"]  # 按文件名排序

    def test_skillmd_not_registered(self, tmp_path):
        """SKILL.md 自身不登记进 scripts。"""
        proj = tmp_path / "skills"
        d = write_skill(proj, "excel", "---\ndescription: 报表\n---\n正文")
        (d / "helper.py").write_text("x", encoding="utf-8")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert "SKILL.md" not in s.scripts
        assert s.scripts == ["helper.py"]

    def test_hidden_files_skipped(self, tmp_path):
        """点开头隐藏文件跳过。"""
        proj = tmp_path / "skills"
        d = write_skill(proj, "excel", "---\ndescription: 报表\n---\n正文")
        (d / ".env").write_text("x", encoding="utf-8")
        (d / ".secret.sh").write_text("x", encoding="utf-8")
        (d / "visible.sh").write_text("x", encoding="utf-8")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.scripts == ["visible.sh"]

    def test_subdirs_not_recursed(self, tmp_path):
        """子目录内容不递归：只登记第一层文件。"""
        proj = tmp_path / "skills"
        d = write_skill(proj, "excel", "---\ndescription: 报表\n---\n正文")
        (d / "lib").mkdir()
        (d / "lib" / "inner.py").write_text("x", encoding="utf-8")
        (d / "top.py").write_text("x", encoding="utf-8")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.scripts == ["top.py"]
        assert "inner.py" not in s.scripts

    def test_no_scripts_default(self, tmp_path):
        """技能目录只有 SKILL.md：scripts 为空列表。"""
        proj = tmp_path / "skills"
        write_skill(proj, "poetry", "---\ndescription: 写诗\n---\n正文")
        mgr = make_manager(proj, tmp_path / "no_user")
        s = mgr.discover()[0]
        assert s.scripts == []


class TestScriptsRender:
    def test_render_absolute_script_paths_for_hit(self, tmp_path):
        """命中技能正文之后追加「附带脚本」清单（每行一条绝对路径）。"""
        proj = tmp_path / "skills"
        d = write_skill(proj, "excel", (
            "---\nname: excel\ndescription: 生成 Excel 报表\n"
            "triggers: 表格\n---\n使用 openpyxl 生成 xlsx。"
        ))
        (d / "gen.py").write_text("x", encoding="utf-8")
        (d / "to_csv.py").write_text("x", encoding="utf-8")
        mgr = make_manager(proj, tmp_path / "no_user")
        text = mgr.render_for_prompt("做一个表格")
        # 绝对路径 = 技能目录 + 相对名
        for rel in ("gen.py", "to_csv.py"):
            assert str(d / rel) in text
        assert "附带脚本" in text
        assert "（含 2 个脚本）" in text            # 索引行计数提示
        # 附带脚本段落在正文之后
        assert text.index("使用 openpyxl 生成 xlsx。") < text.index("附带脚本")

    def test_render_no_scripts_unchanged(self, tmp_path):
        """无脚本技能渲染不受影响：无「附带脚本」段与计数提示。"""
        proj = tmp_path / "skills"
        write_skill(proj, "excel", (
            "---\nname: excel\ndescription: 生成 Excel 报表\n"
            "triggers: 表格\n---\n使用 openpyxl 生成 xlsx。"
        ))
        mgr = make_manager(proj, tmp_path / "no_user")
        text = mgr.render_for_prompt("做一个表格")
        assert "使用 openpyxl 生成 xlsx。" in text
        assert "附带脚本" not in text
        assert "个脚本" not in text

    def test_render_index_count_for_non_hit(self, tmp_path):
        """未命中技能的索引行也带脚本计数提示。"""
        proj = tmp_path / "skills"
        d1 = write_skill(proj, "excel", "---\ndescription: 报表\n---\n正文-excel")
        (d1 / "run.py").write_text("x", encoding="utf-8")
        write_skill(proj, "poetry", "---\ndescription: 写诗\ntriggers: 诗\n---\n正文-poetry")
        mgr = make_manager(proj, tmp_path / "no_user")
        text = mgr.render_for_prompt("写一首诗")   # 只命中 poetry
        assert "正文-poetry" in text               # 命中技能正文
        assert "excel：报表（含 1 个脚本）" in text   # 未命中技能索引行也带计数


# ----------------------------------------------------------------------
# 4. render_for_prompt
# ----------------------------------------------------------------------
class TestRender:
    def test_render_empty_when_no_skills(self, tmp_path):
        """目录不存在 / 无技能时 render 返回空串。"""
        mgr = make_manager(tmp_path / "nope", tmp_path / "nope2")
        assert mgr.render_for_prompt("随便什么目标") == ""
        # 目录存在但没有任何技能
        (tmp_path / "empty").mkdir()
        mgr2 = make_manager(tmp_path / "empty", tmp_path / "nope2")
        assert mgr2.render_for_prompt("随便什么目标") == ""

    def test_render_index_and_hit_body(self, tmp_path):
        """先列全部技能索引，再给命中技能完整正文。"""
        proj = tmp_path / "skills"
        write_skill(proj, "excel", (
            "---\nname: excel\n"
            "description: 生成 Excel 报表\n"
            "triggers: 表格\n"
            "---\n使用 openpyxl 生成 xlsx。\n第二行正文。"
        ))
        write_skill(proj, "poetry", "---\ndescription: 写诗\n---\n正文-poetry")
        mgr = make_manager(proj, tmp_path / "no_user")
        text = mgr.render_for_prompt("做一个表格")
        # 索引包含两个技能
        assert "excel" in text and "生成 Excel 报表" in text
        assert "poetry" in text and "写诗" in text
        # 命中正文只有 excel
        assert "使用 openpyxl 生成 xlsx。" in text
        assert "正文-poetry" not in text
        # 命中时出现“命中技能正文”段落
        assert "命中技能正文" in text

    def test_render_no_hit_only_index(self, tmp_path):
        proj = tmp_path / "skills"
        write_skill(proj, "excel", "---\ndescription: 生成 Excel 报表\n---\n正文-excel")
        mgr = make_manager(proj, tmp_path / "no_user")
        text = mgr.render_for_prompt("写一首诗")
        assert "技能索引" in text
        assert "正文-excel" not in text
        assert "命中技能正文" not in text

    def test_max_chars_truncated(self, tmp_path):
        """总长超过 max_chars 时截断并注明（skill 截断）。"""
        proj = tmp_path / "skills"
        long_body = "这是一段很长的技能正文。" * 40
        write_skill(proj, "long", f"---\ndescription: 长技能\n---\n{long_body}")
        mgr = make_manager(proj, tmp_path / "no_user", max_chars=80)
        text = mgr.render_for_prompt("长技能")
        assert "skill 截断" in text          # 截断注明
        # 大 max_chars 时不截断
        mgr2 = make_manager(proj, tmp_path / "no_user", max_chars=100000)
        text2 = mgr2.render_for_prompt("长技能")
        assert "skill 截断" not in text2
        assert long_body in text2


# ----------------------------------------------------------------------
# 5. agent.py 集成（FakeLLM，无网络）
# ----------------------------------------------------------------------
class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.tools_calls = []
        self.chat_calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_calls.append(messages)
        if not self.script:
            return LLMToolResponse(content="（脚本耗尽）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def chat(self, messages, **kwargs):
        self.chat_calls.append(messages)
        if not self.script:
            return ""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_agent(tmp_path, script, **cfg_overrides):
    config = AgentConfig(
        exec_mode="loop",
        verbose=False,
        rollout_enabled=False,
        guardian_enabled=False,
        approval_policy="never",
        sandbox_mode="workspace-write",
        approval_interactive=False,
        instructions_enabled=False,
        enable_vision=False,
        enable_frame_compare=False,
        enable_anomaly_detect=False,
        snapshot_enabled=False,   # 测试不得触碰真实项目的 git 快照
        checkpoint_per_tool=False,
        repomap_enabled=False,
        **cfg_overrides,
    )
    return Agent(
        llm=FakeLLM(script),
        tool_manager=ToolManager(),
        memory=Memory(db_path=str(tmp_path)),
        config=config,
    )


class TestAgentIntegration:
    def test_hit_skill_injected_into_system_prompt(self, tmp_path):
        """goal 命中技能时，system prompt 包含技能正文与索引。"""
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "excel", (
            "---\n"
            "name: excel\n"
            "description: 生成 Excel 报表\n"
            "triggers: excel, 报表, 表格\n"
            "---\n"
            "使用 openpyxl 生成 xlsx 文件。\n"
            "步骤：1. 写数据 2. 保存。"
        ))
        agent = make_agent(
            tmp_path,
            [LLMToolResponse(content="完成")],
            skills_enabled=True,
            skills_project_dir=str(skills_dir),
            skills_user_dir=str(tmp_path / "no_user_skills"),
        )
        agent.run("帮我生成 Excel 报表")
        system_msg = agent.llm.tools_calls[0][0]["content"]
        assert "## 可用技能（Skills）" in system_msg
        assert "使用 openpyxl 生成 xlsx 文件。" in system_msg       # 命中技能正文
        assert "生成 Excel 报表" in system_msg                     # 技能索引

    def test_no_hit_skill_body_absent(self, tmp_path):
        """goal 不命中技能时，system prompt 不含技能正文。"""
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "excel", (
            "---\nname: excel\n"
            "description: 生成 Excel 报表\n"
            "triggers: 表格\n"
            "---\n秘密正文：openpyxl 专属步骤。"
        ))
        agent = make_agent(
            tmp_path,
            [LLMToolResponse(content="完成")],
            skills_enabled=True,
            skills_project_dir=str(skills_dir),
            skills_user_dir=str(tmp_path / "no_user_skills"),
        )
        agent.run("写一首诗")
        system_msg = agent.llm.tools_calls[0][0]["content"]
        assert "秘密正文：openpyxl 专属步骤。" not in system_msg
        assert "命中技能正文" not in system_msg

    def test_disabled_no_injection(self, tmp_path):
        """skills_enabled=False：不注入任何技能内容。"""
        skills_dir = tmp_path / "skills"
        write_skill(skills_dir, "excel", (
            "---\nname: excel\ndescription: 生成 Excel 报表\n---\n正文-excel"
        ))
        agent = make_agent(
            tmp_path,
            [LLMToolResponse(content="完成")],
            skills_enabled=False,
            skills_project_dir=str(skills_dir),
            skills_user_dir=str(tmp_path / "no_user_skills"),
        )
        assert agent._render_skills_prompt("生成 excel 报表") == ""  # 渲染路径直接返回空
        agent.run("生成 excel 报表")
        system_msg = agent.llm.tools_calls[0][0]["content"]
        assert "## 可用技能（Skills）" not in system_msg
        assert "正文-excel" not in system_msg

    def test_no_skills_no_injection(self, tmp_path):
        """技能为空（目录不存在）：不注入任何内容。"""
        agent = make_agent(
            tmp_path,
            [LLMToolResponse(content="完成")],
            skills_enabled=True,
            skills_project_dir=str(tmp_path / "no_skills_here"),
            skills_user_dir=str(tmp_path / "no_user_skills_here"),
        )
        assert agent._render_skills_prompt("任意目标") == ""
        agent.run("任意目标")
        system_msg = agent.llm.tools_calls[0][0]["content"]
        assert "## 可用技能（Skills）" not in system_msg

    def test_skills_exception_silent(self, tmp_path, monkeypatch):
        """skills 相关异常静默吞掉，不影响主流程。"""
        agent = make_agent(
            tmp_path,
            [LLMToolResponse(content="完成")],
            skills_enabled=True,
            skills_project_dir=str(tmp_path / "no_skills"),
            skills_user_dir=str(tmp_path / "no_user"),
        )

        def boom(goal):
            raise RuntimeError("技能模块炸了")

        monkeypatch.setattr(agent, "_render_skills_prompt", boom)
        out = agent.run("生成 excel 报表")     # 不抛异常，正常完成
        assert "完成" in out


# ----------------------------------------------------------------------
# 4. 技能包安装 / 更新（SkillPackManager，带完整性校验）
# ----------------------------------------------------------------------
def make_skill_pack(base_dir, skills, files=None):
    """构造技能包：base_dir/pack/<skill>/SKILL.md，返回 pack 目录。

    files: {相对路径: 内容} 追加到包内（可放技能目录内或根目录）。
    """
    pack = base_dir / "pack"
    for sname in skills:
        d = pack / sname
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\ndescription: {sname} 技能\n---\n正文-{sname}", encoding="utf-8")
    for rel, content in (files or {}).items():
        p = pack / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return pack


def sign_pack(pack_dir, exclude=None):
    """为技能包生成 MANIFEST.sha256（排除 MANIFEST 自身与 exclude 相对路径）。"""
    exclude = set(exclude or [])
    lines = []
    for root, _dirs, fnames in os.walk(str(pack_dir)):
        for fname in fnames:
            abs_f = os.path.join(root, fname)
            rel = os.path.relpath(abs_f, str(pack_dir)).replace(os.sep, "/")
            if rel == "MANIFEST.sha256" or rel in exclude:
                continue
            with open(abs_f, "rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            lines.append(f"{h}  {rel}")
    (pack_dir / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_pack_manager(tmp_path):
    """构造 SkillPackManager，目标白名单指向 tmp_path/skills_target。"""
    target = tmp_path / "skills_target"
    return SkillPackManager(allowed_dirs=[str(target)]), str(target)


class TestSkillPackManager:
    def test_signed_pack_installs(self, tmp_path):
        """signed 包（MANIFEST 齐全、hash 全部匹配）正常安装全部技能。"""
        pack = make_skill_pack(tmp_path, ["excel", "poetry"])
        sign_pack(pack)
        mgr, target = make_pack_manager(tmp_path)
        v = mgr.verify_pack(str(pack))
        assert v.ok and not v.unsigned
        assert v.skills == ["excel", "poetry"]
        assert v.reasons == []
        res = mgr.install_pack(str(pack), target)
        assert res.ok and res.status == "success"
        assert [r.status for r in res.results] == ["installed", "installed"]
        for name in ("excel", "poetry"):
            assert os.path.isfile(os.path.join(target, name, "SKILL.md"))

    def test_hash_mismatch_rejected(self, tmp_path):
        """manifest hash 与磁盘内容不匹配 → 校验失败并拒绝安装。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        sign_pack(pack)
        (pack / "excel" / "SKILL.md").write_text("被篡改的内容", encoding="utf-8")  # 未重新签名
        mgr, target = make_pack_manager(tmp_path)
        v = mgr.verify_pack(str(pack))
        assert not v.ok
        assert any("hash 不匹配" in r for r in v.reasons)
        res = mgr.install_pack(str(pack), target)
        assert not res.ok and res.status == "failed"
        assert any("完整性校验失败" in r for r in res.reasons)
        assert not os.path.exists(os.path.join(target, "excel"))

    def test_manifest_uncovered_file_rejected(self, tmp_path):
        """manifest 未覆盖的文件（签名后新增）→ 校验失败并拒绝安装。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        sign_pack(pack)
        (pack / "excel" / "extra.py").write_text("print(1)", encoding="utf-8")
        mgr, target = make_pack_manager(tmp_path)
        v = mgr.verify_pack(str(pack))
        assert not v.ok
        assert any("manifest 未覆盖文件" in r for r in v.reasons)
        res = mgr.install_pack(str(pack), target)
        assert not res.ok
        assert not os.path.exists(os.path.join(target, "excel"))

    def test_manifest_missing_file_rejected(self, tmp_path):
        """清单里多余但磁盘缺失的文件 → 校验失败。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        sign_pack(pack)
        lines = (pack / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        lines.append(f"{'0' * 64}  excel/ghost.txt")  # 伪造一个磁盘上不存在的条目
        (pack / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
        mgr, target = make_pack_manager(tmp_path)
        v = mgr.verify_pack(str(pack))
        assert not v.ok
        assert any("清单中文件缺失" in r for r in v.reasons)
        assert not mgr.install_pack(str(pack), target).ok

    def test_unsigned_rejected_by_default(self, tmp_path):
        """未签名包（无 MANIFEST.sha256）：verify 标注 unsigned，默认拒绝安装。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        mgr, target = make_pack_manager(tmp_path)
        v = mgr.verify_pack(str(pack))
        assert v.ok and v.unsigned            # verify 本身不判失败，只标注 unsigned
        assert v.skills == ["excel"]
        res = mgr.install_pack(str(pack), target)
        assert not res.ok and res.status == "failed"
        assert res.unsigned
        assert any("未签名" in r for r in res.reasons)
        assert not os.path.exists(os.path.join(target, "excel"))

    def test_allow_unsigned_installs(self, tmp_path):
        """allow_unsigned=True 时未签名包放行安装。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        mgr, target = make_pack_manager(tmp_path)
        res = mgr.install_pack(str(pack), target, allow_unsigned=True)
        assert res.ok and res.status == "success"
        assert res.results[0].status == "installed"
        assert os.path.isfile(os.path.join(target, "excel", "SKILL.md"))

    def test_same_name_conflict_rejected(self, tmp_path):
        """目标已存在同名技能且 overwrite=False → 拒绝并返回冲突清单。"""
        pack = make_skill_pack(tmp_path, ["excel", "poetry"])
        sign_pack(pack)
        mgr, target = make_pack_manager(tmp_path)
        assert mgr.install_pack(str(pack), target).ok
        # 再次安装同一包：excel/poetry 均冲突 → 整体失败
        res = mgr.install_pack(str(pack), target)
        assert not res.ok and res.status == "failed"
        assert res.conflict == ["excel", "poetry"]
        assert {r.status for r in res.results} == {"conflict"}

    def test_overwrite_updates(self, tmp_path):
        """overwrite=True 时覆盖更新同名技能（新内容生效、旧残留清除）。"""
        pack = make_skill_pack(tmp_path, ["excel"], files={"excel/old.txt": "残留"})
        sign_pack(pack)
        mgr, target = make_pack_manager(tmp_path)
        assert mgr.install_pack(str(pack), target).ok
        # 更新包内容：改 SKILL.md、删掉 old.txt、加 new.py，重新签名
        (pack / "excel" / "SKILL.md").write_text("新版技能正文", encoding="utf-8")
        (pack / "excel" / "old.txt").unlink()
        (pack / "excel" / "new.py").write_text("x = 1", encoding="utf-8")
        sign_pack(pack)
        res = mgr.install_pack(str(pack), target, overwrite=True)
        assert res.ok and res.status == "success"
        assert res.results[0].status == "installed"
        skill_dir = os.path.join(target, "excel")
        assert open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8").read() == "新版技能正文"
        assert not os.path.exists(os.path.join(skill_dir, "old.txt"))   # 残留清除
        assert os.path.isfile(os.path.join(skill_dir, "new.py"))        # 新文件到位

    def test_partial_install_status(self, tmp_path):
        """部分冲突部分成功 → status=partial，ok=False（区分部分/整体失败）。"""
        pack = make_skill_pack(tmp_path, ["excel", "poetry"])
        sign_pack(pack)
        mgr, target = make_pack_manager(tmp_path)
        # 先预置 poetry（不同来源），再装包：poetry 冲突、excel 成功 → partial
        os.makedirs(os.path.join(target, "poetry"), exist_ok=True)
        with open(os.path.join(target, "poetry", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("已有的 poetry")
        res = mgr.install_pack(str(pack), target)
        assert res.status == "partial" and not res.ok
        status_by_name = {r.name: r.status for r in res.results}
        assert status_by_name == {"excel": "installed", "poetry": "conflict"}
        assert res.conflict == ["poetry"]
        assert os.path.isfile(os.path.join(target, "excel", "SKILL.md"))

    def test_path_traversal_entry_rejected(self, tmp_path):
        """相对路径含 '..' 分段或绝对路径的条目被拒绝（防路径穿越）。"""
        mgr, target = make_pack_manager(tmp_path)
        # 单元判定
        assert mgr._unsafe_rel_path("../evil.py")
        assert mgr._unsafe_rel_path("a/../b.txt")
        assert mgr._unsafe_rel_path(os.path.abspath("x"))
        assert not mgr._unsafe_rel_path("a/b.txt")
        # 集成：manifest 伪造 '../evil.py' 条目 → verify 失败且安装被拒
        pack = make_skill_pack(tmp_path, ["excel"])
        sign_pack(pack)
        lines = (pack / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
        lines.append(f"{'0' * 64}  ../evil.py")
        (pack / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
        v = mgr.verify_pack(str(pack))
        assert not v.ok
        assert any("清单中文件缺失" in r for r in v.reasons)
        assert not mgr.install_pack(str(pack), target).ok

    def test_non_skill_target_rejected(self, tmp_path):
        """target_dir 不是 SKILLS_CONFIG 配置的技能目录 → 拒绝（防任意路径写入）。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        sign_pack(pack)
        mgr, target = make_pack_manager(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        res = mgr.install_pack(str(pack), str(elsewhere))
        assert not res.ok and res.status == "failed"
        assert any("不是允许的技能目录" in r for r in res.reasons)
        assert not os.path.exists(os.path.join(str(elsewhere), "excel"))

    def test_symlink_skipped_on_copy(self, tmp_path):
        """复制时跳过符号链接（不跟随、不复制链接本身）。"""
        pack = make_skill_pack(tmp_path, ["excel"])
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.symlink(str(outside), str(pack / "excel" / "link.txt"))
        except (OSError, NotImplementedError):
            pytest.skip("当前平台不支持创建符号链接")
        sign_pack(pack, exclude=["excel/link.txt"])
        mgr, target = make_pack_manager(tmp_path)
        res = mgr.install_pack(str(pack), target)
        assert res.ok and res.status == "success"
        assert res.results[0].status == "installed"
        assert "符号链接" in res.results[0].reason
        assert not os.path.exists(os.path.join(target, "excel", "link.txt"))
        assert not os.path.exists(os.path.join(target, "excel", "outside.txt"))