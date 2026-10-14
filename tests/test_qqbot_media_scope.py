"""补发本地媒体的两道安全约束：只发工程内产物、单文件不超上限。

背景：QQ 桥会把回复里提到的本地图片/音频直接发进聊天，方便用户不用登服务器取图；
但"回复里提到的任意绝对路径"是 agent 可控的 —— 不加约束就等于任何一个本机文件都可能被发到外部。
"""
import asyncio
import os
import time

import pytest

from agent import run_log
from agent.qqbot import QQBotBridge, QQBotConfig


def _cfg(**kw):
    base = {"app_id": "1", "app_secret": "s", "allow_from": "me"}
    base.update(kw)
    return QQBotConfig(base)


class _Api:
    """最小可用的假 botpy api：记录发出的媒体消息。"""

    def __init__(self):
        self.sent = []

    async def post_c2c_message(self, **kw):
        self.sent.append(kw)

    async def post_group_message(self, **kw):
        self.sent.append(kw)


class _CtxBridge(QQBotBridge):
    """把发消息路径上的依赖全部换成可控桩（含 ctx 的完整形状）。"""

    def __init__(self, config, uploads):
        super().__init__(config)
        self.api = _Api()
        self.uploads = uploads

    def _ctx_for(self, key):
        return {"api": self.api, "msg_id": "m-1", "loop": None, "recv_at": time.time()}

    def _remaining_replies(self, key):
        return 4

    async def _upload_base64(self, api, user_openid, group_openid, data, file_type=1,
                             srv_send_msg=False):
        self.uploads.append((file_type, len(data)))
        return "file-info"


def _run(bridge, text):
    sent = asyncio.run(bridge.send_local_images("c:me", text))
    return sent, bridge.api.sent


@pytest.fixture(autouse=True)
def _keep_loop():
    yield
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class TestMediaScope:
    def test_project_file_is_sent(self, tmp_path, monkeypatch):
        import agent.qqbot as qb
        monkeypatch.setattr(qb, "_project_root", lambda: str(tmp_path))
        target = tmp_path / "output" / "shot.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 2048)
        uploads = []
        sent, msgs = _run(_CtxBridge(_cfg(), uploads), f"图在这里 {target}")
        assert sent and msgs, "工程内的产物应当照发"
        assert uploads[0][0] == 1 and msgs[0]["msg_type"] == 7   # 图片 + 富媒体消息

    def test_outside_project_is_refused(self, tmp_path, monkeypatch):
        import agent.qqbot as qb
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "Desktop" / "private.png"
        outside.parent.mkdir()
        outside.write_bytes(b"x" * 2048)
        monkeypatch.setattr(qb, "_project_root", lambda: str(project))
        logged = []
        monkeypatch.setattr(run_log, "log", lambda msg, level="info": logged.append(str(msg)))
        uploads = []
        sent, msgs = _run(_CtxBridge(_cfg(), uploads), f"私人截图 {outside}")
        assert sent == [] and msgs == [] and uploads == [], "工程外的文件绝不能发出去"
        assert any("不在工程目录内" in m for m in logged), "要留痕说明为什么跳过"

    def test_oversize_file_is_refused_then_allowed_when_raised(self, tmp_path, monkeypatch):
        import agent.qqbot as qb
        monkeypatch.setattr(qb, "_project_root", lambda: str(tmp_path))
        big = tmp_path / "output" / "huge.png"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"x" * (2 * 1024 ** 2))                  # 2MB
        monkeypatch.setattr(run_log, "log", lambda msg, level="info": None)
        uploads = []
        sent, msgs = _run(_CtxBridge(_cfg(media_max_mb=1), uploads), f"大图 {big}")
        assert sent == [] and msgs == [] and uploads == [], "超过上限不该读进内存上传"
        sent2, msgs2 = _run(_CtxBridge(_cfg(media_max_mb=5), uploads), f"大图 {big}")
        assert sent2 and msgs2, "上限放宽后才允许"

    def test_default_limit_is_ten_mb(self):
        assert _cfg().media_max_bytes == 10 * 1024 ** 2

    def test_small_file_in_project_still_allowed(self, tmp_path, monkeypatch):
        import agent.qqbot as qb
        monkeypatch.setattr(qb, "_project_root", lambda: str(tmp_path))
        f = tmp_path / "screenshots" / "tiny.png"
        f.parent.mkdir()
        f.write_bytes(b"x" * 16)
        uploads = []
        sent, msgs = _run(_CtxBridge(_cfg(), uploads), f"小图 {f}")
        assert sent and msgs

    def test_paths_outside_project_do_not_eat_the_quota(self, tmp_path, monkeypatch):
        """工程外的路径被拒后，工程内的产物仍应发出去（名额不被占）。"""
        import agent.qqbot as qb
        project = tmp_path / "proj"
        (project / "output").mkdir(parents=True)
        inside = project / "output" / "ok.png"
        inside.write_bytes(b"x" * 2048)
        outside = tmp_path / "private.png"
        outside.write_bytes(b"x" * 2048)
        monkeypatch.setattr(qb, "_project_root", lambda: str(project))
        monkeypatch.setattr(run_log, "log", lambda msg, level="info": None)
        uploads = []
        sent, msgs = _run(_CtxBridge(_cfg(), uploads), f"外 {outside} 内 {inside}")
        assert len(msgs) == 1, "只该发出工程内那一个"
        assert os.path.basename(inside) in sent[0] or str(inside) in sent[0]
