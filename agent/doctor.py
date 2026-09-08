"""
环境自检模块（参考同类开源实现的环境自检）。

把"踩过的坑"固化为自动化检查，一次定位所有环境问题：
- Python / 依赖完整性（websockets、playwright、fastapi/uvicorn 等）
- Playwright 浏览器二进制
- git 与快照安全网
- .env 与 API key
- 主模型连通性（真实调用一次）
- 工具注册 / WebSocket 路由 / 当前安全策略

用法: my-agent --doctor
所有检查函数返回 {"name", "ok", "message", "hint"}，可单测。
"""
import os
import sys
import time
from typing import Callable, List

from config import (
    LLM_CONFIG, APPROVAL_CONFIG, GUARDIAN_CONFIG, SNAPSHOT_CONFIG,
    HOOKS_CONFIG, SKILLS_CONFIG, SANDBOX_EXEC_CONFIG,
)

# 关键依赖（导入名 → 安装提示）
REQUIRED_DEPS = [
    ("openai", "pip install openai"),
    ("dotenv", "pip install python-dotenv"),
    ("rich", "pip install rich"),
    ("fastapi", "pip install fastapi"),
    ("uvicorn", "pip install 'uvicorn[standard]'"),
    ("websockets", "pip install websockets"),
    ("playwright", "pip install playwright"),
    ("mcp", "pip install mcp"),
]


def _check_python() -> dict:
    v = sys.version_info
    ok = v >= (3, 9)
    return {
        "name": "Python 版本",
        "ok": ok,
        "message": f"Python {v.major}.{v.minor}.{v.micro}",
        "hint": "需要 Python 3.9+（建议 3.11+）" if not ok else "",
    }


def _check_deps() -> dict:
    missing = []
    for module, install_hint in REQUIRED_DEPS:
        try:
            __import__(module)
        except ImportError:
            missing.append(f"{module}（{install_hint}）")
    if missing:
        return {
            "name": "依赖完整性",
            "ok": False,
            "message": f"缺失 {len(missing)} 个依赖",
            "hint": "；".join(missing),
        }
    return {"name": "依赖完整性", "ok": True, "message": f"{len(REQUIRED_DEPS)} 个依赖齐全", "hint": ""}


def _check_playwright_browser() -> dict:
    try:
        # 优先直接探测默认安装目录（不启动 playwright 驱动，避免退出时的异步警告）
        import glob
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        if base:
            hits = glob.glob(os.path.join(base, "chromium-*", "chrome-win64", "chrome.exe"))
            if hits:
                return {
                    "name": "Playwright 浏览器",
                    "ok": True,
                    "message": f"chromium: {hits[0]}",
                    "hint": "",
                }
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            ok = os.path.exists(path)
            return {
                "name": "Playwright 浏览器",
                "ok": ok,
                "message": f"chromium: {path}" if ok else "chromium 二进制缺失",
                "hint": "playwright install chromium" if not ok else "",
            }
    except Exception as e:
        return {
            "name": "Playwright 浏览器",
            "ok": False,
            "message": f"检查失败: {str(e)[:80]}",
            "hint": "playwright install chromium",
        }


def _check_git() -> dict:
    try:
        import subprocess
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        if r.returncode != 0:
            return {"name": "Git", "ok": False, "message": "git 不可用", "hint": "安装 Git for Windows"}
        from agent.snapshot import is_git_repo
        work_dir = SNAPSHOT_CONFIG.get("work_dir") or os.getcwd()
        if not is_git_repo(work_dir):
            return {
                "name": "Git 快照安全网",
                "ok": False,
                "message": "项目不是 git 仓库（自我修改将无法回滚）",
                "hint": f"在 {work_dir} 执行: git init",
            }
        return {"name": "Git 快照安全网", "ok": True, "message": f"仓库就绪: {work_dir}", "hint": ""}
    except Exception as e:
        return {"name": "Git 快照安全网", "ok": False, "message": f"检查失败: {str(e)[:80]}", "hint": ""}


def _check_env() -> dict:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return {
            "name": ".env 配置",
            "ok": False,
            "message": "项目根目录缺少 .env",
            "hint": "复制 .env.example 为 .env 并填入 LLM_API_KEY",
        }
    if not LLM_CONFIG.get("api_key"):
        return {
            "name": ".env 配置",
            "ok": False,
            "message": "LLM_API_KEY 未设置",
            "hint": "在 .env 中填写 LLM_API_KEY",
        }
    return {
        "name": ".env 配置",
        "ok": True,
        "message": f"主模型: {LLM_CONFIG.get('default_model')} @ {LLM_CONFIG.get('base_url')}",
        "hint": "",
    }


def _check_llm(llm=None, timeout: int = 20) -> dict:
    """真实调用一次主模型（1 token）验证连通性。"""
    try:
        from models.llm import LLM
        llm = llm or LLM()
        t0 = time.time()
        llm.chat([{"role": "user", "content": "ping"}], max_tokens=1)
        elapsed = time.time() - t0
        return {
            "name": "主模型连通",
            "ok": True,
            "message": f"{LLM_CONFIG.get('default_model')} 响应正常（{elapsed:.1f}s）",
            "hint": "",
        }
    except Exception as e:
        return {
            "name": "主模型连通",
            "ok": False,
            "message": f"调用失败: {str(e)[:120]}",
            "hint": "检查 LLM_BASE_URL/LLM_API_KEY/网络可达性；内网模型需在该网络内",
        }


def _check_tools() -> dict:
    try:
        from tools.tool_manager import ToolManager
        tm = ToolManager()
        names = tm.list_tools()
        return {"name": "工具注册", "ok": True, "message": f"{len(names)} 个工具: {', '.join(names)}", "hint": ""}
    except Exception as e:
        return {"name": "工具注册", "ok": False, "message": f"失败: {str(e)[:80]}", "hint": ""}


def _check_ws_support() -> dict:
    """WebSocket 路由是否可用（Dashboard 依赖 websockets 库）。"""
    try:
        from dashboard.server import app
        from starlette.routing import WebSocketRoute
        ws = [r for r in app.routes if isinstance(r, WebSocketRoute)]
        if not ws:
            return {"name": "Dashboard WebSocket", "ok": False, "message": "/ws 路由未注册", "hint": "pip install websockets 后重启"}
        return {"name": "Dashboard WebSocket", "ok": True, "message": "/ws 路由就绪", "hint": ""}
    except ImportError:
        return {"name": "Dashboard WebSocket", "ok": False, "message": "websockets 依赖缺失", "hint": "pip install websockets"}
    except Exception as e:
        return {"name": "Dashboard WebSocket", "ok": False, "message": str(e)[:80], "hint": ""}


def _check_policy() -> dict:
    return {
        "name": "安全策略",
        "ok": True,
        "message": (
            f"审批 {APPROVAL_CONFIG.get('approval_policy')} · "
            f"沙箱 {APPROVAL_CONFIG.get('sandbox_mode')} · "
            f"Guardian {'开' if GUARDIAN_CONFIG.get('enabled') else '关'}"
        ),
        "hint": "",
    }


def _check_hooks() -> dict:
    """Hooks 文件健康：enabled 且文件存在时必须能通过语法解析。"""
    try:
        if not HOOKS_CONFIG.get("enabled"):
            return {"name": "Hooks 钩子", "ok": True, "message": "未启用", "hint": ""}
        path = HOOKS_CONFIG.get("hooks_file") or ""
        if not path or not os.path.exists(path):
            return {
                "name": "Hooks 钩子",
                "ok": False,
                "message": f"已启用但钩子文件不存在: {path}",
                "hint": "创建钩子文件，或设置 HOOKS_ENABLED=false",
            }
        import ast
        with open(path, "r", encoding="utf-8") as f:
            ast.parse(f.read())
        return {"name": "Hooks 钩子", "ok": True, "message": f"钩子文件语法正常: {path}", "hint": ""}
    except SyntaxError as e:
        return {
            "name": "Hooks 钩子",
            "ok": False,
            "message": f"钩子文件语法错误（line {e.lineno}），将 fail-open 跳过钩子",
            "hint": f"修复 {HOOKS_CONFIG.get('hooks_file')} 的语法",
        }
    except Exception as e:
        return {"name": "Hooks 钩子", "ok": False, "message": f"检查失败: {str(e)[:80]}", "hint": ""}


def _check_exec_policy() -> dict:
    """execpolicy 策略文件健康：enabled 时必须是合法的 JSON 规则数组。

    运行时 ExecPolicy.load 是 fail-open（坏文件静默置空规则），doctor 在这里
    用严格校验把坑提前亮出来——用户显式启用的策略文件坏了必须被看见。
    """
    try:
        if not APPROVAL_CONFIG.get("exec_policy_enabled"):
            return {"name": "execpolicy 策略", "ok": True, "message": "未启用", "hint": ""}
        path = APPROVAL_CONFIG.get("exec_policy_file") or ""
        if not path or not os.path.exists(path):
            return {
                "name": "execpolicy 策略",
                "ok": False,
                "message": f"已启用但策略文件不存在: {path}",
                "hint": "创建策略文件，或设置 APPROVAL_EXEC_POLICY_ENABLED=false",
            }
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {
                "name": "execpolicy 策略",
                "ok": False,
                "message": "策略文件应为规则数组（JSON list）",
                "hint": f"修复 {path} 的结构",
            }
        valid_decisions = {"allow", "deny", "ask"}
        bad = [
            i for i, rule in enumerate(data)
            if not isinstance(rule, dict)
            or not isinstance(rule.get("decision"), str)
            or rule["decision"].lower() not in valid_decisions
        ]
        if bad:
            return {
                "name": "execpolicy 策略",
                "ok": False,
                "message": f"{len(bad)} 条规则缺少合法 decision（allow/deny/ask）: 第 {bad} 条",
                "hint": f"修复 {path} 中对应规则",
            }
        return {
            "name": "execpolicy 策略",
            "ok": True,
            "message": f"策略文件合法: {path}（{len(data)} 条规则）",
            "hint": "",
        }
    except json.JSONDecodeError as e:
        return {
            "name": "execpolicy 策略",
            "ok": False,
            "message": f"策略文件 JSON 损坏（line {e.lineno}），运行时将 fail-open 忽略全部规则",
            "hint": f"修复 {APPROVAL_CONFIG.get('exec_policy_file')} 的 JSON 格式",
        }
    except Exception as e:
        return {
            "name": "execpolicy 策略",
            "ok": False,
            "message": f"检查失败: {str(e)[:80]}",
            "hint": f"检查 {APPROVAL_CONFIG.get('exec_policy_file')}",
        }


def _check_skills() -> dict:
    """Skills 目录健康：目录不存在是可选状态；存在但不可读才是问题。"""
    try:
        if not SKILLS_CONFIG.get("enabled"):
            return {"name": "Skills 技能", "ok": True, "message": "未启用", "hint": ""}
        from agent.skills import SkillManager
        mgr = SkillManager(
            project_dir=SKILLS_CONFIG.get("project_dir"),
            user_dir=SKILLS_CONFIG.get("user_dir"),
        )
        skills = mgr.discover()
        return {
            "name": "Skills 技能",
            "ok": True,
            "message": f"{len(skills)} 个技能已发现"
            + (": " + ", ".join(s.name for s in skills[:5]) if skills else ""),
            "hint": "",
        }
    except Exception as e:
        return {
            "name": "Skills 技能",
            "ok": False,
            "message": f"技能目录检查失败: {str(e)[:80]}",
            "hint": f"检查 {SKILLS_CONFIG.get('project_dir')} 与 {SKILLS_CONFIG.get('user_dir')}",
        }


def _check_sandbox() -> dict:
    """OS 级沙箱健康：off 时提示可选；appcontainer 模式必须平台可用。"""
    try:
        mode = SANDBOX_EXEC_CONFIG.get("mode", "off")
        if mode == "off":
            return {
                "name": "OS 级沙箱",
                "ok": True,
                "message": "未启用（SANDBOX_EXECUTION=off）",
                "hint": "",
            }
        from agent.sandbox import appcontainer_available
        if not appcontainer_available():
            return {
                "name": "OS 级沙箱",
                "ok": False,
                "message": f"模式 {mode} 在当前平台不可用",
                "hint": "AppContainer 仅支持 Windows 8+；非 Windows 请改回 off",
            }
        return {
            "name": "OS 级沙箱",
            "ok": True,
            "message": f"appcontainer 可用（网络 {'放行' if SANDBOX_EXEC_CONFIG.get('allow_network') else '全禁'}）",
            "hint": "",
        }
    except Exception as e:
        return {"name": "OS 级沙箱", "ok": False, "message": f"检查失败: {str(e)[:80]}", "hint": ""}


def run_doctor(include_llm: bool = True) -> List[dict]:
    """执行全部自检，返回结果列表。"""
    checks: List[Callable[[], dict]] = [
        _check_python, _check_deps, _check_playwright_browser,
        _check_git, _check_env, _check_tools, _check_ws_support, _check_policy,
        _check_hooks, _check_exec_policy, _check_skills, _check_sandbox,
    ]
    if include_llm:
        checks.insert(5, _check_llm)
    results = []
    for fn in checks:
        try:
            results.append(fn())
        except Exception as e:
            results.append({"name": fn.__name__, "ok": False, "message": f"异常: {str(e)[:100]}", "hint": ""})
    return results


def render_report(results: List[dict]) -> str:
    """渲染自检报告（终端文本）。"""
    lines = ["my_agent 环境自检", "─" * 40]
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"  [{mark}] {r['name']}: {r['message']}")
        if not r["ok"] and r.get("hint"):
            lines.append(f"      → 修复: {r['hint']}")
    ok_count = sum(1 for r in results if r["ok"])
    lines.append("─" * 40)
    lines.append(f"结果: {ok_count}/{len(results)} 项通过" + ("（全部健康 ✓）" if ok_count == len(results) else ""))
    return "\n".join(lines)
