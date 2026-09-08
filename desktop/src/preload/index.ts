/**
 * Preload 脚本：通过 contextBridge 向渲染进程暴露最小化、类型安全的 API。
 */
import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('desktopApi', {
  /** 后端是否在运行 */
  backendStatus: (): Promise<{ running: boolean }> => ipcRenderer.invoke('backend:status'),
  /** 重启后端进程（传 workDir = 切换工作区并以该目录重启） */
  backendRestart: (workDir?: string): Promise<{ ok: boolean }> => ipcRenderer.invoke('backend:restart', workDir),
  /** 后端 API 基地址（渲染进程直接 fetch /ws 用） */
  getBackendBase: (): Promise<string> => ipcRenderer.invoke('app:get-backend-base'),
  /** 应用版本号（StatusBar 显示用，帮用户确认是否跑的是最新构建） */
  getVersion: (): Promise<string> => ipcRenderer.invoke('app:get-version'),
  /** 当前工作目录（多工作区） */
  getWorkdir: (): Promise<string> => ipcRenderer.invoke('app:get-workdir'),
  /** 原生目录选择器（项目添加/切换） */
  pickDirectory: (): Promise<{ ok: boolean; path?: string }> => ipcRenderer.invoke('app:pick-directory'),
  /** 主题切换时同步标题栏 overlay 颜色 */
  setTitleBarOverlay: (theme: 'light' | 'dark'): Promise<void> => ipcRenderer.invoke('app:set-titlebar-overlay', theme),
  /** 任务完成：任务栏闪烁 + 系统通知（status: completed/failed/stopped/...） */
  notifyDone: (status?: string): Promise<void> => ipcRenderer.invoke('app:notify-done', status),
  /** 密钥加密存取（safeStorage/DPAPI；密文落 userData/secure.json，明文不进 localStorage） */
  secureSet: (name: string, value: string): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('secure:set', name, value),
  secureGet: (name: string): Promise<{ ok: boolean; value?: string; error?: string }> =>
    ipcRenderer.invoke('secure:get', name),
  /** 写系统剪贴板（主进程 clipboard 模块，无焦点/权限限制） */
  copyText: (text: string): Promise<{ ok: boolean }> => ipcRenderer.invoke('clipboard:write', text),
  /** 内嵌浏览器：注册/注销/激活 tab 的 webContentsId（多标签按 tab 定位用） */
  registerWebview: (tabId: string, wcId: number): Promise<void> => ipcRenderer.invoke('webview:register', tabId, wcId),
  unregisterWebview: (tabId: string): Promise<void> => ipcRenderer.invoke('webview:unregister', tabId),
  setActiveWebview: (tabId: string): Promise<void> => ipcRenderer.invoke('webview:set-active', tabId),
  /** 内嵌浏览器：Agent 桥请求切换标签页（IPC → window CustomEvent） */
  onEmbeddedBrowserSwitch: (): void => {
    ipcRenderer.on('embedded-browser:switch', (_e, data: { tab?: string | number }) => {
      window.dispatchEvent(new CustomEvent('embedded-browser-switch', { detail: JSON.stringify(data || {}) }));
    });
  },
  /** 内嵌浏览器：Agent 桥请求关闭标签页 */
  onEmbeddedBrowserCloseTab: (): void => {
    ipcRenderer.on('embedded-browser:close-tab', (_e, data: { tab?: string | number }) => {
      window.dispatchEvent(new CustomEvent('embedded-browser-close-tab', { detail: JSON.stringify(data || {}) }));
    });
  },
  /** 内嵌浏览器：注册 Agent→侧栏 的桥接事件（IPC → window CustomEvent，
   * detail 为 JSON 字符串，跨 contextIsolation 隔离世界安全）。 */
  onEmbeddedBrowserOpen: (): void => {
    ipcRenderer.on('embedded-browser:open', (_e, data: { url?: string }) => {
      window.dispatchEvent(new CustomEvent('embedded-browser-open', { detail: JSON.stringify({ url: data?.url || '' }) }));
    });
  },
  onEmbeddedBrowserClose: (): void => {
    ipcRenderer.on('embedded-browser:close', () => {
      window.dispatchEvent(new CustomEvent('embedded-browser-close'));
    });
  },
  /** 桌面宠物：显示/隐藏（缺省=切换） */
  petToggle: (show?: boolean): Promise<{ visible: boolean }> => ipcRenderer.invoke('pet:toggle', show),
  /** 桌面宠物：主窗口推送运行状态（endStatus: completed/failed/stopped 触发头顶飘字） */
  petStatus: (running: boolean, endStatus?: string): Promise<void> =>
    ipcRenderer.invoke('pet:status', running, endStatus),
  /** 桌面宠物：菜单动作（new-session / stop-run）→ 主窗口 CustomEvent 'pet-action' */
  onPetAction: (): void => {
    ipcRenderer.on('pet:action', (_e, action: string) => {
      window.dispatchEvent(new CustomEvent('pet-action', { detail: action }));
    });
  },
  /** 桌面宠物：发送菜单动作到主窗口渲染层 */
  sendPetAction: (action: string): Promise<void> => ipcRenderer.invoke('pet:action', action),
  /** 桌面宠物：增量移动窗口位置（JS 拖拽用） */
  moveWindow: (dx: number, dy: number): Promise<void> => ipcRenderer.invoke('pet:move', dx, dy),
  /** 桌面宠物：聚焦主窗口 */
  focusMain: (): Promise<void> => ipcRenderer.invoke('pet:focus-main'),
  /** 桌面宠物：订阅状态推送（IPC → CustomEvent 'pet-status'） */
  onPetStatus: (): void => {
    ipcRenderer.on('pet:status', (_e, running: boolean) => {
      window.dispatchEvent(new CustomEvent('pet-status', { detail: running }));
    });
  },
  /** 桌宠对话面板：打开面板窗口 */
  petOpenPanel: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('pet:open-panel'),
  /** 桌宠对话面板：发送一条消息（主窗口真实执行，可指定会话） */
  petSendMessage: (text: string, sessionId?: string): Promise<{ ok: boolean }> =>
    ipcRenderer.invoke('pet:send-message', text, sessionId || null),
  /** 主窗口渲染层推送（状态/摘要/用户消息）→ CustomEvent 'pet-push'（detail=对象） */
  onPetPush: (): void => {
    ipcRenderer.on('pet:push', (_e, raw: string) => {
      try {
        window.dispatchEvent(new CustomEvent('pet-push', { detail: JSON.parse(raw || '{}') }));
      } catch { /* 忽略损坏帧 */ }
    });
  },
  /** 主窗口：接收桌宠面板发来的消息（IPC → CustomEvent 'pet-chat'） */
  onPetChat: (): void => {
    ipcRenderer.on('pet:chat', (_e, text: string) => {
      window.dispatchEvent(new CustomEvent('pet-chat', { detail: text }));
    });
  },
  /** 主窗口渲染层 → 桌宠两窗推送 */
  petPush: (payload: unknown): void => {
    ipcRenderer.send('pet:push', JSON.stringify(payload ?? {}));
  },
  /** 内置终端：历史快照 / 用户执行命令 / 回显订阅（单回调，面板挂载时注册） */
  terminalSnapshot: (): Promise<unknown[]> => ipcRenderer.invoke('terminal:snapshot'),
  terminalUserRun: (command: string): Promise<{ ok: boolean; output?: string; error?: string }> =>
    ipcRenderer.invoke('terminal:user-run', command),
  onTerminalEcho: (cb: (entry: unknown) => void): void => { terminalCb = cb; },
  offTerminalEcho: (): void => { terminalCb = null; },
  /** Agent 首次跑命令：主进程请求渲染层切到「终端」面板 */
  onTerminalOpen: (): void => {
    ipcRenderer.on('terminal:open', () => {
      window.dispatchEvent(new CustomEvent('embedded-terminal-open'));
    });
  },
});

let terminalCb: ((entry: unknown) => void) | null = null;
ipcRenderer.on('terminal:echo', (_e, entry) => { terminalCb?.(entry); });

export type DesktopApi = {
  backendStatus: () => Promise<{ running: boolean }>;
  backendRestart: (workDir?: string) => Promise<{ ok: boolean }>;
  getBackendBase: () => Promise<string>;
  getVersion: () => Promise<string>;
  getWorkdir: () => Promise<string>;
  pickDirectory: () => Promise<{ ok: boolean; path?: string }>;
  setTitleBarOverlay: (theme: 'light' | 'dark') => Promise<void>;
  notifyDone: (status?: string) => Promise<void>;
  secureSet: (name: string, value: string) => Promise<{ ok: boolean; error?: string }>;
  secureGet: (name: string) => Promise<{ ok: boolean; value?: string; error?: string }>;
  copyText: (text: string) => Promise<{ ok: boolean }>;
  registerWebview: (tabId: string, wcId: number) => Promise<void>;
  unregisterWebview: (tabId: string) => Promise<void>;
  setActiveWebview: (tabId: string) => Promise<void>;
  onEmbeddedBrowserOpen: () => void;
  onEmbeddedBrowserClose: () => void;
  onEmbeddedBrowserSwitch: () => void;
  onEmbeddedBrowserCloseTab: () => void;
  petToggle: (show?: boolean) => Promise<{ visible: boolean }>;
  petStatus: (running: boolean, endStatus?: string) => Promise<void>;
  focusMain: () => Promise<void>;
  onPetStatus: () => void;
  onPetAction: () => void;
  terminalSnapshot: () => Promise<unknown[]>;
  terminalUserRun: (command: string) => Promise<{ ok: boolean; output?: string; error?: string }>;
  onTerminalEcho: (cb: (entry: unknown) => void) => void;
  offTerminalEcho: () => void;
  onTerminalOpen: () => void;
  sendPetAction: (action: string) => Promise<void>;
  moveWindow: (dx: number, dy: number) => Promise<void>;
  petOpenPanel: () => Promise<{ ok: boolean }>;
  petSendMessage: (text: string, sessionId?: string) => Promise<{ ok: boolean }>;
  onPetPush: () => void;
  onPetChat: () => void;
  petPush: (payload: unknown) => void;
};
