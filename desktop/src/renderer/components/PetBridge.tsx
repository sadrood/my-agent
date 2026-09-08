/**
 * 桌宠桥（PetBridge）：把主窗口的状态/事件推给桌宠两窗，并执行面板发来的消息。
 * 渲染层常驻空组件（挂 App）；桌宠收到的推送用 CustomEvent 'pet-push'，
 * 本组件只做 主窗口 → 桌宠 的转发与 桌宠 → sendGoal 的执行。
 */
import React, { useEffect, useMemo, useRef } from 'react';
import { useBackendStore, useSessionStore, useUIStore } from '../store';
import { sendGoal } from '../lib/backend';

interface PushState {
  kind: 'state' | 'user' | 'answer' | 'status' | 'done' | 'error';
  sessionId?: string;
  session?: string;
  running?: boolean;
  theme?: string;
  text?: string;
}

export function PetBridge() {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const running = useBackendStore((s) => s.running);
  const events = useBackendStore((s) => s.events);
  const theme = useUIStore((s) => s.theme);
  const activeTitle = useMemo(
    () => sessions.find((s) => s.id === activeSessionId)?.title || '对话',
    [sessions, activeSessionId],
  );

  const lastLen = useRef(0);
  const lastState = useRef('');

  const push = (p: PushState) => {
    try {
      window.desktopApi?.petPush?.({ ...p, ts: Date.now() });
    } catch { /* 非 Electron 环境忽略 */ }
  };

  // 收到桌宠面板消息 → 真实执行（走与主界面相同的 sendGoal 链路）
  useEffect(() => {
    const api = window.desktopApi;
    if (!api?.onPetChat) return;
    api.onPetChat();
    const onChat = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const text = String((detail && typeof detail === 'object' ? (detail as { text?: string }).text : detail) || '').trim();
      if (!text) return;
      const sid = (detail && typeof detail === 'object' ? (detail as { sessionId?: string }).sessionId : null) || activeSessionId;
      if (!sid) return;
      push({ kind: 'user', sessionId: sid, session: activeTitle, text: text.slice(0, 200) });
      void sendGoal(text, [], sid);
    };
    window.addEventListener('pet-chat', onChat);
    return () => window.removeEventListener('pet-chat', onChat);
  }, [activeSessionId, activeTitle]);

  // 事件流 → 桌宠简报（user 已在上面推送；这里推状态/回答/摘要/错误）
  useEffect(() => {
    const api = window.desktopApi;
    if (!api?.petPush) return;
    const slice = events.slice(lastLen.current);
    lastLen.current = events.length;
    for (const e of slice) {
      const d = (e.data || {}) as Record<string, unknown>;
      if (e.type === 'turn_start') {
        const st = String(d.status_text || d.statusText || '');
        if (st && st !== lastState.current) {
          lastState.current = st;
          push({ kind: 'status', sessionId: activeSessionId ?? undefined, session: activeTitle, running: true, text: st });
        }
      } else if (e.type === 'answer') {
        const out = String(d.output || '').trim();
        if (out) push({ kind: 'answer', sessionId: activeSessionId ?? undefined, session: activeTitle, text: out.slice(0, 600) });
      } else if (e.type === 'metrics') {
        const line = String(d.line || '').trim();
        if (line) push({ kind: 'done', sessionId: activeSessionId ?? undefined, session: activeTitle, running: false, text: line });
      } else if (e.type === 'run_end') {
        const st = String(d.status || '');
        if (st && st !== 'completed') {
          push({ kind: 'done', sessionId: activeSessionId ?? undefined, session: activeTitle, running: false, text: `任务${st === 'failed' ? '失败' : st === 'stopped' ? '已停止' : '结束'}` });
        }
      } else if (e.type === 'error') {
        push({ kind: 'error', sessionId: activeSessionId ?? undefined, session: activeTitle, running: false, text: String(d.message || '出错了').slice(0, 300) });
      }
    }
  }, [events, activeSessionId, activeTitle]);

  // 会话/运行态/主题变化 → 定时推送 state（面板切换会话/显示头部/跟随主题）
  useEffect(() => {
    const t = window.setTimeout(() => {
      push({ kind: 'state', sessionId: activeSessionId ?? undefined, session: activeTitle, running, theme });
    }, 300);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, activeTitle, running, theme, events.length]);

  return null;
}
