/**
 * 后端集成：连接 FastAPI 后端（/ws 事件流 + /api/run 提交任务）。
 *
 * 事件流模型对齐 dashboard/hub.py + agent.py 真实事件：
 * - run_start → model_turn → tool_call(args) / tool_result → stream_delta(流式文本) →
 *   answer(完整回答) → run_end
 * 事件映射为对话消息（text / diff / command / steps / approval），
 * 流式文本 stream_delta 实时追加到当前回答。
 */
import type { AgentEvent, ChatMessage, Session } from './types';
import { useBackendStore, useSessionStore, useParamsStore, usePermissionStore, useUIStore } from '../store';
import { useSideChatStore } from '../store/sideChat';
import { useTasksStore } from '../store/tasks';
import { loadConfig, displayAgentName } from './appConfig';

/** preload 通过 contextBridge 暴露的桌面 API（仅 Electron 环境存在） */
declare global {
  interface Window {
    desktopApi?: {
      backendStatus: () => Promise<{ running: boolean }>;
      backendRestart: (workDir?: string) => Promise<{ ok: boolean }>;
      getBackendBase: () => Promise<string>;
      /** 应用版本号（package.json） */
      getVersion?: () => Promise<string>;
      setShellTitle?: (name: string) => Promise<boolean>;
      getWorkdir?: () => Promise<string>;
      pickDirectory?: () => Promise<{ ok: boolean; path?: string }>;
      setTitleBarOverlay?: (theme: 'light' | 'dark') => Promise<void>;
      petOpenPanel?: () => Promise<{ ok: boolean }>;
      petSendMessage?: (text: string) => Promise<{ ok: boolean }>;
      onPetPush?: () => void;
      onPetChat?: () => void;
      petPush?: (payload: unknown) => void;
      notifyDone?: (status?: string) => Promise<void>;
      secureGet?: (name: string) => Promise<{ ok: boolean; value?: string; error?: string }>;
      secureSet?: (name: string, value: string) => Promise<{ ok: boolean; error?: string }>;
      copyText?: (text: string) => Promise<{ ok: boolean }>;
      /** 内嵌浏览器桥：注册 IPC → window CustomEvent 转发（仅 Electron 主窗口存在） */
      onEmbeddedBrowserOpen?: () => void;
      onEmbeddedBrowserClose?: () => void;
      onEmbeddedBrowserSwitch?: () => void;
      onEmbeddedBrowserCloseTab?: () => void;
      /** 多标签：登记 tabId↔webContentsId / 激活 */
      registerWebview?: (tabId: string, wcId: number) => Promise<void>;
      unregisterWebview?: (tabId: string) => Promise<void>;
      setActiveWebview?: (tabId: string) => Promise<void>;
      /** 桌面宠物：显示/隐藏（缺省=切换）、推送运行状态、聚焦主窗、订阅状态与菜单动作 */
      petToggle?: (show?: boolean) => Promise<{ visible: boolean }>;
      petStatus?: (running: boolean, endStatus?: string) => Promise<void>;
      focusMain?: () => Promise<void>;
      onPetStatus?: () => void;
      onPetAction?: () => void;
      sendPetAction?: (action: string) => Promise<void>;
      moveWindow?: (dx: number, dy: number) => Promise<void>;
      /** 内置终端（桥接回显式）：历史快照 / 用户执行 / 回显订阅 */
      terminalSnapshot?: () => Promise<unknown[]>;
      terminalUserRun?: (command: string) => Promise<{ ok: boolean; output?: string; error?: string }>;
      onTerminalEcho?: (cb: (entry: unknown) => void) => void;
      offTerminalEcho?: () => void;
      onTerminalOpen?: () => void;
    };
  }
}

/** 后端默认地址（兜底：非 Electron 环境或 preload 未注入时使用） */
const DEFAULT_BASE = 'http://127.0.0.1:8090';

/** 后端基地址缓存：优先走主进程 IPC 获取，失败回退默认值 */
let backendBase: string | null = null;

/** 获取后端 API 基地址（不带末尾斜杠） */
async function getApiBase(): Promise<string> {
  if (backendBase) return backendBase;
  try {
    const base = await window.desktopApi?.getBackendBase();
    backendBase = (base && base.length > 0 ? base : DEFAULT_BASE).replace(/\/+$/, '');
  } catch {
    backendBase = DEFAULT_BASE;
  }
  return backendBase;
}

/** 由 HTTP 基地址推导 WebSocket 地址 */
function getWsUrl(base: string): string {
  return base.replace(/^http/i, 'ws') + '/ws';
}

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
/** 当前正在流式构建的回答消息 id（用于 stream_delta 追加） */
let streamingMessageId: string | null = null;

/** 当前运行任务归属的会话 id（提交任务时锁定）。
 * 关键：任务跑的过程中用户可能切到别的会话，若事件按"当前选中的会话"落盘，
 * A 会话的提问/回答/工具卡片就会出现在 B 会话里（串会话）。单任务语义下
 * 锁定提交时的会话，run_end / 断连时清空。 */
let runSessionId: string | null = null;

/** 事件落盘用哪个会话：运行中的任务 → 锁定的归属会话；空闲事件 → 当前选中会话 */
function eventSessionId(): string | null {
  return runSessionId || useSessionStore.getState().activeSessionId;
}

// 仅测试用：读写当前任务归属会话（验证"任务进行中切会话不串"的回归保护）
export function __setRunSessionId(id: string | null): void {
  runSessionId = id;
}
export function __eventSessionId(): string | null {
  return eventSessionId();
}

/** 用户选过「总是允许」的工具：本次应用运行期间对该工具的审批自动放行（不持久化，重启后重置更安全） */
const alwaysAllowedTools = new Set<string>();

/** 记住「总是允许此工具」并立即放行当前请求 */
export function alwaysAllowTool(tool: string, id?: string): void {
  if (tool) alwaysAllowedTools.add(tool);
  void sendApprovalResponse(id, true);
}

/** 流式增量节流缓冲：合并 100ms 内的 delta，避免每个增量都触发整条消息重渲染（长思考/长回答会卡） */
let streamBuffer: { kind: 'reasoning' | 'text'; text: string }[] = [];
let streamFlushTimer: ReturnType<typeof setTimeout> | null = null;

/** 把后端事件转成对话消息（供 ChatFlow 渲染） */
export function eventToMessage(e: AgentEvent): ChatMessage | null {
  const d = e.data as Record<string, any>;
  const id = `${e.type}-${e.timestamp}-${Math.random().toString(36).slice(2, 6)}`;
  switch (e.type) {
    case 'answer':
      // 完整回答：若已有流式消息则不再重复（由 onmessage 层处理），此处兜底
      return { id, role: 'assistant', content: String(d.output || ''), kind: 'text', timestamp: e.timestamp };
    case 'run_end':
      // 成功收尾不额外插入「✅ 任务完成」系统消息（回答本身即是结果）；
      // 失败/停止仍需可见提示与重试入口
      if (d.status === 'completed') return null;
      return {
        id, role: 'system', content: statusText(d), kind: 'text',
        meta: { runStatus: d.status, goal: d.goal, retryable: d.status === 'failed' && Boolean(d.goal) },
        timestamp: e.timestamp,
      };
    case 'error':
      return { id, role: 'system', content: `⚠️ ${String(d.message || '')}`, kind: 'text', timestamp: e.timestamp };
    case 'plan':
      return { id, role: 'system', content: '任务步骤', kind: 'steps', meta: { steps: d.steps || [] }, timestamp: e.timestamp };
    case 'tool_call': {
      // 真实事件字段是 args（demo 用 input）；两者都兼容。
      // content 留空：命令串放 meta.cmd，输出位等 tool_result 回填（避免未完成卡片把命令重复显示成输出）
      const args = d.args ?? d.arguments ?? d.input ?? {};
      const tool = String(d.tool || '');
      const cmd = formatToolCall(tool, args);
      const path = String(args?.file_path ?? args?.path ?? '') || undefined;
      return { id, role: 'system', content: '', kind: 'command', meta: { cmd, tool, path, success: undefined as boolean | undefined }, timestamp: e.timestamp };
    }
    case 'tool_result': {
      // 结果事件同样带 arguments：补进 meta.cmd，避免"调用参数在 call 里丢、result 又没接"时
      // 工具行只剩 "tool 失败 xx s" 干巴巴一行（命令摘要 + 失败原因才是 CLI 同款信息量）
      const args = d.args ?? d.arguments ?? d.input ?? {};
      const tool = String(d.tool || '');
      return {
        id, role: 'tool', content: String(d.output || d.error || ''),
        kind: 'command',
        meta: { tool, success: Boolean(d.success), cmd: formatToolCall(tool, args), args },
        timestamp: e.timestamp,
      };
    }
    case 'approval':
      return { id, role: 'system', content: String(d.command || d.tool || ''), kind: 'approval', meta: { id: d.id, tool: d.tool, command: d.command, risk: d.risk_level || d.risk || 'medium', reason: d.reason }, timestamp: e.timestamp };
    case 'step_start':
      // 步骤进度由「任务步骤」卡片 + 工具行承载，聊天流里不再漂居中的步骤文字
      return null;
    default:
      return null; // model_turn/stream_delta/compaction/checkpoint 等由专门逻辑处理
  }
}

/** run_end 状态文案（导出供测试） */
export function statusText(d: Record<string, any>): string {
  if (d.status === 'stopped') return '⏹ 已停止';
  if (d.status === 'completed') return '✅ 任务完成';
  if (d.status === 'failed') return `❌ ${String(d.error || d.summary || '任务失败').slice(0, 200)}`;
  return '任务结束';
}

/** 辅助 Agent 事件路由：按 side_of（主会话 id）把 panel=side 的事件灌入对应辅助面板。
 *  离散事件（tool_call/tool_result/answer/approval/run_end/error）复用 eventToMessage；
 *  流式 delta 简单追加到进行中的 assistant 消息（不污染主对话）。 */
function routeSideEvent(msg: { type: string; data: Record<string, unknown>; timestamp: number }): void {
  const d = (msg.data || {}) as Record<string, any>;
  const mainSessionId = String(d?.side_of || d?.main_session_id || '').trim();
  if (!mainSessionId) return;
  const sc = useSideChatStore.getState();

  // 侧任务完成：摘要投递到主会话（主 Agent 可见，且已写回后端会话历史）
  if (msg.type === 'side_complete') {
    const summary = String(d?.summary || '').trim();
    const goal = String(d?.goal || '').trim();
    if (summary) {
      useSessionStore.getState().addMessage(mainSessionId, {
        id: `side-done-${Date.now()}`,
        role: 'system',
        kind: 'text',
        content: `🧩 辅助Agent 完成「${goal}」：${summary.slice(0, 500)}`,
        timestamp: Date.now(),
      });
    }
    return;
  }

  // 流式增量：追加到该主会话辅助面板最后一条 assistant 消息（或新建一条）
  if (msg.type === 'stream_delta') {
    const text = String(d?.text || '');
    if (!text) return;
    const kind = d?.kind === 'reasoning' ? 'reasoning' : 'text';
    const list = sc.chats[mainSessionId] || [];
    const last = list[list.length - 1];
    if (last && last.role === 'assistant') {
      const idx = list.length - 1;
      useSideChatStore.setState((st) => ({
        chats: {
          ...st.chats,
          [mainSessionId]: (st.chats[mainSessionId] || []).map((m, i) =>
            i === idx
              ? {
                  ...m,
                  reasoning: kind === 'reasoning' ? (m.reasoning || '') + text : m.reasoning,
                  content: kind === 'text' ? m.content + text : m.content,
                }
              : m,
          ),
        },
      }));
    } else {
      sc.addMessage(mainSessionId, {
        id: `side-a-${Date.now()}`,
        role: 'assistant',
        content: kind === 'text' ? text : '',
        reasoning: kind === 'reasoning' ? text : '',
        kind: 'text',
        timestamp: Date.now(),
      });
    }
    return;
  }

  if (msg.type === 'run_end') {
    sc.setSending(mainSessionId, false);
  }
  const chatMsg = eventToMessage({ type: msg.type as AgentEvent['type'], data: d, timestamp: msg.timestamp });
  if (chatMsg) sc.addMessage(mainSessionId, chatMsg);
}

/** turn_start 事件 → 状态条展示数据；无效载荷返回 null（导出供测试） */
export function turnStartView(d: Record<string, any> | null | undefined): { turn: number; maxOps: number; statusText: string } | null {
  if (!d || typeof d !== 'object') return null;
  const turn = Number(d.turn);
  if (!Number.isFinite(turn) || turn < 1) return null;
  return {
    turn,
    maxOps: Number(d.max_ops) > 0 ? Number(d.max_ops) : 0,
    statusText: String(d.status_text || ''),
  };
}

/** skills_matched 事件 → 匹配技能提示；无技能或载荷非法返回 null（导出供测试） */
export function skillsMatchedView(d: Record<string, any> | null | undefined): { skills: string[]; goal: string } | null {
  if (!d || typeof d !== 'object') return null;
  const skills = Array.isArray(d.skills)
    ? d.skills.filter((s) => s != null).map((s) => String(s)).filter(Boolean)
    : [];
  if (!skills.length) return null;
  return { skills, goal: String(d.goal || '') };
}

/** 工具输出增量：追加到最近一条同名、执行中的工具行（保留末尾 4000 字防止超长刷屏） */
function appendToolOutput(d: { tool?: string; text?: string }): void {
  const text = String(d.text || "");
  if (!text) return;
  const { messages } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId) return;
  const list = messages[activeSessionId] || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    const meta = (m.meta || {}) as { tool?: string; success?: boolean };
    if (m.kind === 'command' && meta.success !== undefined) break; // 只找最近的执行中行
    if (m.kind === 'command' && meta.success === undefined && String(d.tool ?? meta.tool) === String(meta.tool)) {
      useSessionStore.setState((st) => ({
        messages: {
          ...st.messages,
          [activeSessionId]: st.messages[activeSessionId].map((x, j) => {
            if (j !== i) return x;
            const next = x.content + text;
            return { ...x, content: next.length > 4000 ? next.slice(next.length - 4000) : next };
          }),
        },
      }));
      return;
    }
  }
}

/** 把 tool_result 回填到当前会话最近一条同名、未完成的 tool_call 行上；配对成功返回 true */
function resolvePendingToolCall(d: Record<string, any>): boolean {
  const { messages } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId) return false;
  const list = messages[activeSessionId] || [];
  const now = Date.now();
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    const meta = (m.meta || {}) as { tool?: string; success?: boolean | undefined };
    if (m.kind === 'command' && meta.success === undefined && String(d.tool ?? meta.tool) === String(meta.tool)) {
      useSessionStore.setState((st) => ({
        messages: {
          ...st.messages,
          [activeSessionId]: st.messages[activeSessionId].map((x, j) =>
            j === i
              ? { ...x, role: 'tool' as const, content: String(d.output || d.error || ''), meta: { ...(x.meta || {}), success: Boolean(d.success), durationMs: now - x.timestamp } }
              : x,
          ),
        },
      }));
      return true;
    }
  }
  return false;
}

/** 任务结束时把仍未回填的工具调用卡片标记为失败（停止/中断场景，避免卡片永远"执行中"） */
function failPendingToolCalls(status: unknown): void {
  const { messages } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId) return;
  const list = messages[activeSessionId] || [];
  const isPending = (m: ChatMessage) =>
    m.kind === 'command' && ((m.meta || {}) as { success?: boolean }).success === undefined;
  if (!list.some(isPending)) return;
  const note = status === 'stopped' ? '（任务已停止，未收到结果）' : '（任务结束，未收到结果）';
  useSessionStore.setState((st) => ({
    messages: {
      ...st.messages,
      [activeSessionId]: st.messages[activeSessionId].map((m) =>
        isPending(m) ? { ...m, content: note, meta: { ...(m.meta || {}), success: false } } : m,
      ),
    },
  }));
}

/** 把工具调用参数格式化为可读命令串（导出供测试） */
export function formatToolCall(tool: string, args: any): string {
  if (typeof args === 'string') return args; // demo 事件把 input 字符串直接当 args 传入
  if (tool === 'terminal' || tool === 'python') {
    return String(args.command ?? args.code ?? args.cmd ?? JSON.stringify(args));
  }
  if (tool === 'file') {
    const op = String(args.operation ?? '');
    return `${op} ${String(args.path ?? '')}`.trim();
  }
  if (tool === 'edit') {
    // 行首已显示工具名，这里只给目标路径，避免 "edit edit" 式重复
    return String(args.file_path ?? '');
  }
  if (tool === 'browser') {
    return `${String(args.command ?? '')} ${String(args.url ?? args.args ?? '')}`.trim();
  }
  if (tool === 'think') return ''; // 思考卡不显示命令行，只展示思考摘要输出
  return `${tool} ${JSON.stringify(args).slice(0, 120)}`;
}

/** 建立 WebSocket 连接（带自动重连与状态同步） */
let connecting = false;
export async function connectBackend(): Promise<void> {
  // 同步锁：StrictMode 会把 effect 跑两遍，async 在 await 前不加锁会建出两个 WebSocket → 事件/消息重复
  if (connecting) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  connecting = true;
  try {
    const base = await getApiBase();
    ws = new WebSocket(getWsUrl(base));
    ws.onopen = () => {
    useBackendStore.getState().setConnected(true);
    ws?.send(JSON.stringify({ type: 'get_state' }));
    // 后端重启会丢运行时设置（视觉模型覆盖），连上后自动重新下发
    void syncRuntimeConfig();
    // 恢复运行态：若后端仍有活动任务（刷新/重连场景），把 running 置回，
    // 让停止按钮/停止入口可用，避免"任务在跑却停不了"
    void fetchRunning().then((r) => {
      if (r.running) {
        const active = useSessionStore.getState().activeSessionId;
        const sid = r.sessions.includes(active || '') ? active : (r.sessions[0] || null);
        useBackendStore.getState().setRunning(true, sid ?? undefined);
      }
    });
    // 恢复持久化的任务队列：后端空闲时自动续发排队任务
    setTimeout(() => { void drainQueue(); }, 1200);
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      // 辅助 Agent 事件（panel=side）：路由到对应主会话的辅助面板，不污染主对话
      if ((msg.data || {}).panel === 'side') {
        routeSideEvent(msg);
        return;
      }
      const store = useBackendStore.getState();
      if (msg.type === 'state_snapshot') {
        store.setRunning(Boolean((msg.data || {}).running));
        // 队列兜底：后端空闲且有排队任务（如上次后端崩溃没等到 run_end）→ 续发
        if (!(msg.data || {}).running) setTimeout(() => { void drainQueue(); }, 800);
        return;
      }
      if (msg.type === 'run_start') {
        // 记录任务归属会话：多会话时只在发起任务的会话里显示"运行中"，
        // 且事件只写入该会话（runSessionId 未锁定说明任务来自其他客户端/demo）
        runSessionId = runSessionId || useSessionStore.getState().activeSessionId;
        store.setRunning(true, runSessionId);
        streamingMessageId = null; clearStreamBuffer();
        // 新任务开始：清空上轮的状态条/技能提示/全局统计
        store.setTurnStart(null);
        store.setSkillsMatched(null);
        store.setGlobalStats('');
        // 运行状态外显：窗口标题 + 完成时任务栏闪烁
        document.title = `▶ 运行中 · ${displayAgentName(loadConfig().agentName)} Desktop`;
      }
      if (msg.type === 'run_end') {
        flushStreamBuffer(); store.setRunning(false); streamingMessageId = null;
        // 任务收尾：清空状态条与技能匹配提示
        store.setTurnStart(null);
        store.setSkillsMatched(null);
        // 任务收尾：把仍未回填的工具调用卡片标记为失败（停止/中断场景，避免卡片永远"执行中"）
        failPendingToolCalls((msg.data || {}).status);
        document.title = `${displayAgentName(loadConfig().agentName)} Desktop`;
        runSessionId = null;   // 任务结束：解除会话锁定
        void window.desktopApi?.notifyDone?.(String((msg.data || {}).status || ''));
        // 任务队列：上一轮结束 → 自动下发下一条排队任务（稍等 UI 落定）
        setTimeout(() => { void drainQueue(); }, 600);
      }
      store.pushEvent({ type: msg.type, data: msg.data || {}, timestamp: Date.now() });

      // tasks/thoughts 事件（任务中心）：同步全量列表到 tasks store
      if (msg.type === 'tasks') {
        const tasks = Array.isArray((msg.data || {}).tasks) ? (msg.data as any).tasks : [];
        useTasksStore.getState().setTasks(tasks);
        return;
      }
      if (msg.type === 'thoughts') {
        const thoughts = Array.isArray((msg.data || {}).thoughts) ? (msg.data as any).thoughts : [];
        useTasksStore.getState().setThoughts(thoughts);
        return;
      }

      // metrics 事件（任务收尾后由后端发出）：更新发送框下方的全局统计条（对齐 cmd 的 render_line）
      if (msg.type === 'metrics') {
        const line = String((msg.data || {}).line || '').trim();
        useBackendStore.getState().setGlobalStats(line);
        return;
      }

      // todo 事件（模型用 todo_write 更新清单）：在会话里 upsert 一张待办卡
      if (msg.type === 'todo') {
        const todos = Array.isArray((msg.data || {}).todos) ? (msg.data as any).todos : [];
        const sid = eventSessionId();
        if (!sid) return;
        const { messages } = useSessionStore.getState();
        const list = messages[sid] || [];
        const idx = list.findIndex((m) => m.kind === 'todo');
        if (idx >= 0) {
          useSessionStore.setState((st) => ({
            messages: {
              ...st.messages,
              [sid]: (st.messages[sid] || []).map((m, i) =>
                i === idx ? { ...m, meta: { todos }, timestamp: Date.now() } : m,
              ),
            },
          }));
        } else {
          useSessionStore.getState().addMessage(sid, {
            id: `todo-${Date.now()}`,
            role: 'system',
            kind: 'todo',
            content: '',
            meta: { todos },
            timestamp: Date.now(),
          });
        }
        return;
      }

      // turn_start 状态条：轮次 + token/沙箱/策略聚合文本 → 顶部常驻状态条
      if (msg.type === 'turn_start') {
        const v = turnStartView(msg.data || {});
        if (v) store.setTurnStart(v);
        // 关键：每轮模型调用前把当前流式气泡收尾（flush + 复位）。
        // 否则整个任务的思考/正文全部堆进第一条消息，
        // 视觉上就是"上面一直思考、下面一直堆工具"（与按回合交错的常见做法相悖）
        flushStreamBuffer();
        streamingMessageId = null;
        return;
      }

      // skills_matched 匹配提示：命中的技能名列表 → 顶部状态条提示（仅命中时由后端发出）
      if (msg.type === 'skills_matched') {
        const v = skillsMatchedView(msg.data || {});
        if (v) store.setSkillsMatched(v);
        return;
      }

      // checkpoint 事件：把 commit hash 挂到对应的工具行上（供「回滚到此处」按钮）
      if (msg.type === 'checkpoint') {
        attachCommitToToolRow((msg.data || {}) as { tool?: string; commit?: string });
        return;
      }

      // 工具输出增量：实时追加到对应 pending 工具行的输出区（长命令边跑边看）
      if (msg.type === 'tool_output') {
        appendToolOutput(msg.data || {});
        return;
      }

      // 工具结果回填：与最近一条同名、未完成的 tool_call 卡片配对（单卡片状态流转）；
      // 找不到配对（如只收到孤立结果）才回退为独立消息
      if (msg.type === 'tool_result' && resolvePendingToolCall(msg.data || {})) return;

      // 流式文本：reasoning → 可折叠思考块；text → 正文。节流合并后统一写入状态，
      // 避免每个增量都触发 React 整条消息重渲染（长思考会把界面拖卡）。
      if (msg.type === 'stream_delta') {
        const { kind, text } = (msg.data || {}) as { kind: string; text: string };
        if (!text) return;
        streamBuffer.push({ kind: kind === 'reasoning' ? 'reasoning' : 'text', text });
        if (!streamFlushTimer) {
          streamFlushTimer = setTimeout(flushStreamBuffer, 100);
        }
        return;
      }

      // 审批已被后端裁决（超时 / 其他客户端处理）：同步更新本地卡片
      if (msg.type === 'approval_resolved') {
        const d = (msg.data || {}) as { id?: string; allow?: boolean; reason?: string };
        markApprovalResolved(d.id, Boolean(d.allow), d.reason);
        return;
      }

      // 审批事件：用户此前对该工具选过「总是允许」→ 自动放行，不再弹卡片
      if (msg.type === 'approval') {
        const at = String((msg.data || {}).tool || '');
        if (at && alwaysAllowedTools.has(at)) {
          const aid = (msg.data || {}).id as string | undefined;
          markApprovalResolved(aid, true, 'auto-always');
          if (aid && ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'approval_response', id: aid, allow: true }));
          }
          return;
        }
      }

      // answer 事件：先把节流缓冲的增量落盘，再判断是否已有流式回答（避免重复渲染）
      if (msg.type === 'answer') {
        flushStreamBuffer();
        if (streamingMessageId) {
          const { messages } = useSessionStore.getState();
          const activeSessionId = eventSessionId();
          if (activeSessionId && (messages[activeSessionId] || []).some((m) => m.id === streamingMessageId)) {
            streamingMessageId = null;
            return;
          }
        }
        streamingMessageId = null;
      }

      // error 视为任务终止：解除会话锁定
      if (msg.type === 'error') runSessionId = null;

      // 事件 → 消息（写入任务归属会话）
      const chatMsg = eventToMessage({ type: msg.type, data: msg.data || {}, timestamp: Date.now() });
      if (chatMsg) {
        const activeSessionId = eventSessionId();
        const { addMessage } = useSessionStore.getState();
        if (activeSessionId) addMessage(activeSessionId, chatMsg);
      }
    } catch { /* 忽略解析失败 */ }
  };
  ws.onclose = () => {
    useBackendStore.getState().setConnected(false);
    useBackendStore.getState().setRunning(false);
    streamingMessageId = null;
    runSessionId = null;
    scheduleReconnect();
  };
  ws.onerror = () => ws?.close();
  } finally {
    connecting = false;
  }
}

function scheduleReconnect(): void {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectBackend, 2000);
}

/** 清空流式节流缓冲 */
function clearStreamBuffer(): void {
  streamBuffer = [];
  if (streamFlushTimer) {
    clearTimeout(streamFlushTimer);
    streamFlushTimer = null;
  }
}

/** 把节流缓冲的增量一次性写入当前流式消息（reasoning 与 text 分开追加） */
function flushStreamBuffer(): void {
  const buf = streamBuffer;
  streamBuffer = [];
  streamFlushTimer = null;
  if (buf.length === 0) return;

  const reasoningText = buf.filter((b) => b.kind === 'reasoning').map((b) => b.text).join('');
  const answerText = buf.filter((b) => b.kind === 'text').map((b) => b.text).join('');

  const { messages, addMessage } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId) return;
  const list = messages[activeSessionId] || [];

  if (streamingMessageId && list.some((m) => m.id === streamingMessageId)) {
    useSessionStore.setState((st) => ({
      messages: {
        ...st.messages,
        [activeSessionId]: st.messages[activeSessionId].map((m) => {
          if (m.id !== streamingMessageId) return m;
          return {
            ...m,
            reasoning: (m.reasoning || '') + reasoningText,
            content: m.content + answerText,
          };
        }),
      },
    }));
  } else if (reasoningText || answerText) {
    const newId = `ans-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`;
    streamingMessageId = newId;
    addMessage(activeSessionId, {
      id: newId, role: 'assistant',
      content: answerText,
      reasoning: reasoningText || '',
      kind: 'text', timestamp: Date.now(),
    });
  }
}

/** 后端（重）连上后，把桌面端设置里的视觉模型覆盖重新下发（后端重启会丢内存覆盖） */
export async function syncRuntimeConfig(): Promise<void> {
  try {
    const cfg = loadConfig();
    const vision: Record<string, string> = {};
    if (cfg.visionModel) vision.model = cfg.visionModel;
    if (cfg.visionBaseUrl) vision.base_url = cfg.visionBaseUrl;
    if (cfg.visionApiKey) vision.api_key = cfg.visionApiKey;
    if (Object.keys(vision).length === 0) return;
    await fetch(`${await getApiBase()}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vision }),
    });
  } catch { /* 后端不可达时忽略，下次连接再同步 */ }
}

/** 发送一个目标到当前会话：加用户消息气泡 + 提交后端（已配置端点+模型则真实调用，仅完全未配置才演示）。
 *  App.handleSend 与任务队列排空共用这一条路径。
 *  sessionId 缺省时取当前选中会话；任务队列显式传入"发起任务的会话"，保证不串台。 */
export async function sendGoal(
  text: string,
  attachments: { name: string; content: string }[],
  sessionId?: string,
): Promise<void> {
  const { activeSessionId, addMessage } = useSessionStore.getState();
  const sid = sessionId || activeSessionId;
  if (!sid) return;
  // 锁定本次任务归属会话：之后的所有事件（流式/工具/结果）都写回这个会话，
  // 即使中途切到别的会话也不会串
  runSessionId = sid;
  const c = loadConfig();
  addMessage(sid, { id: `u-${Date.now()}`, role: 'user', content: text || '(附件)', timestamp: Date.now() });
  const configured = Boolean(c.baseUrl && c.model);
  const useDemo = !c.apiKey && !configured;
  const ok = useDemo ? await submitDemo(text) : await submitGoal(text, attachments, c, sid);
  if (ok) {
    // 立即标记归属会话（run_start 事件到达前就显示"运行中"，且只在本会话显示）
    useBackendStore.getState().setRunning(true, sid);
  } else {
    runSessionId = null;   // 提交失败：解除锁定
  }
  if (!ok) {
    addMessage(sid, {
      id: `e-${Date.now()}`, role: 'system', kind: 'text',
      content: '⚠️ 无法连接后端，请确认 dashboard 服务已启动（desktop:backend）',
      timestamp: Date.now(),
    });
  }
}

/** 排空任务队列：run_end 或重连后自动下发下一条排队任务（运行中/断连时不动）。
 *  关键：用队列条目记录的发起会话 id 下发，而不是当前选中的会话——避免 A 会话排队的
 *  任务在用户切到 B 后落到 B。 */
export async function drainQueue(): Promise<void> {
  const { queue, dequeueGoal, connected, running } = useBackendStore.getState();
  if (!queue.length || !connected || running) return;
  const next = dequeueGoal();
  if (next) await sendGoal(next.text, next.attachments, next.sessionId);
}

/** 组装提交 payload（含前端配置：模型/端点/key 覆盖 .env） */
function buildPayload(
  goal: string,
  attachments?: { name: string; content: string }[],
  cfg?: { apiKey?: string; model?: string; baseUrl?: string; agentName?: string },
  sessionId?: string | null,
): Record<string, unknown> {
  let text = goal;
  if (attachments?.length) {
    const parts = attachments.map((a) => `[附件 ${a.name}]\n${a.content.slice(0, 1000)}`).join('\n\n');
    text = `${goal}\n\n---\n${parts}`;
  }
  const params = useParamsStore.getState().params;
  const permMode = usePermissionStore.getState().mode;
  // 运行时引擎：按会话绑定（Session.runtime），custom 从 localStorage 取命令模板
  const sid = (sessionId || useSessionStore.getState().activeSessionId) || undefined;
  const session = useSessionStore.getState().sessions.find((s) => s.id === sid);
  const runtime = session?.runtime || 'myagent';
  let runtime_config: { command?: string } | undefined;
  if (runtime === 'custom') {
    const cmd = localStorage.getItem('my-agent-custom-runtime') || '';
    if (cmd) runtime_config = { command: cmd };
  }
  return {
    goal: text,
    temperature: params.temperature,
    top_p: params.topP,
    max_tokens: params.maxTokens,
    // 任务最大操作轮数：0 = 后端默认（.env MAX_LOOP_OPS）；>0 覆盖本轮
    max_ops: params.maxOps > 0 ? params.maxOps : undefined,
    permission_mode: permMode,
    // 关键：把桌面端初始化填的模型/端点/key 真正传给后端（否则一直用 .env 默认值）
    model: cfg?.model || undefined,
    base_url: cfg?.baseUrl || undefined,
    api_key: cfg?.apiKey || undefined,
    // 会话连续性：后端按此 id 恢复历史上下文并自动保存本轮记录。
    // 用任务归属会话（而不是提交瞬间"当前选中的会话"），与事件落盘保持一致
    session_id: sid,
    // 多 Agent Runtime：会话绑定的引擎（内置 / 外部 CLI / 自定义）
    runtime,
    runtime_config,
  };
}

/** 提交任务到后端 */
export async function submitGoal(
  goal: string,
  attachments?: { name: string; content: string }[],
  cfg?: { apiKey?: string; model?: string; baseUrl?: string; agentName?: string },
  sessionId?: string | null,
): Promise<{ ok: boolean }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildPayload(goal, attachments, cfg, sessionId)),
    });
    const json = await res.json();
    return { ok: Boolean(json.ok) };
  } catch {
    return { ok: false };
  }
}

/** 把本地审批卡片替换为裁决结果提示（本人操作或后端 approval_resolved 都会走这里） */
function markApprovalResolved(id: string | undefined, allow: boolean, reason?: string): void {
  const { messages, addMessage } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId) return;
  const list = messages[activeSessionId] || [];
  let idx = -1;
  if (id) {
    idx = list.findIndex((m) => m.kind === 'approval' && (m.meta as { id?: string } | undefined)?.id === id);
  } else {
    // 无 id：取最近一张未处理的审批卡片
    for (let i = list.length - 1; i >= 0; i--) {
      if (list[i].kind === 'approval') { idx = i; break; }
    }
  }
  const verdict = reason === 'timeout'
    ? '⏱ 审批超时，已自动拒绝'
    : reason === 'auto-always'
      ? '✅ 已自动允许（你此前对该工具选择了「总是允许」）'
      : allow ? '✅ 已允许执行' : '🚫 已拒绝';
  if (idx >= 0) {
    useSessionStore.setState((st) => ({
      messages: {
        ...st.messages,
        [activeSessionId]: (st.messages[activeSessionId] || []).map((m, i) =>
          i === idx ? { ...m, kind: 'text' as const, role: 'system' as const, content: `${verdict}：${m.content}` } : m,
        ),
      },
    }));
  } else {
    addMessage(activeSessionId, {
      id: `ap-r-${Date.now()}`, role: 'system', kind: 'text', content: verdict, timestamp: Date.now(),
    });
  }
}

/** 审批决定回传后端（WebSocket 优先，断线时 HTTP /api/approve 兜底），并同步本地卡片 */
export async function sendApprovalResponse(id: string | undefined, allow: boolean): Promise<void> {
  markApprovalResolved(id, allow);
  usePermissionStore.getState().resolveApproval(id || '', allow);
  if (!id) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'approval_response', id, allow }));
    return;
  }
  try {
    await fetch(`${await getApiBase()}/api/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, allow }),
    });
  } catch { /* 后端不可达时忽略，本地已标记 */ }
}

/** 文件树节点（对齐后端 /api/files 返回结构） */
export interface FileTreeNode {
  name: string;
  path: string;
  type: 'dir' | 'file';
  children?: FileTreeNode[];
}

/** 技能包信息（对齐后端 /api/skills 返回结构；scripts 仅告知，不自动执行） */
export interface SkillInfo {
  name: string;
  description: string;
  triggers: string[];
  scripts: string[];
  path?: string;
}

/** 拉取可用技能包列表（技能面板数据源；失败静默为空列表） */
export async function fetchSkills(): Promise<SkillInfo[]> {
  try {
    const res = await fetch(`${await getApiBase()}/api/skills`);
    const json = await res.json();
    return json.ok ? (Array.isArray(json.skills) ? json.skills : []) : [];
  } catch {
    return [];
  }
}

/** 拉取工作目录文件树（只读；后端绑定工作区，越界不可达） */
export async function fetchFileTree(): Promise<{ root: string; tree: FileTreeNode } | null> {
  try {
    const res = await fetch(`${await getApiBase()}/api/files`);
    const json = await res.json();
    if (!json.tree) return null;
    return { root: String(json.root || ''), tree: json.tree as FileTreeNode };
  } catch {
    return null;
  }
}

/** 读取工作区内文件内容（只读预览） */
export async function fetchFileContent(
  path: string,
): Promise<{ ok: boolean; content?: string; truncated?: boolean; error?: string }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/file?path=${encodeURIComponent(path)}`);
    return await res.json();
  } catch {
    return { ok: false, error: '后端不可达' };
  }
}

/** 文件 diff 结果（对齐后端 /api/diff 返回结构） */
export interface FileDiff {
  ok: boolean;
  path?: string;
  tool?: string;
  writes?: number;
  is_new?: boolean;
  deleted?: boolean;
  old?: string;
  new?: string;
  unified_diff?: string;
  added?: number;
  removed?: number;
  timestamp?: number;
  error?: string;
}

/** 拉取文件修改前后对比（数据源：后端变更追踪器，仅覆盖 file/edit 工具的写入） */
export async function fetchDiff(path: string): Promise<FileDiff> {
  try {
    const res = await fetch(`${await getApiBase()}/api/diff?path=${encodeURIComponent(path)}`);
    return await res.json();
  } catch {
    return { ok: false, error: '后端不可达' };
  }
}

/** 停止当前运行中的任务：后端置位 stop_event，执行器在下一个检查点退出，
 * 正在运行的前台子进程（terminal/python）会被强杀——真停止，不是心理安慰。 */
/** 查询后端当前活动运行（刷新/重连后恢复 running 状态，保证能停止） */
export async function fetchRunning(): Promise<{ running: boolean; sessions: string[] }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/running`);
    const json = await res.json();
    return { running: Boolean(json.running), sessions: Array.isArray(json.sessions) ? json.sessions : [] };
  } catch {
    return { running: false, sessions: [] };
  }
}

/** 名字保存后刷新界面壳标题（文档标题 + Electron 窗口标题/通知跟随） */
export function refreshShellTitle(name?: string): void {
  const n = displayAgentName(name ?? loadConfig().agentName);
  document.title = `${n} Desktop`;
  void window.desktopApi?.setShellTitle?.(n).catch(() => {});
}

/** 运行中切换某会话的权限模式（auto/ask/block）：对进行中的任务立即生效 */
export async function setPermissionMode(mode: string): Promise<boolean> {
  try {
    const res = await fetch(`${await getApiBase()}/api/permission-mode`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode,
        session_id: runSessionId || useSessionStore.getState().activeSessionId || 'default',
      }),
    });
    const json = await res.json();
    return Boolean(json.ok);
  } catch {
    return false;
  }
}

export async function stopRun(): Promise<boolean> {
  try {
    const sid = runSessionId || useSessionStore.getState().activeSessionId || undefined;
    const res = await fetch(`${await getApiBase()}/api/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: sid ? JSON.stringify({ session_id: sid }) : undefined,
    });
    const json = await res.json();
    return Boolean(json.ok && json.stopped);
  } catch {
    return false;
  }
}

/** 把 checkpoint 的 commit hash 挂到最近一条同工具、已成功的工具行上（供「回滚到此处」） */
function attachCommitToToolRow(d: { tool?: string; commit?: string }): void {
  const { messages } = useSessionStore.getState();
  const activeSessionId = eventSessionId();
  if (!activeSessionId || !d.commit) return;
  const list = messages[activeSessionId] || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const m = list[i];
    const meta = (m.meta || {}) as { tool?: string; success?: boolean; commit?: string };
    if (m.kind === 'command' && meta.success === true && !meta.commit
        && String(d.tool ?? meta.tool) === String(meta.tool)) {
      useSessionStore.setState((st) => ({
        messages: {
          ...st.messages,
          [activeSessionId]: st.messages[activeSessionId].map((x, j) =>
            j === i ? { ...x, meta: { ...(x.meta || {}), commit: d.commit } } : x,
          ),
        },
      }));
      return;
    }
  }
}

/** 回滚工作区到某个 checkpoint（后端以新提交方式恢复树，不重写历史） */
export async function rollbackToCheckpoint(commit: string): Promise<{ ok: boolean; head?: string; error?: string }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ commit }),
    });
    return await res.json();
  } catch {
    return { ok: false, error: '后端不可达' };
  }
}

/** 删除后端持久化对话（会话连续性：前端删会话时同步清理；后端不存在时静默成功） */
export async function deleteBackendSession(id: string): Promise<void> {
  if (!id) return;
  try {
    await fetch(`${await getApiBase()}/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch { /* 后端不可达时忽略 */ }
}

// ---------------- 后端会话（单一数据源） ----------------

/** 后端会话列表里的元信息字段（对齐 /api/sessions 返回） */
export interface BackendSessionSummary {
  id: string;
  title: string;
  count: number;
  created_at: string;
  updated_at: string;
  /** 最后一条消息的内容片段（会话卡片预览） */
  preview?: string;
}

/** 创建会话：后端生成 conv-YYYYMMDD-hex id 并落盘；失败返回 { ok:false }（前端本地兜底） */
export async function createBackendSession(): Promise<{
  ok: boolean;
  session?: { id: string; title: string; created_at?: string; updated_at?: string };
}> {
  try {
    const res = await fetch(`${await getApiBase()}/api/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    return await res.json();
  } catch {
    return { ok: false };
  }
}

/** 测试 LLM 连通性：用给定（或当前）配置做一次最小真实调用（设置页「测试连接」） */
export async function testLlmConnection(opts: {
  model?: string;
  baseUrl?: string;
  apiKey?: string;
}): Promise<{ ok: boolean; latency_ms?: number; reply?: string; error?: string; endpoint?: string }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/test-llm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: opts.model || undefined,
        base_url: opts.baseUrl || undefined,
        api_key: opts.apiKey || undefined,
      }),
    });
    return await res.json();
  } catch {
    return { ok: false, error: '无法连接后端（请确认 dashboard 服务已启动）' };
  }
}

/** 拉取后端全部会话元信息（启动/重连时合并到本地会话列表） */
export async function listBackendSessions(): Promise<BackendSessionSummary[]> {
  try {
    const res = await fetch(`${await getApiBase()}/api/sessions`);
    const json = await res.json();
    return Array.isArray(json.sessions) ? json.sessions : [];
  } catch {
    return [];
  }
}

/** 拉取单个会话的完整 transcript（选中有后端记录但本地无消息的会话时灌入） */
export async function fetchBackendSession(
  id: string,
): Promise<{ messages: { role: string; content: string }[] } | null> {
  try {
    const res = await fetch(`${await getApiBase()}/api/sessions/${encodeURIComponent(id)}`);
    const json = await res.json();
    if (json.ok && Array.isArray(json.conversation?.messages)) {
      return { messages: json.conversation.messages };
    }
    return null;
  } catch {
    return null;
  }
}

/** 启动/重连后把后端会话合并进本地（后端为单一数据源：只增不删，本地离线会话保留） */
export async function syncSessionsFromBackend(): Promise<void> {
  try {
    const remote = await listBackendSessions();
    if (!remote.length) return;
    const sessions = useSessionStore.getState().sessions;
    const known = new Set(sessions.map((s) => s.id));
    const now = new Date().toISOString();
    for (const r of remote) {
      const patch: Partial<Session> = {
        title: r.title || '新对话',
        updatedAt: r.updated_at || now,
        messageCount: r.count || 0,
        preview: r.preview,
      };
      if (known.has(r.id)) {
        // 已存在会话也刷新 preview/count/updatedAt，让会话卡片始终对齐后端
        useSessionStore.getState().updateSessionMeta(r.id, patch);
      } else {
        useSessionStore.getState().addSessionMeta(r.id, {
          id: r.id,
          title: r.title || '新对话',
          createdAt: r.created_at || now,
          updatedAt: r.updated_at || now,
          pinned: false,
          messageCount: r.count || 0,
          preview: r.preview,
        });
      }
    }
  } catch { /* 后端不可达时跳过，localStorage 继续工作 */ }
}

/** 选中会话时把后端 transcript 灌入本地（本地已有消息则跳过，避免覆盖流式/未保存内容） */
export async function hydrateSessionMessages(id: string): Promise<void> {
  if (!id) return;
  const { messages } = useSessionStore.getState();
  if (messages[id] && messages[id].length) return;
  const data = await fetchBackendSession(id);
  if (!data?.messages?.length) return;
  const chatMsgs: ChatMessage[] = data.messages.map((m, i) => ({
    id: `bk-${id}-${i}`,
    role: m.role === 'user' ? 'user' : m.role === 'tool' ? 'tool' : 'assistant',
    content: m.content || '',
    kind: 'text' as const,
    timestamp: Date.now(),
  }));
  useSessionStore.getState().loadMessages(id, chatMsgs);
}

/** 新建会话：走后端生成 id（与 cmd 命名空间一致、重启可恢复）；离线回退本地 id。 */
export async function createNewSession(): Promise<void> {
  const store = useSessionStore.getState();
  let s;
  try {
    const res = await createBackendSession();
    if (res.ok && res.session?.id) {
      s = store.createSession('新对话', res.session.id);
      useUIStore.getState().addTab(s.id);
      return;
    }
  } catch { /* 后端不可达，回退本地 */ }
  s = store.createSession('新对话');
  useUIStore.getState().addTab(s.id);
}

/** 任务失败后重试续跑：用同一目标 + 同一会话重新提交（上下文已保留）。 */
export function retryLastRun(sessionId: string, goal: string): void {
  if (!sessionId || !goal) return;
  void sendGoal(goal, [], sessionId);
}

/** 切换项目：以项目目录重启后端（切换 workdir，项目=目录）。 */
export async function switchProject(path: string): Promise<{ ok: boolean }> {
  if (!path) return { ok: false };
  try {
    const res = await window.desktopApi?.backendRestart?.(path);
    return { ok: Boolean(res?.ok) };
  } catch {
    return { ok: false };
  }
}

/** 本地全文搜索：会话历史 + 工作区文件。 */
export async function searchLocal(
  q: string,
  scope: 'all' | 'conversations' | 'files' = 'all',
): Promise<{ conversations: { session_id: string; title: string; role: string; snippet: string }[]; files: { path: string; match: string; snippet?: string }[] }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/search?q=${encodeURIComponent(q)}&scope=${scope}`);
    const json = await res.json();
    return { conversations: json.conversations || [], files: json.files || [] };
  } catch {
    return { conversations: [], files: [] };
  }
}

/** Git 分支与状态（modified/untracked）。 */
export async function fetchGit(): Promise<{ branch: string; in_repo: boolean; files: { path: string; state: string }[] }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/git`);
    const json = await res.json();
    return { branch: String(json.branch || ''), in_repo: Boolean(json.in_repo), files: json.files || [] };
  } catch {
    return { branch: '', in_repo: false, files: [] };
  }
}

/** 工作区文件搜索（按文件名/内容）。 */
export async function fileSearch(q: string): Promise<{ path: string; match: string; snippet?: string }[]> {
  try {
    const res = await fetch(`${await getApiBase()}/api/file-search?q=${encodeURIComponent(q)}`);
    const json = await res.json();
    return json.files || [];
  } catch {
    return [];
  }
}

/** 可用 Agent 运行时（内置 + 外部 CLI 是否安装）。 */
export async function fetchRuntimes(): Promise<{ runtimes: Record<string, string>; available: Record<string, boolean> }> {
  try {
    const res = await fetch(`${await getApiBase()}/api/runtimes`);
    const json = await res.json();
    return { runtimes: json.runtimes || {}, available: json.available || {} };
  } catch {
    return { runtimes: {}, available: {} };
  }
}

/** 演示模式（无 key 时跑通全流程） */
export async function submitDemo(goal: string): Promise<{ ok: boolean }> {  try {
    const res = await fetch(`${await getApiBase()}/api/demo`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal }),
    });
    const json = await res.json();
    return { ok: Boolean(json.ok) };
  } catch {
    return { ok: false };
  }
}
