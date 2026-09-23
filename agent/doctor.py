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


def _check_env_sync(env_path: str = None, example_path: str = None) -> dict:
    """对比 .env 与 .env.example，报告 .env 里缺失的可调项。

    为什么需要：`.env` 含密钥不入库，从模板复制后就与仓库脱钩——项目后续
    新增的配置项不会自动出现在用户的 .env 里（典型困惑："这个配置我在
    .env 里怎么找不到"）。缺失项本身不影响运行（代码有默认值），但用户
    不知道它们可调。

    返回 ok=True（缺项只是提示，不是错误），message 里给出数量与示例。
    env_path / example_path 可注入（便于测试）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = env_path or os.path.join(root, ".env")
    example_path = example_path or os.path.join(root, ".env.example")
    if not os.path.exists(env_path) or not os.path.exists(example_path):
        return {"name": "配置同步", "ok": True, "message": "跳过（.env / .env.example 不完整）", "hint": ""}

    import re as _re

    def _parse(path: str) -> dict:
        out: dict = {}
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    commented = line.lstrip().startswith("#")
                    s = line.lstrip("#").strip() if commented else line.strip()
                    m = _re.match(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", s)
                    if m:
                        key, val = m.group(1), m.group(2).strip()
                        if key not in out or (out[key][1] and not commented):
                            out[key] = (val, commented)
        except OSError:
            pass
        return out

    env, example = _parse(env_path), _parse(example_path)
    # 只关心"模板里有真实赋值、而 .env 里完全没有"的项
    missing = sorted(k for k, (v, c) in example.items() if not c and k not in env)
    if not missing:
        return {"name": "配置同步", "ok": True,
                "message": f".env 已覆盖模板全部 {len(example)} 项", "hint": ""}
    shown = ", ".join(missing[:6])
    more = f" 等 {len(missing)} 项" if len(missing) > 6 else ""
    return {
        "name": "配置同步",
        "ok": True,   # 缺项不影响运行（都有代码默认值），仅提示可调项
        "message": f".env 未包含模板中的 {len(missing)} 项（用默认值）：{shown}{more}",
        "hint": "想调整某项时，把对应行从 .env.example 复制进 .env 即可（例如 MAX_LOOP_OPS=120）",
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


def _short_host(base_url: str) -> str:
    """端点简写（去掉协议与路径，用于报告里少占地方）。"""
    text = str(base_url or "")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.split("/")[0][:28] or "（未配置）"


def _subsystem_endpoints() -> List[dict]:
    """收集「子系统 → (端点, 密钥, 模型名)」清单，供 /models 对账。

    只收录**配了模型名**的条目；端点/密钥留空时跟随主 LLM（与各子系统的实际
    取值逻辑保持一致——否则这里会对账一个线上根本不用的组合）。
    """
    from config import (
        IMAGE_GEN_CONFIG, SMALL_MODEL_CONFIG, SUPERVISOR_CONFIG,
        VIDEO_GEN_CONFIG, VISION_CONFIG,
    )

    items: List[dict] = []

    def add(name, base, key, model):
        if model:
            items.append({"name": name, "base_url": base or LLM_CONFIG.get("base_url"),
                          "api_key": key or LLM_CONFIG.get("api_key"), "model": model})

    add("主模型", LLM_CONFIG.get("base_url"), LLM_CONFIG.get("api_key"),
        LLM_CONFIG.get("default_model"))
    if VISION_CONFIG.get("enabled", True):
        add("视觉", VISION_CONFIG.get("base_url"), VISION_CONFIG.get("api_key"),
            VISION_CONFIG.get("vision_model"))
        for model in VISION_CONFIG.get("fallback_models") or []:
            add("视觉备用", VISION_CONFIG.get("fallback_base_url"),
                VISION_CONFIG.get("fallback_api_key"), model)
    if GUARDIAN_CONFIG.get("enabled", False):
        add("Guardian", GUARDIAN_CONFIG.get("base_url"), GUARDIAN_CONFIG.get("api_key"),
            GUARDIAN_CONFIG.get("model"))
    if SMALL_MODEL_CONFIG.get("enabled", True):
        add("小快模型", SMALL_MODEL_CONFIG.get("base_url"), SMALL_MODEL_CONFIG.get("api_key"),
            SMALL_MODEL_CONFIG.get("model"))
    if IMAGE_GEN_CONFIG.get("enabled", True) and IMAGE_GEN_CONFIG.get("base_url"):
        add("文生图", IMAGE_GEN_CONFIG.get("base_url"), IMAGE_GEN_CONFIG.get("api_key"),
            IMAGE_GEN_CONFIG.get("model"))
        for model in IMAGE_GEN_CONFIG.get("fallback_models") or []:
            add("文生图备用", IMAGE_GEN_CONFIG.get("fallback_base_url"),
                IMAGE_GEN_CONFIG.get("fallback_api_key"), model)
    if VIDEO_GEN_CONFIG.get("enabled", True) and VIDEO_GEN_CONFIG.get("base_url"):
        add("文生视频", VIDEO_GEN_CONFIG.get("base_url"), VIDEO_GEN_CONFIG.get("api_key"),
            VIDEO_GEN_CONFIG.get("model"))
    if SUPERVISOR_CONFIG.get("enabled", True):
        add("监管者", SUPERVISOR_CONFIG.get("base_url"), SUPERVISOR_CONFIG.get("api_key"),
            SUPERVISOR_CONFIG.get("model"))
    return items


def _fetch_model_ids(base_url: str, api_key: str, timeout: float = 8.0) -> List[str]:
    """问端点的 /models，返回模型 id 列表（失败抛异常，由调用方归类）。"""
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        str(base_url).rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8", "replace"))
    return [str(m.get("id", "")) for m in (data.get("data") or [])]


def _check_subsystem_models(fetch=None, timeout: float = 8.0) -> dict:
    """对账：各子系统的「模型名 × 端点」是否真的对得上（逐个端点问 /models）。

    为什么需要（2026-09-23 实测踩到）：不少子系统的端点默认**跟随主 LLM**
    （`SMALL_MODEL_BASE_URL`、`IMAGE_GEN_FALLBACK_API_KEY`、`VISION_*`……），
    但模型名常是**厂商专有**的。主模型一换网关，那些名字在新端点上就不存在：
      · 小快模型 → 503 model_not_found，杂活（会话标题）**无声**退回规则实现；
      · 备用链 → key 与端点不匹配，401。
    这类故障运行时不报错，只表现为"功能悄悄变差"，所以做成自检项。

    端点不提供 /models（部分厂商没有该接口）时**不算失败**，只标注"未能核对"，
    避免误报；能核对的条目里模型缺失才算失败。但 **401/403 算失败**——那说明
    "key 与端点不匹配"，是实打实的配置错误（实测：主模型换端点后，留空即继承
    主 LLM key 的备用链就会拿 A 家的 key 去打 B 家的端点）。
    """
    fetch = fetch or _fetch_model_ids
    items = _subsystem_endpoints()
    if not items:
        return {"name": "子系统模型对账", "ok": True,
                "message": "没有需要核对的条目", "hint": ""}

    cache: dict = {}
    missing, unchecked, auth_failed = [], [], []
    for item in items:
        key = (str(item["base_url"]), str(item["api_key"]))
        if key not in cache:
            try:
                cache[key] = fetch(key[0], key[1], timeout)
            except Exception as e:                      # noqa: BLE001
                cache[key] = e
        got = cache[key]
        if isinstance(got, Exception):
            where = f"{item['name']}@{_short_host(item['base_url'])}"
            code = getattr(got, "code", None)
            if code in (401, 403):
                auth_failed.append(f"{where}（HTTP {code}：key 与该端点不匹配）")
            else:
                unchecked.append(f"{where}（{str(got)[:40]}）")
        elif item["model"] not in got:
            missing.append(f"{item['name']} 的 {item['model']}@{_short_host(item['base_url'])}")

    if missing or auth_failed:
        parts = []
        if missing:
            parts.append("这些模型在它配置的端点上**不存在**（会 503/404，且多在运行时"
                         "无声降级）：" + "；".join(missing))
        if auth_failed:
            parts.append("这些端点的**密钥不匹配**：" + "；".join(auth_failed))
        return {
            "name": "子系统模型对账",
            "ok": False,
            "message": "  ".join(parts),
            "hint": ("给该子系统显式配 *_BASE_URL/*_API_KEY，或换成该端点服务的模型。"
                     "典型场景：主模型换网关后，『留空即跟随主 LLM』的备用链会拿 A 家 key"
                     "打 B 家端点（401），厂商专有的模型名也会在新端点上消失（503）。"),
        }
    message = f"{len(items)} 个条目、{len(cache)} 个端点全部对得上"
    if unchecked:
        message += f"；{len(unchecked)} 项未能核对（端点无 /models）：" + "，".join(unchecked[:3])
    return {"name": "子系统模型对账", "ok": True, "message": message, "hint": ""}


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
        _check_git, _check_env, _check_env_sync, _check_tools, _check_ws_support,
        _check_policy, _check_hooks, _check_exec_policy, _check_skills, _check_sandbox,
    ]
    if include_llm:
        checks.insert(5, _check_llm)
        # 需要网络（逐个端点问 /models），所以与"主模型连通"同组，离线自检不跑
        checks.insert(6, _check_subsystem_models)
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
