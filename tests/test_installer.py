"""
插件安装器测试（InstallerTool：技能包 / MCP 插件）。
技能包复用 SkillPackManager 安全边界；MCP 持久化写用户级配置。
全部不依赖网络。
"""
import json
import os

import pytest

import config as config_mod
from tools.installer import InstallerTool


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """重定向技能目录与 MCP 用户配置到临时目录，并构造工具实例。"""
    skills_target = tmp_path / "skills"
    skills_user = tmp_path / "skills_user"
    skills_target.mkdir()
    skills_user.mkdir()
    monkeypatch.setitem(config_mod.SKILLS_CONFIG, "project_dir", str(skills_target))
    monkeypatch.setitem(config_mod.SKILLS_CONFIG, "user_dir", str(skills_user))
    monkeypatch.setattr(config_mod, "MCP_USER_CONFIG_FILE", str(tmp_path / "mcp_servers.json"))
    tool = InstallerTool(tool_manager=FakeToolManager())
    return tmp_path, tool


class FakeToolManager:
    """记录 connect/disconnect 调用的假 ToolManager。"""

    def __init__(self):
        self.connected = {}
        self.disconnected = []

    def connect_mcp_server(self, name, command, args, env):
        self.connected[name] = {"command": command, "args": args, "env": env}
        return True

    def disconnect_mcp_server(self, name=None):
        if name:
            self.connected.pop(name, None)
            self.disconnected.append(name)

    def list_mcp_servers(self):
        return list(self.connected.keys())

    def list_tools(self):
        return ["fs_read", "fs_write"]


def make_skill_pack(base, skills):
    """构造未签名技能包：base/<skill>/SKILL.md。"""
    pack = base / "pack"
    for s in skills:
        d = pack / s
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\ndescription: {s} 的说明\ntriggers: {s}, {s}工具\n---\n{s} 技能正文",
            encoding="utf-8",
        )
    return pack


def read_user_config(tmp_path):
    p = tmp_path / "mcp_servers.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# skill_install / skill_list
# ----------------------------------------------------------------------

def test_skill_install_unsigned_rejected(env):
    tmp_path, tool = env
    pack = make_skill_pack(tmp_path, ["excel"])
    r = tool.execute_json({"command": "skill_install", "pack_path": str(pack), "target": "project"})
    assert not r.success
    assert "未签名" in (r.error or "")
    assert not (tmp_path / "skills" / "excel" / "SKILL.md").exists()


def test_skill_install_allow_unsigned_ok(env):
    tmp_path, tool = env
    pack = make_skill_pack(tmp_path, ["excel"])
    r = tool.execute_json({"command": "skill_install", "pack_path": str(pack),
                           "target": "project", "allow_unsigned": True})
    assert r.success
    assert (tmp_path / "skills" / "excel" / "SKILL.md").exists()


def test_skill_install_missing_pack_path(env):
    _, tool = env
    r = tool.execute_json({"command": "skill_install", "pack_path": "/no/such/dir"})
    assert not r.success
    assert "不存在" in r.error


def test_skill_list_shows_installed(env):
    tmp_path, tool = env
    pack = make_skill_pack(tmp_path, ["poetry"])
    tool.execute_json({"command": "skill_install", "pack_path": str(pack),
                       "target": "user", "allow_unsigned": True})
    r = tool.execute_json({"command": "skill_list"})
    assert r.success
    assert "poetry" in r.output
    assert "poetry 的说明" in r.output


def test_skill_install_string_interface(env):
    tmp_path, tool = env
    pack = make_skill_pack(tmp_path, ["excel"])
    r = tool.execute(f"skill_install {pack} user --allow-unsigned")
    assert r.success
    assert (tmp_path / "skills_user" / "excel" / "SKILL.md").exists()


# ----------------------------------------------------------------------
# mcp_add / mcp_list / mcp_remove
# ----------------------------------------------------------------------

def test_mcp_add_persists_and_connects(env):
    tmp_path, tool = env
    r = tool.execute_json({
        "command": "mcp_add",
        "name": "fs",
        "mcp_command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    })
    assert r.success
    assert "已安装并连接" in r.output
    assert "fs" in tool._tool_manager.connected
    cfg = read_user_config(tmp_path)
    assert cfg and cfg[0]["name"] == "fs"
    assert cfg[0]["command"] == "npx"
    assert cfg[0]["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_mcp_add_requires_name_command(env):
    _, tool = env
    r = tool.execute_json({"command": "mcp_add", "name": "x"})
    assert not r.success
    assert "name 与 command" in r.error


def test_mcp_add_duplicate_rejected_then_overwrite(env):
    tmp_path, tool = env
    base = {"command": "mcp_add", "name": "fs", "mcp_command": "npx", "args": ["a"]}
    assert tool.execute_json(base).success
    r2 = tool.execute_json(base)   # 同名
    assert not r2.success
    assert "已安装" in r2.error
    r3 = tool.execute_json({**base, "overwrite": True, "args": ["b"]})
    assert r3.success
    cfg = read_user_config(tmp_path)
    assert cfg and len(cfg) == 1 and cfg[0]["args"] == ["b"]


def test_mcp_add_connect_failure_not_persisted(env):
    tmp_path, tool = env
    tool._tool_manager.connect_mcp_server = lambda *a, **k: False
    r = tool.execute_json({"command": "mcp_add", "name": "bad", "mcp_command": "nope"})
    assert not r.success
    assert "连接失败" in r.error
    assert read_user_config(tmp_path) is None


def test_mcp_list_and_remove(env):
    tmp_path, tool = env
    tool.execute_json({"command": "mcp_add", "name": "fs", "mcp_command": "npx"})
    r = tool.execute_json({"command": "mcp_list"})
    assert r.success and "fs" in r.output and "已连接" in r.output

    r2 = tool.execute_json({"command": "mcp_remove", "name": "fs"})
    assert r2.success
    assert "fs" not in read_user_config(tmp_path)
    assert "fs" not in tool._tool_manager.connected

    r3 = tool.execute_json({"command": "mcp_remove", "name": "fs"})
    assert not r3.success


def test_mcp_string_interface(env):
    tmp_path, tool = env
    r = tool.execute("mcp_add fs npx -y @modelcontextprotocol/server-filesystem /tmp")
    assert r.success
    cfg = read_user_config(tmp_path)
    assert cfg and cfg[0]["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


# ----------------------------------------------------------------------
# 审批元数据 & 注册 & config 合并
# ----------------------------------------------------------------------

def test_approval_risk_levels(env):
    _, tool = env
    high = tool.build_approval_request({"command": "mcp_add"})
    assert high.risk_level == "high"
    med = tool.build_approval_request({"command": "skill_install"})
    assert med.risk_level == "medium"
    low = tool.build_approval_request({"command": "mcp_list"})
    assert low.risk_level == "low"


def test_tool_manager_registers_installer():
    from tools.tool_manager import ToolManager
    tm = ToolManager()
    t = tm.get_tool("installer")
    assert t is not None and t.name == "installer"


def test_config_merges_user_mcp_servers(tmp_path, monkeypatch):
    user_file = tmp_path / "mcp.json"
    user_file.write_text(json.dumps([
        {"name": "fs", "command": "npx", "args": ["a"]},
        {"name": "shared", "command": "user-cmd"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config_mod, "MCP_USER_CONFIG_FILE", str(user_file))
    env_servers = [{"name": "shared", "command": "env-cmd"}]
    merged = config_mod._merge_user_mcp_servers(env_servers)
    names = {s["name"] for s in merged}
    assert names == {"fs", "shared"}
    by_name = {s["name"]: s for s in merged}
    assert by_name["shared"]["command"] == "user-cmd"   # 用户级覆盖
    assert by_name["fs"].get("installed") is True
