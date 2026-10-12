"""运行态自省（`main.py --ps`）：进程存活判断、凭据/PID 文件解读、孤儿标注不误报。"""
import json
import os
import time

import pytest

import agent.status as status


class TestPidAlive:
    def test_current_process_is_alive(self):
        assert status.pid_alive(os.getpid()) is True

    def test_bogus_pid_is_not_alive(self):
        assert status.pid_alive(999999) is False

    @pytest.mark.parametrize("bad", ["", None, "abc", -1, 0])
    def test_bad_values_are_false_not_crash(self, bad):
        assert status.pid_alive(bad) is False


class TestCollectAndRender:
    def _fake_ipc(self, tmp_path, monkeypatch, payload):
        import config
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        os.makedirs(tmp_path / "memory", exist_ok=True)
        with open(tmp_path / "memory" / "ipc.json", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return tmp_path / "memory" / "ipc.json"

    def test_dashboard_credentials_are_reported(self, tmp_path, monkeypatch):
        self._fake_ipc(tmp_path, monkeypatch, {"host": "127.0.0.1", "port": 8080,
                                               "pid": os.getpid(), "token": "t",
                                               "started_at": "2026-10-12T10:00:00"})
        s = status.collect()
        assert s["dashboard"]["alive"] is True
        out = status.render(s)
        assert "面板" in out and "在跑" in out and "8080" in out

    def test_stale_credential_file_is_called_out(self, tmp_path, monkeypatch):
        self._fake_ipc(tmp_path, monkeypatch, {"host": "0.0.0.0", "port": 8080,
                                               "pid": 999999, "token": "t"})
        out = status.render(status.collect())
        assert "已退出" in out, "残留的凭据文件要说明，别让人以为还在跑"

    def test_qq_pid_file(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        os.makedirs(tmp_path / "memory", exist_ok=True)
        (tmp_path / "memory" / "qqbot.pid").write_text(
            json.dumps({"pid": os.getpid(), "started_at": "2026-10-12T10:00:00"}), encoding="utf-8")
        out = status.render(status.collect())
        assert "QQ 桥" in out and "在跑" in out

    def test_running_task_is_not_labelled_orphan(self, tmp_path, monkeypatch):
        """刚还在写事件 = 可能正在跑，不能吓人地说成孤儿。"""
        import config
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        from agent import recover
        rollout = tmp_path / "memory" / "rollout"
        rollout.mkdir(parents=True)
        (rollout / "run-20990101-000000-1.jsonl").write_text(
            json.dumps({"event": "run_start", "data": {"goal": "在跑的任务"}}) + "\n",
            encoding="utf-8")
        monkeypatch.setattr(recover, "rollout_dir", lambda: str(rollout))
        out = status.render(status.collect())
        assert "无收尾" in out
        assert "可能正在跑" in out
        assert "孤儿运行" not in out, "别用孤儿这个吓人的词"


class TestPsFlag:
    def test_ps_is_registered_and_skips_preflight(self, monkeypatch):
        from config import LLM_CONFIG
        from main import _preflight_error, build_parser
        args = build_parser().parse_args(["--ps"])
        assert args.ps is True
        monkeypatch.setitem(LLM_CONFIG, "api_key", "")
        assert _preflight_error(args) == "", "--ps 不看模型，缺密钥也要能跑"

    def test_qqbot_writes_and_clears_pid_file(self, tmp_path, monkeypatch):
        import config
        from agent.qqbot import _clear_pid_file, _write_pid_file
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        path = _write_pid_file()
        assert os.path.isfile(path)
        data = json.loads(open(path, encoding="utf-8").read())
        assert data["pid"] == os.getpid() and data["started_at"]
        _clear_pid_file(path)
        assert not os.path.exists(path), "桥退出后 PID 文件不该留着"

    def test_started_at_is_recent(self, tmp_path, monkeypatch):
        import config
        from agent.qqbot import _clear_pid_file, _write_pid_file
        monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path), raising=False)
        path = _write_pid_file()
        try:
            assert time.time() - os.path.getmtime(path) < 10
        finally:
            _clear_pid_file(path)
