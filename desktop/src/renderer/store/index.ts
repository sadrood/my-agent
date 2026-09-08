/**
 * 全局状态（Zustand）。
 *
 * 分片：
 * - ui: 主题、面板折叠、当前会话
 * - session: 会话列表与消息
 * - permission: 权限模式与待确认操作
 * - params: 模型参数（自动持久化 localStorage）
 * - backend: 后端连接状态与事件流
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { AgentEvent, ChatMessage, ModelParams, PermissionMode, Session } from '../lib/types';
import { loadConfig } from '../lib/appConfig';
import { useProjectStore } from './project';

/** 左右栏宽度调节范围（px） */
export const PANEL_WIDTH_LIMITS = { left: [200, 520] as const, right: [220, 640] as const };
export const PANEL_DEFAULT_WIDTHS = { left: 262, right: 340 };
/** 中央对话流最小宽度（px）：窗口缩小时左右栏总宽不得超过 窗口宽 - 该值。
    不低于输入区工具栏「图标化+隐藏次要 chip」档位的承载宽度，防止发送键被挤出中央列。 */
export const CENTER_MIN_WIDTH = 520;

function clampWidth(side: 'left' | 'right', w: number): number {
  const [min, max] = PANEL_WIDTH_LIMITS[side];
  const clamped = Math.min(max, Math.max(min, Math.round(w)));
  // 拖拽/设置时也夹紧窗口：该侧宽度不得超过 窗口宽 - 中央最小宽 - 对侧当前宽，
  // 避免把中央对话流挤没（"出主体"）。
  const avail = Math.max(min, window.innerWidth - CENTER_MIN_WIDTH);
  if (side === 'right') {
    return Math.min(clamped, Math.max(min, avail - useUIStore.getState().leftWidth));
  }
  return Math.min(clamped, Math.max(min, avail - useUIStore.getState().rightWidth));
}

/** 按当前窗口宽度重新夹紧左右栏：保证中央对话流不被挤没（窗口 resize 时调用）。 */
export function clampWidthToWindow(): void {
  const s = useUIStore.getState();
  const avail = Math.max(0, window.innerWidth - CENTER_MIN_WIDTH);
  if (s.leftWidth > avail) s.setLeftWidth(avail);
  if (s.rightWidth > avail - useUIStore.getState().leftWidth) {
    s.setRightWidth(Math.max(0, avail - useUIStore.getState().leftWidth));
  }
}

interface UIState {
  agentName: string;
  setAgentName: (name: string) => void;
  theme: 'light' | 'dark';
  rightPanelOpen: boolean;
  leftPanelOpen: boolean;
  /** 右侧 Dock 当前标签：'files'（文件树）/ 'side'（辅助对话）/ 'file:<path>'（打开的文件） */
  rightTab: string;
  /** 左右栏宽度（px，可拖拽调节，持久化） */
  leftWidth: number;
  rightWidth: number;
  /** Agent 经桥请求内嵌浏览器导航（seq 防重放；BrowserPane 监听后跳转） */
  embeddedNav: { seq: number; url: string } | null;
  /** 多标签 Agent：当前打开为 Tab 的会话 id 列表 */
  tabIds: string[];
  /** 桌面宠物浮窗开关（持久化；启动时据此恢复） */
  petVisible: boolean;
  setPetVisible: (v: boolean) => void;
  /** 模型供应商卡片顺序（拖拽排序持久化；未收录的 id 追加在尾） */
  providerOrder: string[];
  setProviderOrder: (order: string[]) => void;
  setEmbeddedNav: (nav: { seq: number; url: string } | null) => void;
  addTab: (id: string) => void;
  removeTab: (id: string) => void;
  setTheme: (t: 'light' | 'dark') => void;
  toggleRightPanel: () => void;
  toggleLeftPanel: () => void;
  setRightTab: (t: string) => void;
  setLeftWidth: (w: number) => void;
  setRightWidth: (w: number) => void;
}

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      theme: 'light',
      // Agent 显示名：界面壳动态跟随（设置里改名字后全局生效；空=默认「小悟」）
      agentName: '',
      setAgentName: (name) => set({ agentName: name }),
      rightPanelOpen: false,       // 默认两栏（左+中）；右栏（工作台）手动打开
      leftPanelOpen: true,
      rightTab: 'overview',
      leftWidth: PANEL_DEFAULT_WIDTHS.left,
      rightWidth: PANEL_DEFAULT_WIDTHS.right,
      embeddedNav: null,
      tabIds: [],
      petVisible: true,
      setPetVisible: (petVisible) => set({ petVisible }),
      providerOrder: [],
      setProviderOrder: (providerOrder) => set({ providerOrder }),
      setEmbeddedNav: (embeddedNav) => set({ embeddedNav }),
      addTab: (id) => set((s) => ({ tabIds: s.tabIds.includes(id) ? s.tabIds : [...s.tabIds, id] })),
      removeTab: (id) => set((s) => ({ tabIds: s.tabIds.filter((x) => x !== id) })),
      setTheme: (theme) => set({ theme }),
      toggleRightPanel: () => set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      toggleLeftPanel: () => set((s) => ({ leftPanelOpen: !s.leftPanelOpen })),
      setRightTab: (rightTab) => set({ rightTab }),
      setLeftWidth: (w) => set({ leftWidth: clampWidth('left', w) }),
      setRightWidth: (w) => set({ rightWidth: clampWidth('right', w) }),
    }),
    { name: 'my-agent-ui',
      version: 6,
      // v4 → v5：默认两栏（右栏工作台改为手动打开）。旧数据里 rightPanelOpen=true
      // 会覆盖新默认，迁移时强制关掉；之后仍尊重手动开关。
      // v5 → v6：右栏重构为「概览」面板，默认 tab 改为 overview（一次性迁移）
      migrate: (persisted: unknown, version: number) => {
        const st = (persisted || {}) as Partial<UIState>;
        if (version < 4) return { ...st, theme: 'light', rightPanelOpen: false, rightTab: 'overview' } as UIState;
        if (version < 5) return { ...st, rightPanelOpen: false, rightTab: 'overview' } as UIState;
        if (version < 6) return { ...st, rightTab: 'overview' } as UIState;
        return st as UIState;
      },
      // embeddedNav 是瞬态导航请求（含 seq），持久化会在重启后重放导致多余 tab
      partialize: (st) => {
        const { embeddedNav, ...rest } = st;
        void embeddedNav;
        return rest as unknown as UIState;
      },
    },
  ),
);

interface SessionState {
  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, ChatMessage[]>;
  selectSession: (id: string) => void;
  createSession: (title?: string, id?: string) => Session;
  /** 把后端会话合并进列表（幂等：已存在则跳过），供启动时后端单一数据源同步 */
  addSessionMeta: (id: string, s: Session) => void;
  /** 更新已有会话的元信息（preview/count/updatedAt 等），不存在则忽略 */
  updateSessionMeta: (id: string, patch: Partial<Session>) => void;
  /** 整批写入某会话的消息（后端 transcript 灌入时用） */
  loadMessages: (sessionId: string, msgs: ChatMessage[]) => void;
  renameSession: (id: string, title: string) => void;
  setSessionRuntime: (id: string, runtime: string) => void;
  togglePin: (id: string) => void;
  deleteSession: (id: string) => void;
  addMessage: (sessionId: string, msg: ChatMessage) => void;
}

/** 会话默认标题（自动命名占位：发第一条消息后会被自动替换） */
export const DEFAULT_TITLE = '新对话';

/** 当前工作目录（多工作区：新建会话时打上归属标签；StatusBar 拉到后注入） */
let CURRENT_WORKSPACE = '';
export function setCurrentWorkspace(dir: string): void {
  CURRENT_WORKSPACE = dir || '';
}
export function getCurrentWorkspace(): string {
  return CURRENT_WORKSPACE;
}

export const useSessionStore = create<SessionState>()(
  persist(
    (set, get) => ({
      sessions: [],
      activeSessionId: null,
      messages: {},
      selectSession: (id) => set({ activeSessionId: id }),
      createSession: (title, id) => {
        // 优先用后端生成的 id（conv-YYYYMMDD-hex，与 cmd 会话命名空间一致）；
        // 未传时本地兜底（离线场景）。
        const sid = id || `conv-${Date.now().toString(36)}`;
        const now = new Date().toISOString();
        const s: Session = {
          id: sid,
          title: title || DEFAULT_TITLE,
          createdAt: now,
          updatedAt: now,
          pinned: false,
          model: loadConfig()?.model || undefined,
          runtime: 'myagent',
          messageCount: 0,
          projectId: useProjectStore.getState().activeProjectId || undefined,
          workspace: CURRENT_WORKSPACE || undefined,
        };
        set((st) => ({
          sessions: [s, ...st.sessions],
          activeSessionId: sid,
          messages: { ...st.messages, [sid]: [] },
        }));
        return s;
      },
      addSessionMeta: (id, s) =>
        set((st) => {
          if (st.sessions.some((x) => x.id === id)) return st;
          return { sessions: [s, ...st.sessions] };
        }),
      // 更新已有会话的元信息（preview/count/updatedAt 等），不存在则忽略
      updateSessionMeta: (id, patch) =>
        set((st) => ({
          sessions: st.sessions.map((s) =>
            s.id === id ? { ...s, ...patch } : s,
          ),
        })),
      loadMessages: (sessionId, msgs) =>
        set((st) => ({
          messages: { ...st.messages, [sessionId]: msgs },
          // 后端 transcript 灌入后同步会话的消息计数，避免侧栏计数停在 0/旧值
          sessions: st.sessions.map((s) =>
            s.id === sessionId
              ? { ...s, messageCount: msgs.length, updatedAt: new Date().toISOString() }
              : s,
          ),
        })),
      renameSession: (id, title) =>
        set((st) => ({
          sessions: st.sessions.map((s) => (s.id === id ? { ...s, title } : s)),
        })),
      setSessionRuntime: (id, runtime) =>
        set((st) => ({
          sessions: st.sessions.map((s) => (s.id === id ? { ...s, runtime } : s)),
        })),
      togglePin: (id) =>
        set((st) => {
          const sessions = st.sessions.map((s) =>
            s.id === id ? { ...s, pinned: !s.pinned } : s,
          );
          // 置顶会话排前
          sessions.sort((a, b) => Number(b.pinned) - Number(a.pinned));
          return { sessions };
        }),
      deleteSession: (id) =>
        set((st) => {
          const messages = { ...st.messages };
          delete messages[id];
          const sessions = st.sessions.filter((s) => s.id !== id);
          let activeSessionId = st.activeSessionId;
          if (activeSessionId === id) {
            // 删除当前会话 → 跳到相邻会话（主流桌面端行为），不置 null
            const idx = st.sessions.findIndex((s) => s.id === id);
            activeSessionId = sessions[idx]?.id ?? sessions[idx - 1]?.id ?? null;
          }
          return { sessions, messages, activeSessionId };
        }),
      addMessage: (sessionId, msg) =>
        set((st) => {
          const list = st.messages[sessionId] || [];
          return {
            messages: { ...st.messages, [sessionId]: [...list, msg] },
            sessions: st.sessions.map((s) =>
              s.id === sessionId
                ? {
                    ...s,
                    // 自动命名：会话还是默认标题时，用第一条用户消息（首行前 30 字）当标题
                    title:
                      s.title === DEFAULT_TITLE && msg.role === 'user' && msg.content.trim()
                        ? msg.content.trim().split(/\r?\n/)[0].slice(0, 30)
                        : s.title,
                    updatedAt: new Date().toISOString(),
                    messageCount: list.length + 1,
                  }
                : s,
            ),
          };
        }),
    }),
    {
      name: 'my-agent-sessions',
      // 消息历史不进 persist：流式期间每 100ms 一次 setState，若全量序列化写
      // localStorage 会随历史增长放大成主线程卡顿。消息改由下方节流落盘单独保存。
      partialize: (st) =>
        ({ sessions: st.sessions, activeSessionId: st.activeSessionId }) as unknown as SessionState,
    },
  ),
);

// ---- 消息历史的节流持久化 ----
const MESSAGES_KEY = 'my-agent-messages';

function saveMessagesNow(): void {
  try {
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(useSessionStore.getState().messages));
  } catch { /* 配额满等场景静默跳过 */ }
}

let messagesSaveTimer: ReturnType<typeof setTimeout> | null = null;
useSessionStore.subscribe((st, prev) => {
  if (st.messages === prev.messages) return;
  if (document.hidden) { saveMessagesNow(); return; }
  if (messagesSaveTimer) return;
  messagesSaveTimer = setTimeout(() => { messagesSaveTimer = null; saveMessagesNow(); }, 2000);
});

window.addEventListener('pagehide', () => {
  if (messagesSaveTimer) { clearTimeout(messagesSaveTimer); messagesSaveTimer = null; }
  saveMessagesNow();
});

// 主题即时级联到 <html>：任何入口（顶栏按钮 / 设置弹窗）切换主题时，
// body/html 背景与滚动条立刻跟随，无需刷新（启动时的初始同步在 main.tsx）
useUIStore.subscribe((st) => {
  document.documentElement.dataset.theme = st.theme;
});

// 启动时恢复消息历史；旧版把消息存在 my-agent-sessions 里，做一次性迁移
(function hydrateMessages() {
  let restored: Record<string, ChatMessage[]> | null = null;
  try {
    const raw = localStorage.getItem(MESSAGES_KEY);
    if (raw) restored = JSON.parse(raw);
  } catch { /* ignore */ }
  if (!restored) {
    try {
      const legacy = localStorage.getItem('my-agent-sessions');
      if (legacy) {
        const parsed = JSON.parse(legacy) as { state?: { messages?: Record<string, ChatMessage[]> } };
        if (parsed.state?.messages && Object.keys(parsed.state.messages).length > 0) {
          restored = parsed.state.messages;
          try { localStorage.setItem(MESSAGES_KEY, JSON.stringify(restored)); } catch { /* ignore */ }
        }
      }
    } catch { /* ignore */ }
  }
  if (restored && Object.keys(restored).length > 0) {
    useSessionStore.setState({ messages: { ...useSessionStore.getState().messages, ...restored } });
  }
})();

interface PermissionState {
  mode: PermissionMode;
  pending: { id: string; tool: string; command: string; risk: string } | null;
  setMode: (m: PermissionMode) => void;
  requestApproval: (req: { tool: string; command: string; risk: string }) => string;
  resolveApproval: (id: string, allow: boolean) => void;
}

export const usePermissionStore = create<PermissionState>()(
  persist(
    (set) => ({
      mode: 'ask',
      pending: null,
      setMode: (mode) => set({ mode }),
      requestApproval: (req) => {
        const id = `ap-${Date.now().toString(36)}`;
        set({ pending: { id, ...req } });
        return id;
      },
      resolveApproval: (id, allow) =>
        set((st) => (st.pending && st.pending.id === id ? { pending: null } : st)),
    }),
    { name: 'my-agent-permission' },
  ),
);

interface ParamsState {
  params: ModelParams;
  setParam: (k: keyof ModelParams, v: number) => void;
}

export const useParamsStore = create<ParamsState>()(
  persist(
    (set) => ({
      params: { temperature: 0.7, topP: 1.0, maxTokens: 8192, maxOps: 0 },
      setParam: (k, v) => set((s) => ({ params: { ...s.params, [k]: v } })),
    }),
    {
      name: 'my-agent-params',
      version: 3,
      // v1 曾为慢思考模型把上限压到 2048——思考长的任务会在"只思考未回答"处被截断
      // （finish_reason=length、正文为空），体验与 CLI（8192）不一致 → v2 恢复 8192。
      // v3：新增 maxOps（0=后端默认），旧存档补 0。
      migrate: (persisted: any) => {
        const p = (persisted && persisted.params) || {};
        return {
          params: {
            temperature: typeof p.temperature === 'number' ? p.temperature : 0.7,
            topP: typeof p.topP === 'number' ? p.topP : 1.0,
            maxTokens: typeof p.maxTokens === 'number' ? p.maxTokens : 8192,
            maxOps: typeof p.maxOps === 'number' ? p.maxOps : 0,
          },
        };
      },
    },
  ),
);

/** 队列里的一条待发任务（绑定发起会话，防止出队后落到当前选中会话） */
export interface QueueGoal {
  text: string;
  attachments: { name: string; content: string }[];
  sessionId: string;
}

interface BackendState {
  connected: boolean;
  events: AgentEvent[];
  running: boolean;
  /** 当前运行任务归属的会话 id（多会话隔离：只在发起任务的会话里显示"运行中"） */
  runningSessionId: string | null;
  /** 最近一次 turn_start 状态条数据（轮次/总轮/聚合文本），新任务开始时清空 */
  turnStart: { turn: number; maxOps: number; statusText: string } | null;
  /** 最近一次 skills_matched 匹配提示（技能名列表），新任务开始时清空 */
  skillsMatched: { skills: string[]; goal: string } | null;
  /** 全局运行统计（发送框下方展示，metrics 事件更新；新任务开始清空） */
  globalStats: { line: string; updatedAt: number } | null;
  queue: QueueGoal[];
  setConnected: (c: boolean) => void;
  pushEvent: (e: AgentEvent) => void;
  /** 设置运行状态；sessionId 传入则记录任务归属会话（结束时置 null） */
  setRunning: (r: boolean, sessionId?: string | null) => void;
  setTurnStart: (t: { turn: number; maxOps: number; statusText: string } | null) => void;
  setSkillsMatched: (s: { skills: string[]; goal: string } | null) => void;
  setGlobalStats: (line: string) => void;
  clearEvents: () => void;
  enqueueGoal: (g: QueueGoal) => void;
  dequeueGoal: () => QueueGoal | undefined;
  removeQueuedAt: (idx: number) => void;
}

export const useBackendStore = create<BackendState>()(
  persist(
    (set, get) => ({
      connected: false,
      events: [],
      running: false,
      runningSessionId: null,
      turnStart: null,
      skillsMatched: null,
      globalStats: null,
      queue: [],
      setConnected: (connected) => set({ connected }),
      pushEvent: (e) => set((s) => ({ events: [...s.events, e].slice(-500) })),
      setRunning: (running, sessionId) =>
        set((st) => ({
          running,
          runningSessionId: running
            ? (sessionId !== undefined ? sessionId : st.runningSessionId)
            : null,
        })),
      setTurnStart: (turnStart) => set({ turnStart }),
      setSkillsMatched: (skillsMatched) => set({ skillsMatched }),
      setGlobalStats: (line) => set({ globalStats: line ? { line, updatedAt: Date.now() } : null }),
      clearEvents: () => set({ events: [] }),
      enqueueGoal: (g) => set((s) => ({ queue: [...s.queue, g] })),
      dequeueGoal: () => {
        const q = get().queue;
        if (!q.length) return undefined;
        set({ queue: q.slice(1) });
        return q[0];
      },
      removeQueuedAt: (idx) => set((s) => ({ queue: s.queue.filter((_, i) => i !== idx) })),
    }),
    {
      name: 'my-agent-backend',
      // 只持久化队列：应用重启后排队任务不丢（重连且空闲时自动续发）
      partialize: (st) => ({ queue: st.queue }) as unknown as BackendState,
    },
  ),
);
