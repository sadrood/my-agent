"""评测框架自身：任务发现、验收、环境隔离、汇总。不调用真实 LLM。"""
import json
import sys
from pathlib import Path

import pytest

from eval import runner


def _write_task(tmp_path, task_id="t1", checks=None, files=None, manifest_extra=None):
    task_dir = tmp_path / task_id
    workspace = task_dir / "workspace"
    workspace.mkdir(parents=True)
    for name, content in (files or {}).items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = {"id": task_id, "goal": "做个任务", "checks": checks or [
        {"name": "exists", "type": "file_exists", "path": "out.txt"}]}
    manifest.update(manifest_extra or {})
    (task_dir / "task.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                        encoding="utf-8")
    return task_dir


def test_discover_and_load(tmp_path):
    _write_task(tmp_path, "b-task")
    _write_task(tmp_path, "a-task")
    tasks = runner.discover_tasks(tmp_path)
    assert [task["id"] for task in tasks] == ["a-task", "b-task"]
    assert tasks[0]["timeout_seconds"] == 300
    assert tasks[0]["max_ops"] == 30
    assert tasks[0]["vision"] is False


def test_manifest_validation(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "task.json").write_text('{"id": "x"}', encoding="utf-8")
    with pytest.raises(runner.TaskError, match="goal"):
        runner.load_task(bad)
    (bad / "task.json").write_text('{"id": "x", "goal": "g"}', encoding="utf-8")
    with pytest.raises(runner.TaskError, match="checks"):
        runner.load_task(bad)


def test_checks_all_types(tmp_path):
    task_dir = _write_task(tmp_path, files={"a.txt": "hello 60\n", "gone.txt": "x"})
    task = runner.load_task(task_dir)
    task["checks"] = [
        {"name": "contains", "type": "file_contains", "path": "a.txt", "text": "60"},
        {"name": "case", "type": "file_contains", "path": "a.txt", "text": "HELLO",
         "ignore_case": True},
        {"name": "not", "type": "file_not_contains", "path": "a.txt", "text": "world"},
        {"name": "equals", "type": "file_equals", "path": "a.txt", "text": "hello 60"},
        {"name": "exists", "type": "file_exists", "path": "a.txt"},
        {"name": "absent", "type": "file_absent", "path": "nope.txt"},
        {"name": "cmd", "type": "command",
         "cmd": ["{python}", "-c", "print('ok')"]},
    ]
    results = runner.run_checks(task, task_dir / "workspace")
    assert all(item["passed"] for item in results), results


def test_checks_failure_details(tmp_path):
    task_dir = _write_task(tmp_path, files={"a.txt": "hello"})
    task = runner.load_task(task_dir)
    task["checks"] = [
        {"name": "missing-file", "type": "file_contains", "path": "b.txt", "text": "x"},
        {"name": "wrong-content", "type": "file_equals", "path": "a.txt", "text": "bye"},
        {"name": "bad-exit", "type": "command",
         "cmd": ["{python}", "-c", "raise SystemExit(3)"], "expect_exit": 0},
        {"name": "unknown", "type": "magic"},
    ]
    results = runner.run_checks(task, task_dir / "workspace")
    assert [item["passed"] for item in results] == [False, False, False, False]
    assert all(item["detail"] for item in results)


def test_custom_check_script(tmp_path):
    task_dir = _write_task(tmp_path, files={"a.txt": "hello"})
    (task_dir / "check.py").write_text(
        "import sys, pathlib\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "assert (root / 'a.txt').read_text(encoding='utf-8') == 'hello'\n",
        encoding="utf-8")
    task = runner.load_task(task_dir)
    task["checks"] = [{"name": "custom", "type": "custom"}]
    results = runner.run_checks(task, task_dir / "workspace")
    assert results[0]["passed"], results


def test_custom_check_resolves_relative_workspace(tmp_path, monkeypatch):
    """--out 传相对路径时,custom 检查的子进程 cwd 是工作区,必须拿到绝对路径。"""
    task_dir = _write_task(tmp_path, "rel", files={"a.txt": "x"})
    (task_dir / "check.py").write_text(
        "import sys, pathlib\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "assert root.is_absolute(), sys.argv[1]\n"
        "assert (root / 'a.txt').exists()\n",
        encoding="utf-8")
    task = runner.load_task(task_dir)
    task["checks"] = [{"name": "custom", "type": "custom"}]
    monkeypatch.chdir(tmp_path)
    results = runner.run_checks(task, Path("rel") / "workspace")
    assert results[0]["passed"], results


def test_build_agent_env_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-flash")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-cli")
    monkeypatch.setattr(runner, "repo_dotenv",
                        lambda repo_root=None: {"LLM_DEFAULT_MODEL": "glm-5.2",
                                                "LLM_API_KEY": "sk-repo"})
    env = runner.build_agent_env(tmp_path, model="my-model", no_fallback=True)
    assert "ANTHROPIC_MODEL" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["LLM_DEFAULT_MODEL"] == "my-model"
    assert env["LLM_API_KEY"] == "sk-repo"
    assert env["LLM_FALLBACK_MODELS"] == ""
    assert env["SESSION_DIR"] == str(tmp_path / "sessions")
    assert env["AGENTS_ENABLED"] == "false"
    assert env["LEARN_ENABLED"] == "false"


def test_agent_env_keeps_dotenv_model_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "repo_dotenv",
                        lambda repo_root=None: {"LLM_DEFAULT_MODEL": "glm-5.2"})
    env = runner.build_agent_env(tmp_path)
    assert env["LLM_DEFAULT_MODEL"] == "glm-5.2"
    assert "ANTHROPIC_MODEL" not in env


def test_parse_payload_takes_last_json_line():
    out = 'log line\n{"ok": false}\nnoise\n{"ok": true, "output": "done"}\n'
    assert runner._parse_payload(out) == {"ok": True, "output": "done"}
    assert runner._parse_payload("no json") == {}


def test_run_process_stops_after_payload(tmp_path):
    """打印结果后挂住的子进程不能拖住整轮评测（后台任务会攥着管道）。"""
    script = ("import json, time; "
              "print(json.dumps({'ok': True, 'output': 'x'}), flush=True); "
              "time.sleep(60)")
    outcome = runner._run_process([sys.executable, "-c", script], tmp_path,
                                  runner._check_env(), timeout_seconds=30,
                                  grace_seconds=0.5)
    assert outcome["payload_seen"] is True
    assert outcome["timed_out"] is False
    assert outcome["killed_after_payload"] is True
    assert outcome["payload"]["ok"] is True


def test_run_process_timeout_without_payload(tmp_path):
    outcome = runner._run_process([sys.executable, "-c", "import time; time.sleep(60)"],
                                  tmp_path, runner._check_env(),
                                  timeout_seconds=0.5, grace_seconds=0.1)
    assert outcome["payload_seen"] is False
    assert outcome["timed_out"] is True


def test_session_stats_prefers_exact_session(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.json").write_text(json.dumps(
        {"messages": [1, 2, 3], "model": "m1", "title": "t1"}), encoding="utf-8")
    (sessions / "other.json").write_text(json.dumps(
        {"messages": [1], "model": "other"}), encoding="utf-8")
    stats = runner._session_stats(tmp_path, "s1")
    assert stats["messages"] == 3 and stats["model"] == "m1"


def _record(task_id, run, passed, duration=1.0, agent_ok=True, checks=None):
    return {"task_id": task_id, "run": run, "passed": passed,
            "duration_sec": duration, "agent_ok": agent_ok,
            "checks": checks or [{"name": "c", "passed": passed, "detail": ""}]}


def test_summarize_pass_at_1_and_k():
    records = [
        _record("a", 1, True), _record("a", 2, True),
        _record("b", 1, True), _record("b", 2, False),
        _record("c", 1, False), _record("c", 2, False),
    ]
    summary = runner.summarize(records, runs_per_task=2)
    assert summary["pass@1"] == pytest.approx(3 / 6)
    assert summary["pass@k"] == pytest.approx(1 / 3, abs=1e-4)   # 只有 a 两次全过
    assert summary["tasks"]["b"]["pass_rate"] == 0.5
    assert summary["tasks"]["b"]["all_pass"] is False
    assert summary["tasks"]["a"]["all_pass"] is True
    assert summary["agent_ok_rate"] == 1.0


def test_render_markdown_contains_key_numbers():
    summary = runner.summarize([_record("a", 1, True)], runs_per_task=1)
    report = {"meta": {"created_at": "now", "model": "m", "no_fallback": True},
              "summary": summary}
    text = runner.render_markdown(report)
    assert "pass@1: 100.0%" in text
    assert "| a | 1 | 1 |" in text
