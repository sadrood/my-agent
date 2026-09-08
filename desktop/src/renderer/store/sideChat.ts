/**
 * 辅助 Agent（Side Agent）：按主会话隔离的独立完整 Agent 面板。
 * - 每个主会话各自维护一份辅助对话（chats[mainSessionId]），不再全局共享。
 * - 走后端 /api/side-run 跑完整 Agent（可调工具、事件带 panel=side 由 backend.ts 路由进来）。
 * - 历史持久化到 localStorage（按主会话 id 分键）。
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { ChatMessage } from '../lib/types';

interface SideChatState {
  /** 按主会话 id 分键的消息流 */
  chats: Record<string, ChatMessage[]>;
  /** 按主会话 id 的运行状态 */
  sending: Record<string, boolean>;
  error: Record<string, string>;
  send: (sessionId: string, text: string) => Promise<void>;
  addMessage: (sessionId: string, msg: ChatMessage) => void;
  setSending: (sessionId: string, v: boolean) => void;
  setError: (sessionId: string, e: string) => void;
  clear: (sessionId: string) => void;
}

const FALLBACK_BASE = 'http://127.0.0.1:8090';

export const useSideChatStore = create<SideChatState>()(
  persist(
    (set, get) => ({
      chats: {},
      sending: {},
      error: {},
      send: async (sessionId, text) => {
        const trimmed = text.trim();
        if (!trimmed || !sessionId) return;
        if (get().sending[sessionId]) return;
        const sid = sessionId;
        const userMsg: ChatMessage = {
          id: `u-${Date.now()}`,
          role: 'user',
          content: trimmed,
          kind: 'text',
          timestamp: Date.now(),
        };
        set((s) => ({
          chats: { ...s.chats, [sid]: [...(s.chats[sid] || []), userMsg] },
          sending: { ...s.sending, [sid]: true },
          error: { ...s.error, [sid]: '' },
        }));
        try {
          const res = await fetch(`${FALLBACK_BASE}/api/side-run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal: trimmed, main_session_id: sid }),
          });
          const json = await res.json();
          if (!json.ok) {
            set((s) => ({
              sending: { ...s.sending, [sid]: false },
              error: { ...s.error, [sid]: String(json.error || '提交失败') },
            }));
          }
          // 提交成功：sending 保持 true，等 run_end(panel=side) 事件置 false
        } catch {
          set((s) => ({
            sending: { ...s.sending, [sid]: false },
            error: { ...s.error, [sid]: '后端不可达' },
          }));
        }
      },
      addMessage: (sessionId, msg) =>
        set((s) => ({
          chats: { ...s.chats, [sessionId]: [...(s.chats[sessionId] || []), msg] },
        })),
      setSending: (sessionId, v) =>
        set((s) => ({ sending: { ...s.sending, [sessionId]: v } })),
      setError: (sessionId, e) =>
        set((s) => ({ error: { ...s.error, [sessionId]: e } })),
      clear: (sessionId) =>
        set((s) => ({
          chats: { ...s.chats, [sessionId]: [] },
          error: { ...s.error, [sessionId]: '' },
        })),
    }),
    {
      name: 'my-agent-sidechat',
      partialize: (st) => ({ chats: st.chats }) as unknown as SideChatState,
    },
  ),
);
