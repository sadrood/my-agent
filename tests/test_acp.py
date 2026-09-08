"""
ACP 客户端测试：用 fake ACP agent 脚本（模拟 session/update 帧）验证事件桥接。
"""
import json
import os
import sys

from agent.acp import ACPClient, run_acp_session

FAKE_AGENT = r'''
import sys, json

def emit(update):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","method":"session/update",
        "params":{"sessionId":"s1","update":update}}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        frame = json.loads(line)
    except Exception:
        continue
    method = frame.get("method")
    rid = frame.get("id")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"1.2"}}) + "\n")
        sys.stdout.flush()
    elif method == "session/new":
        sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"sessionId":"s1"}}) + "\n")
        sys.stdout.flush()
    elif method == "session/prompt":
        emit({"variant":"agentThoughtChunk","content":{"type":"text","text":"先想想"}})
        emit({"variant":"agentMessageChunk","content":{"type":"text","text":"你好，"}})
        emit({"variant":"agentMessageChunk","content":{"type":"text","text":"世界"}})
        emit({"variant":"toolCall","toolCall":{"toolCallId":"t1","title":"read","input":{"filePath":"a.py"}}})
        emit({"variant":"toolCallUpdate","toolCallUpdate":{"toolCallId":"t1","title":"read","status":"completed","content":{"text":"文件内容"}}})
        emit({"variant":"state_update","stopReason":"endTurn"})
'''


def _fake_argv(tmp_path):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    return [sys.executable, str(script)]


def test_acp_bridges_events(tmp_path):
    events = []
    out = run_acp_session("测试", cwd=str(tmp_path), on_event=lambda t, d: events.append((t, d)),
                          argv=_fake_argv(tmp_path))
    types = [e[0] for e in events]
    assert types[0] == "run_start"
    assert "stream_delta" in types
    # 文本 + 思考增量
    texts = [e[1].get("text") for e in events if e[0] == "stream_delta"]
    assert any("先想想" in t for t in texts)          # reasoning
    assert any("世界" in t for t in texts)            # text
    # 工具调用 + 结果
    assert any(e[0] == "tool_call" and e[1]["tool"] == "read" for e in events)
    assert any(e[0] == "tool_result" and e[1]["success"] for e in events)
    # 收尾
    assert types[-1] == "run_end"
    assert events[-1][1]["status"] == "completed"


def test_acp_client_handshake(tmp_path):
    client = ACPClient(str(tmp_path), lambda t, d: None, argv=_fake_argv(tmp_path))
    try:
        assert client.initialize() is True
        assert client.new_session() is True
        assert client._session_id == "s1"
    finally:
        client.close()
