/**
 * 任务中心 store（借鉴同类实现：想法+任务状态机）。
 * - tasks：全局任务状态机（todo/in_progress/done/failed + 日志），跨会话。
 * - thoughts：想法速记列表。
 * 操作走后端 /api/tasks、/api/thoughts；WS `tasks`/`thoughts` 事件同步全量列表。
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface TaskItem {
  id: string;
  title: string;
  status: 'todo' | 'in_progress' | 'done' | 'failed';
  created: string;
  updated: string;
  logs: string[];
}

export interface ThoughtItem {
  id: string;
  content: string;
  created: string;
}

export interface ScheduledTask {
  id: string;
  goal: string;
  runtime: string;
  interval_minutes: number;
  last_run: string | null;
  next_run: number | null;
}

const BASE = 'http://127.0.0.1:8090';

interface TasksState {
  tasks: TaskItem[];
  thoughts: ThoughtItem[];
  scheduled: ScheduledTask[];
  setTasks: (tasks: TaskItem[]) => void;
  setThoughts: (thoughts: ThoughtItem[]) => void;
  setTaskStatus: (id: string, status: TaskItem['status']) => Promise<void>;
  addLog: (id: string, content: string) => Promise<void>;
  addThought: (content: string) => Promise<boolean>;
  refresh: () => Promise<void>;
  refreshScheduled: () => Promise<void>;
  addScheduled: (goal: string, interval: number, runtime?: string) => Promise<boolean>;
  removeScheduled: (id: string) => Promise<void>;
  /** 给消息打分（👍👎 + 备注） */
  rateMessage: (messageId: string, rating: 'up' | 'down', note?: string) => Promise<boolean>;
}

export const useTasksStore = create<TasksState>()(
  persist(
    (set, get) => ({
      tasks: [],
      thoughts: [],
      scheduled: [],
      setTasks: (tasks) => set({ tasks }),
      setThoughts: (thoughts) => set({ thoughts }),
      refresh: async () => {
        try {
          const [t, th, sc] = await Promise.all([
            fetch(`${BASE}/api/tasks`).then((r) => r.json()),
            fetch(`${BASE}/api/thoughts`).then((r) => r.json()),
            fetch(`${BASE}/api/scheduled`).then((r) => r.json()),
          ]);
          if (t.ok) set({ tasks: t.tasks });
          if (th.ok) set({ thoughts: th.thoughts });
          if (sc.ok) set({ scheduled: sc.tasks });
        } catch { /* 后端不可达忽略 */ }
      },
      refreshScheduled: async () => {
        try {
          const sc = await fetch(`${BASE}/api/scheduled`).then((r) => r.json());
          if (sc.ok) set({ scheduled: sc.tasks });
        } catch { /* 忽略 */ }
      },
      addScheduled: async (goal, interval, runtime) => {
        if (!goal.trim()) return false;
        try {
          const res = await fetch(`${BASE}/api/scheduled`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal, interval_minutes: interval, runtime: runtime || 'myagent' }),
          });
          const json = await res.json();
          if (json.ok) { useTasksStore.getState().refreshScheduled(); return true; }
          return false;
        } catch { return false; }
      },
      removeScheduled: async (id) => {
        try {
          await fetch(`${BASE}/api/scheduled/remove`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id }),
          });
          useTasksStore.getState().refreshScheduled();
        } catch { /* 忽略 */ }
      },
      rateMessage: async (messageId, rating, note) => {
        if (!messageId) return false;
        try {
          const res = await fetch(`${BASE}/api/feedback`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message_id: messageId, rating, note: note || '' }),
          });
          const json = await res.json();
          return Boolean(json.ok);
        } catch { return false; }
      },
      setTaskStatus: async (id, status) => {
        // 乐观更新 + 后端（后端会发 tasks 事件同步）
        set((s) => ({
          tasks: s.tasks.map((t) => t.id === id ? { ...t, status, updated: new Date().toLocaleString() } : t),
        }));
        try {
          await fetch(`${BASE}/api/tasks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operation: 'status', id, status }),
          });
        } catch { /* 忽略 */ }
      },
      addLog: async (id, content) => {
        if (!content.trim()) return;
        const now = new Date().toLocaleString();
        set((s) => ({
          tasks: s.tasks.map((t) => t.id === id ? { ...t, logs: [...t.logs, `[${now}] ${content}`] } : t),
        }));
        try {
          await fetch(`${BASE}/api/tasks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operation: 'log', id, content }),
          });
        } catch { /* 忽略 */ }
      },
      addThought: async (content) => {
        if (!content.trim()) return false;
        try {
          const res = await fetch(`${BASE}/api/thoughts`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
          });
          const json = await res.json();
          if (json.ok && json.thoughts) set({ thoughts: json.thoughts });
          return Boolean(json.ok);
        } catch {
          return false;
        }
      },
    }),
    {
      name: 'my-agent-tasks',
      partialize: (s) => ({ tasks: s.tasks, thoughts: s.thoughts }) as unknown as TasksState,
    },
  ),
);
