"""固定任务集评测：隔离工作区运行 agent,机器可判定验收,汇总 pass@1 / pass@k。"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"

#: 看到 JSON 结果后再给 agent 多久退出(后台子进程可能攥着管道不放)
PAYLOAD_GRACE_SECONDS = 5.0

#: 运行期产物全部落在本次评测目录,不碰仓库的 memory/rollouts
_ISOLATION_ENV = {
    "AGENTS_ENABLED": "false",
    "SKILLS_ENABLED": "false",
    "LEARN_ENABLED": "false",
    "EXPERIENCE_ENABLED": "false",
    "SNAPSHOT_ENABLED": "false",
    "REPOMAP_ENABLED": "false",
    "EDIT_PREFLIGHT": "false",       # 别让 edit 去跑仓库的全量测试
    "APPROVAL_INTERACTIVE": "false",
    "APPROVAL_NONINTERACTIVE_ANSWER": "deny",
}


class TaskError(ValueError):
    """任务定义不合法。"""


def repo_dotenv(repo_root: Path = REPO_ROOT) -> dict:
    """读仓库 .env(而不是进程环境):在 Claude Code 里 ANTHROPIC_* 会压过 LLM_*。"""
    from dotenv import dotenv_values

    path = Path(repo_root) / ".env"
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v not in (None, "")}


def build_agent_env(run_dir: Path, model: str = None, no_fallback: bool = False,
                    extra_env: dict = None) -> dict:
    """构造子 agent 的环境:剥掉 ANTHROPIC_*,以 .env 为准,再叠加隔离目录。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.update(repo_dotenv())
    if model:
        env["LLM_DEFAULT_MODEL"] = model
        env.pop("MY_AGENT_MODEL", None)
    if no_fallback:
        env["LLM_FALLBACK_MODELS"] = ""
        env["VISION_FALLBACK_MODELS"] = ""
    run_dir = Path(run_dir)
    env.update({
        "SESSION_DIR": str(run_dir / "sessions"),
        "CONTEXT_STORE_DIR": str(run_dir / "ctx"),
        "RUN_LOG_DIR": str(run_dir / "logs"),
        "ROLLOUT_DIR": str(run_dir / "rollouts"),
        "MEMORY_DB_PATH": str(run_dir / "memory"),
    })
    env.update(_ISOLATION_ENV)
    if extra_env:
        env.update(extra_env)
    return env


def load_task(task_dir: Path) -> dict:
    """读取并校验一个任务目录(task.json + workspace/)。"""
    task_dir = Path(task_dir)
    manifest_path = task_dir / "task.json"
    if not manifest_path.exists():
        raise TaskError(f"缺少 task.json: {task_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise TaskError(f"task.json 不是合法 JSON: {task_dir}: {e}") from e
    task_id = str(manifest.get("id") or task_dir.name).strip()
    goal = str(manifest.get("goal") or "").strip()
    checks = manifest.get("checks")
    if not goal:
        raise TaskError(f"任务缺少 goal: {task_dir}")
    if not isinstance(checks, list) or not checks:
        raise TaskError(f"任务缺少 checks: {task_dir}")
    return {
        "id": task_id,
        "goal": goal,
        "checks": checks,
        "timeout_seconds": float(manifest.get("timeout_seconds", 300)),
        "max_ops": int(manifest.get("max_ops", 30)),
        "approval": str(manifest.get("approval", "never")),
        "sandbox": str(manifest.get("sandbox", "workspace-write")),
        "vision": bool(manifest.get("vision", False)),
        "tags": list(manifest.get("tags") or []),
        "path": str(task_dir),
        "workspace": str(task_dir / "workspace"),
    }


def discover_tasks(tasks_dir: Path) -> list:
    """按目录名排序发现任务。"""
    root = Path(tasks_dir)
    if not root.is_dir():
        raise TaskError(f"任务目录不存在: {root}")
    tasks = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "task.json").exists():
            tasks.append(load_task(child))
    return tasks


def _kill_tree(pid: int) -> None:
    """超时后连子进程一起杀(Windows 下的 terminal 工具会拉子进程)。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:                       # noqa: BLE001
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:                   # noqa: BLE001
            pass


def _parse_payload(stdout: str) -> dict:
    """取 stdout 里最后一行 JSON(main.py --output json 的契约)。"""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


def _session_stats(run_dir: Path, session_id: str = "") -> dict:
    """从隔离会话目录读消息数与实际使用的模型(优先精确匹配本次会话)。"""
    root = Path(run_dir) / "sessions"
    path = root / f"{session_id}.json" if session_id else None
    if path is None or not path.exists():
        files = list(root.glob("*.json"))
        if not files:
            return {}
        path = max(files, key=lambda p: p.stat().st_mtime)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "messages": len(payload.get("messages") or []),
        "model": str(payload.get("model") or ""),
        "title": str(payload.get("title") or ""),
    }


def _run_process(cmd: list, cwd: Path, env: dict, timeout_seconds: float,
                 grace_seconds: float = PAYLOAD_GRACE_SECONDS) -> dict:
    """跑子进程:看到结果 JSON 就视为完成,不无限等后台子进程关闭管道。"""
    popen_kwargs = dict(cwd=cwd, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                        errors="replace")
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    out_lines, err_lines, payload_box = [], [], {}
    done = threading.Event()

    def _read(stream, sink, detect):
        try:
            for line in stream:
                sink.append(line)
                if detect and not done.is_set():
                    parsed = _parse_payload(line)
                    if parsed and "ok" in parsed:
                        payload_box["payload"] = parsed
                        done.set()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:               # noqa: BLE001
                pass

    readers = [
        threading.Thread(target=_read, args=(proc.stdout, out_lines, True), daemon=True),
        threading.Thread(target=_read, args=(proc.stderr, err_lines, False), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.time() + timeout_seconds
    while not done.is_set() and proc.poll() is None and time.time() < deadline:
        done.wait(0.1)

    payload_seen = done.is_set()
    timed_out = False
    killed_after_payload = False
    if payload_seen and proc.poll() is None:
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            killed_after_payload = True
            _kill_tree(proc.pid)
    elif not payload_seen and proc.poll() is None:
        timed_out = True
        _kill_tree(proc.pid)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
    for reader in readers:
        reader.join(timeout=2)

    stdout = "".join(out_lines)
    stderr = "".join(err_lines)
    return {
        "payload": payload_box.get("payload") or _parse_payload(stdout),
        "payload_seen": payload_seen,
        "timed_out": timed_out,
        "killed_after_payload": killed_after_payload,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _check_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def run_checks(task: dict, workspace: Path, check_env: dict = None) -> list:
    """逐条执行任务验收;每条都必须通过。"""
    # 统一成绝对路径:custom 检查的子进程 cwd 是工作区,相对路径会解析错
    workspace = Path(workspace).resolve()
    env = check_env or _check_env()
    results = []
    for index, check in enumerate(task["checks"]):
        if not isinstance(check, dict):
            results.append({"name": f"check[{index}]", "passed": False,
                            "detail": "检查项必须是对象"})
            continue
        kind = str(check.get("type") or "").strip().lower()
        name = str(check.get("name") or f"{kind or 'check'}[{index}]")
        try:
            passed, detail = _run_one_check(kind, check, workspace, env, task)
        except Exception as e:              # noqa: BLE001 — 验收脚本自身出错按失败处理
            passed, detail = False, f"{type(e).__name__}: {e}"
        results.append({"name": name, "passed": bool(passed), "detail": detail})
    return results


def _run_one_check(kind: str, check: dict, workspace: Path, env: dict, task: dict):
    if kind == "command":
        cmd = [str(part).replace("{python}", sys.executable)
               for part in (check.get("cmd") or [])]
        if not cmd:
            return False, "command 检查缺少 cmd"
        proc = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=float(check.get("timeout", 120)))
        expect = int(check.get("expect_exit", 0))
        detail = (proc.stdout or proc.stderr or "").strip()[-1200:]
        return proc.returncode == expect, f"exit={proc.returncode} (期望 {expect}) {detail}"
    if kind in ("file_contains", "file_not_contains"):
        path = workspace / str(check.get("path") or "")
        text = str(check.get("text") or "")
        if not path.exists():
            return False, f"文件不存在: {path.name}"
        content = path.read_text(encoding="utf-8", errors="replace")
        if check.get("ignore_case"):
            content, text = content.lower(), text.lower()
        found = text in content
        return (found if kind == "file_contains" else not found), \
            f"{path.name} {'包含' if found else '不包含'} {text!r}"
    if kind == "file_equals":
        path = workspace / str(check.get("path") or "")
        if not path.exists():
            return False, f"文件不存在: {path.name}"
        content = path.read_text(encoding="utf-8", errors="replace")
        expected = str(check.get("text") or "")
        if check.get("strip", True):
            content, expected = content.strip(), expected.strip()
        return content == expected, f"{path.name} 内容{'一致' if content == expected else '不一致'}"
    if kind == "file_exists":
        path = workspace / str(check.get("path") or "")
        return path.exists(), f"{path.name} {'存在' if path.exists() else '不存在'}"
    if kind == "file_absent":
        path = workspace / str(check.get("path") or "")
        return not path.exists(), f"{path.name} {'不存在' if not path.exists() else '仍然存在'}"
    if kind == "custom":
        script = Path(task["path"]) / str(check.get("script") or "check.py")
        if not script.exists():
            return False, f"缺少自定义验收脚本: {script}"
        proc = subprocess.run([sys.executable, str(script), str(workspace)],
                              cwd=workspace, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=float(check.get("timeout", 60)))
        detail = (proc.stdout or proc.stderr or "").strip()[-1200:]
        return proc.returncode == 0, f"exit={proc.returncode} {detail}"
    return False, f"未知检查类型: {kind!r}"


def run_agent(task: dict, run_root: Path, model: str = None,
              no_fallback: bool = False) -> dict:
    """在独立工作区跑一次 agent,返回原始结果(不做验收)。"""
    run_root = Path(run_root).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    workspace = run_root / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    source = Path(task["workspace"])
    if source.is_dir():
        shutil.copytree(source, workspace)
    else:
        workspace.mkdir(parents=True)

    session_id = f"eval-{task['id']}-{uuid.uuid4().hex[:6]}"
    env = build_agent_env(run_root, model=model, no_fallback=no_fallback,
                          extra_env={"PYTHONPATH": str(REPO_ROOT)})
    cmd = [
        sys.executable, str(REPO_ROOT / "main.py"),
        "--session", session_id,
        "-p", task["goal"],
        "--output", "json", "--no-stream",
        "--approval", task["approval"],
        "--sandbox", task["sandbox"],
        "--max-step-ops", str(task["max_ops"]),
    ]
    if not task.get("vision"):
        cmd.append("--no-vision")

    started = time.time()
    outcome = _run_process(cmd, workspace, env, task["timeout_seconds"])
    duration = round(time.time() - started, 2)
    payload = outcome["payload"]
    return {
        "session_id": session_id,
        "workspace": str(workspace),
        "duration_sec": duration,
        "timed_out": outcome["timed_out"],
        "killed_after_payload": outcome["killed_after_payload"],
        "exit_code": outcome["exit_code"],
        "agent_ok": bool(payload.get("ok")),
        "agent_error": str(payload.get("error") or ""),
        "agent_output": str(payload.get("output") or ""),
        "stderr_tail": (outcome["stderr"] or "").strip()[-2000:],
        **_session_stats(run_root, session_id),
    }


def evaluate_task(task: dict, out_root: Path, run_index: int, model: str = None,
                  no_fallback: bool = False) -> dict:
    """跑一次任务并验收,写单次结果 JSON。"""
    run_root = Path(out_root) / "workspaces" / f"{task['id']}-r{run_index}"
    record = run_agent(task, run_root, model=model, no_fallback=no_fallback)
    check_results = run_checks(task, Path(record["workspace"]))
    record.update({
        "task_id": task["id"],
        "run": run_index,
        "checks": check_results,
        "passed": all(c["passed"] for c in check_results) and not record["timed_out"],
    })
    runs_dir = Path(out_root) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{task['id']}-r{run_index}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def summarize(records: list, runs_per_task: int) -> dict:
    """汇总 pass@1(每次尝试)与 pass@k(每个任务 k 次全过)。"""
    by_task = {}
    for record in records:
        by_task.setdefault(record["task_id"], []).append(record)
    tasks = {}
    for task_id, items in by_task.items():
        passed = sum(1 for item in items if item["passed"])
        durations = [item["duration_sec"] for item in items]
        tasks[task_id] = {
            "runs": len(items),
            "passed": passed,
            "pass_rate": round(passed / len(items), 4) if items else 0.0,
            "all_pass": passed == len(items) == runs_per_task,
            "mean_duration_sec": round(sum(durations) / len(durations), 2) if durations else 0.0,
            "failed_checks": [
                f"r{item['run']}:{check['name']}"
                for item in items for check in item["checks"] if not check["passed"]
            ],
        }
    attempts = len(records)
    passed_attempts = sum(1 for record in records if record["passed"])
    total_duration = sum(record["duration_sec"] for record in records)
    return {
        "tasks": tasks,
        "task_count": len(tasks),
        "attempts": attempts,
        "passed_attempts": passed_attempts,
        "pass@1": round(passed_attempts / attempts, 4) if attempts else 0.0,
        "pass@k": round(sum(1 for item in tasks.values() if item["all_pass"]) / len(tasks), 4)
        if tasks else 0.0,
        "runs_per_task": runs_per_task,
        "agent_ok_rate": round(sum(1 for r in records if r["agent_ok"]) / attempts, 4)
        if attempts else 0.0,
        "total_duration_sec": round(total_duration, 2),
    }


def render_markdown(report: dict) -> str:
    """人读的汇总报告。"""
    meta = report["meta"]
    summary = report["summary"]
    lines = [
        "# Eval report",
        "",
        f"- 时间: {meta['created_at']}",
        f"- 模型: `{meta.get('model') or '(取 .env)'}`"
        f"{'（禁用备用链）' if meta.get('no_fallback') else ''}",
        f"- 任务: {summary['task_count']} 个 × {summary['runs_per_task']} 次",
        f"- **pass@1: {summary['pass@1']:.1%}** "
        f"({summary['passed_attempts']}/{summary['attempts']})",
        f"- **pass@{summary['runs_per_task']}: {summary['pass@k']:.1%}**"
        + ("（每个任务全部尝试通过的比例）" if summary["runs_per_task"] > 1
           else "（单次运行,与 pass@1 相同）"),
        f"- agent 自报成功: {summary['agent_ok_rate']:.1%}",
        f"- 总耗时: {summary['total_duration_sec']:.0f}s",
        "",
        "| 任务 | 通过 | 尝试 | 平均耗时 | 失败验收 |",
        "|---|---:|---:|---:|---|",
    ]
    for task_id, item in summary["tasks"].items():
        failed = ", ".join(item["failed_checks"][:4]) or "-"
        lines.append(f"| {task_id} | {item['passed']} | {item['runs']} | "
                     f"{item['mean_duration_sec']:.0f}s | {failed} |")
    return "\n".join(lines) + "\n"


def run_cli(argv: list = None) -> int:
    """命令行入口:发现任务 → 逐个跑 → 写 JSON/Markdown 报告。"""
    import argparse

    parser = argparse.ArgumentParser(description="固定任务集评测")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS_DIR), help="任务集目录")
    parser.add_argument("--task", action="append", default=[], help="只跑指定任务(可重复)")
    parser.add_argument("--runs", type=int, default=1, help="每个任务跑几次")
    parser.add_argument("--model", default="", help="覆盖 .env 的主模型")
    parser.add_argument("--no-fallback", action="store_true", help="禁用备用模型链")
    parser.add_argument("--out", default="", help="结果目录(默认 eval/results/<时间>)")
    parser.add_argument("--list", action="store_true", help="只列出任务")
    args = parser.parse_args(argv)

    tasks = discover_tasks(Path(args.tasks))
    if args.task:
        wanted = set(args.task)
        tasks = [task for task in tasks if task["id"] in wanted]
        missing = wanted - {task["id"] for task in tasks}
        if missing:
            print(f"找不到任务: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not tasks:
        print("没有可执行的任务。", file=sys.stderr)
        return 2
    if args.list:
        for task in tasks:
            print(f"{task['id']}: {task['goal'][:60]}")
        return 0
    if args.runs < 1:
        print("--runs 必须 >= 1", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out) if args.out else (
        Path(__file__).resolve().parent / "results" / stamp)
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"评测 {len(tasks)} 个任务 × {args.runs} 次 → {out_root}")
    records = []
    for task in tasks:
        for run_index in range(1, args.runs + 1):
            print(f"· {task['id']} r{run_index} ...", end="", flush=True)
            record = evaluate_task(task, out_root, run_index,
                                   model=args.model or None,
                                   no_fallback=args.no_fallback)
            records.append(record)
            state = "PASS" if record["passed"] else "FAIL"
            print(f" {state} {record['duration_sec']}s")
            if not record["passed"]:
                for check in record["checks"]:
                    if not check["passed"]:
                        print(f"    - {check['name']}: {check['detail'][:200]}")

    summary = summarize(records, args.runs)
    report = {
        "meta": {
            "created_at": stamp,
            "model": args.model or None,
            "no_fallback": args.no_fallback,
            "tasks_dir": str(Path(args.tasks).resolve()),
            "git_commit": _git_commit(),
        },
        "summary": summary,
        "records": records,
    }
    (out_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    (out_root / "report.md").write_text(markdown, encoding="utf-8")
    print()
    print(markdown)
    return 0 if summary["passed_attempts"] == summary["attempts"] else 1


def _git_commit() -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
        return proc.stdout.strip()
    except Exception:                       # noqa: BLE001
        return ""
