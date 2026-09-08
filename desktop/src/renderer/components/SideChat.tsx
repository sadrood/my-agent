/**
 * 辅助 Agent 面板（右侧 Dock 的「辅助」标签）：
 * 独立完整 Agent——按主会话隔离、可调工具、有工具卡片流（复用 ChatFlow 的 Message）。
 * 完成后后端把摘要写入主会话记录，前端在 `side_complete` 时往主对话投递摘要。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Eraser, Send, Square } from 'lucide-react';
import { useSideChatStore } from '../store/sideChat';
import { useSessionStore } from '../store';
import { Message } from './ChatFlow';

export function SideChat() {
  const { chats, sending, error, send, clear, setSending } = useSideChatStore();
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const [text, setText] = useState('');
  const listRef = useRef<HTMLDivElement>(null);

  const sid = activeSessionId || '';
  const messages = (sid && chats[sid]) || [];
  const isRunning = Boolean(sid && sending[sid]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
  }, [messages.length, isRunning, messages.map((m) => m.content.length + (m.reasoning?.length || 0)).join(',')]);

  const submit = () => {
    const t = text.trim();
    if (!t || isRunning) return;
    setText('');
    void send(sid, t);
  };

  const stop = () => {
    void fetch(`http://127.0.0.1:8090/api/side-stop`, { method: 'POST' });
    setSending(sid, false);
  };

  return (
    <div className="side-chat">
      <div className="side-chat-tip">
        辅助 Agent：可调工具、独立会话（按主会话隔离），完成后结论会发到主对话。
        <button className="collapse-btn" title="清空当前会话的辅助对话" onClick={() => sid && clear(sid)}><Eraser size={12} /></button>
      </div>
      <div className="side-chat-list" ref={listRef}>
        {messages.length === 0 && !isRunning && (
          <div className="side-chat-empty">让辅助 Agent 帮你干点事：「帮我调研 xxx 并整理成清单」</div>
        )}
        {messages.map((m) => (
          <Message key={m.id} msg={m} runningHere={isRunning} />
        ))}
        {isRunning && (
          <div className="thinking">
            <span className="thinking-dot" />
            <span>辅助 Agent 思考中…</span>
          </div>
        )}
        {sid && error[sid] && <div className="side-error">⚠️ {error[sid]}</div>}
      </div>
      <div className="side-input">
        <textarea
          value={text}
          placeholder={isRunning ? '辅助 Agent 运行中…（完成后结论会发到主对话）' : '给辅助 Agent 派任务…（Enter 发送 / Shift+Enter 换行）'}
          rows={2}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button
          className={`btn ${isRunning ? 'danger' : 'primary'}`}
          disabled={!isRunning && (!text.trim() || !sid)}
          onClick={isRunning ? stop : submit}
        >
          {isRunning ? <><Square size={13} /> 停止</> : <><Send size={13} /></>}
        </button>
      </div>
    </div>
  );
}
