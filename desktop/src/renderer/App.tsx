/**
 * 主应用：三栏布局装配 + 后端连接 + 首次引导 + 键盘快捷键。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { StatusBar } from './components/StatusBar';
import { LeftPanel } from './components/LeftPanel';
import { ChatFlow } from './components/ChatFlow';
import { InputArea } from './components/InputArea';
import { PetBridge } from './components/PetBridge';
import { OnboardingModal } from './components/Onboarding';
import { RightDock } from './components/RightDock';
import { ResizeHandle } from './components/ResizeHandle';
import { loadConfig, markSecureInUse, needsOnboarding, setSecureCache } from './lib/appConfig';
import { connectBackend, sendGoal, createNewSession, syncSessionsFromBackend, hydrateSessionMessages, stopRun } from './lib/backend';
import { useSessionStore, useUIStore, useBackendStore, clampWidthToWindow } from './store';
import { X, Search } from 'lucide-react';
import { SearchPalette } from './components/SearchPalette';
import { AgentTabs } from './components/AgentTabs';
import { SettingsModal } from './components/SettingsModal';
import { RailPanel } from './components/RailPanel';

export default function App() {
  const { theme, leftPanelOpen, rightPanelOpen } = useUIStore();
  const { activeSessionId, messages, createSession, selectSession } = useSessionStore();
  const connected = useBackendStore((s) => s.connected);
  const [onboarded, setOnboarded] = useState(false);
  const [cfg, setCfg] = useState(loadConfig());
  const [searchOpen, setSearchOpen] = useState(false);

  // 首次引导
  useEffect(() => {
    const c = loadConfig();
    setCfg(c);
    setOnboarded(!needsOnboarding(c));
  }, []);

  // 确保有活动会话（初始为 Tab）
  useEffect(() => {
    if (!activeSessionId) {
      const s = createSession('新对话');
      selectSession(s.id);
      useUIStore.getState().addTab(s.id);
    }
  }, [activeSessionId, createSession, selectSession]);

  // 连接后端
  useEffect(() => {
    connectBackend();
  }, []);

  // 桌面宠物：按持久化开关恢复 + 运行状态推送（气泡随任务变化）
  const running = useBackendStore((s) => s.running);
  const events = useBackendStore((s) => s.events);
  const petVisible = useUIStore((s) => s.petVisible);
  useEffect(() => {
    if (petVisible) void window.desktopApi?.petToggle?.(true);
  }, []);
  useEffect(() => {
    void window.desktopApi?.petStatus?.(running);
  }, [running]);
  // 任务结束（run_end）→ 推送结束状态，宠物头顶 ✅/❌/⏹ 飘字
  const lastEvent = events[events.length - 1];
  useEffect(() => {
    if (lastEvent?.type === 'run_end') {
      const st = String((lastEvent.data as Record<string, unknown>)?.status || '');
      void window.desktopApi?.petStatus?.(false, st);
    }
  }, [lastEvent]);
  // 宠物菜单动作：新对话 / 暂停任务（主窗口在前台响应）
  useEffect(() => {
    window.desktopApi?.onPetAction?.();
    const handler = async (e: Event) => {
      const action = (e as CustomEvent).detail;
      if (action === 'new-session') await createNewSession();
      if (action === 'stop-run') await stopRun();
    };
    window.addEventListener('pet-action', handler);
    return () => window.removeEventListener('pet-action', handler);
  }, []);
  const setPetVisible = useUIStore((s) => s.setPetVisible);
  useEffect(() => {
    if (!petVisible) void window.desktopApi?.petToggle?.(false);
    void setPetVisible;
  }, [petVisible, setPetVisible]);

  // 后端连上 → 把后端会话合并进本地（后端为单一数据源；重连也会触发）
  useEffect(() => {
    if (connected) void syncSessionsFromBackend();
  }, [connected]);

  // 选中会话 → 本地无消息时从后端拉取完整 transcript 灌入
  useEffect(() => {
    if (activeSessionId && connected) void hydrateSessionMessages(activeSessionId);
  }, [activeSessionId, connected]);

  // 窗口缩放 → 重新夹紧左右栏宽度，保证中央对话流不被挤没
  useEffect(() => {
    const onResize = () => clampWidthToWindow();
    window.addEventListener('resize', onResize);
    clampWidthToWindow();
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // 密钥安全层：探测 safeStorage → 解密注入缓存 → 把 localStorage 里的明文残留迁入安全文件
  useEffect(() => {
    void (async () => {
      const api = window.desktopApi;
      if (!api?.secureGet || !api.secureSet) return;
      markSecureInUse();
      try {
        const [a, v] = await Promise.all([api.secureGet('apiKey'), api.secureGet('visionApiKey')]);
        setSecureCache({ apiKey: a.value || '', visionApiKey: v.value || '' });
        const raw = JSON.parse(localStorage.getItem('my-agent-config') || '{}') as { apiKey?: string; visionApiKey?: string };
        if (raw.apiKey) void api.secureSet('apiKey', raw.apiKey);
        if (raw.visionApiKey) void api.secureSet('visionApiKey', raw.visionApiKey);
      } catch { /* 下次启动再迁移 */ }
    })();
  }, []);

  // 主题变化 → 同步 Windows 原生标题栏悬浮层颜色（设置弹窗/状态栏/系统跟随都走这里）
  useEffect(() => {
    void window.desktopApi?.setTitleBarOverlay?.(theme).catch(() => {});
  }, [theme]);

  // 首次使用（无主题偏好记录）时跟随系统深浅色
  useEffect(() => {
    if (!localStorage.getItem('my-agent-ui')) {
      const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
      useUIStore.getState().setTheme(prefersLight ? 'light' : 'dark');
    }
  }, []);

  // 主题切换同步到 <html>：body/html 必须吃到主题级联，否则窗口两侧露出
  // :root 的深色底（浅色主题下两侧发黑的根因——仅靠内层 div 的 data-theme 不够）
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // 左侧「设置」按钮 → 重新打开配置引导（模型 / API Key）
  useEffect(() => {
    const open = () => setOnboarded(false);
    window.addEventListener('open-onboarding', open);
    return () => window.removeEventListener('open-onboarding', open);
  }, []);

  // 左侧搜索按钮 → 打开本地全文搜索浮层
  useEffect(() => {
    const open = () => setSearchOpen(true);
    window.addEventListener('open-search', open);
    return () => window.removeEventListener('open-search', open);
  }, []);

  // 设置中心（左栏「设置」按钮 / 快捷键派发）
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => {
    const open = () => setSettingsOpen(true);
    window.addEventListener('open-settings', open);
    return () => window.removeEventListener('open-settings', open);
  }, []);

  // 内嵌浏览器：Agent（Python 桥）请求打开/关闭侧栏浏览器页。
  // 多标签：BrowserPane 自己监听 embedded-browser-open 建 tab；这里只负责把面板打开。
  useEffect(() => {
    const onOpen = (e: Event) => {
      let url = '';
      try { url = (JSON.parse((e as CustomEvent).detail) || {}).url || ''; } catch { url = ''; }
      const ui = useUIStore.getState();
      if (!ui.rightPanelOpen) ui.toggleRightPanel();
      ui.setRightTab('browser');
      // 存入 UI store，BrowserPane 挂载/变化时据此建 tab（事件可能早于面板挂载而丢失）
      ui.setEmbeddedNav({ seq: Date.now(), url });
    };
    const onClose = () => {
      const ui = useUIStore.getState();
      if (ui.rightTab === 'browser') ui.setRightTab('files');
    };
    window.addEventListener('embedded-browser-open', onOpen);
    window.addEventListener('embedded-browser-close', onClose);
    // 注册 preload 侧的 IPC → 事件桥
    window.desktopApi?.onEmbeddedBrowserOpen?.();
    window.desktopApi?.onEmbeddedBrowserClose?.();
    window.desktopApi?.onEmbeddedBrowserSwitch?.();
    window.desktopApi?.onEmbeddedBrowserCloseTab?.();
    return () => {
      window.removeEventListener('embedded-browser-open', onOpen);
      window.removeEventListener('embedded-browser-close', onClose);
    };
  }, []);

  // 内置终端：Agent 首次跑 terminal 命令 → 自动展开右栏并切到「终端」面板
  useEffect(() => {
    const onTermOpen = () => {
      const ui = useUIStore.getState();
      if (!ui.rightPanelOpen) ui.toggleRightPanel();
      ui.setRightTab('terminal');
    };
    window.addEventListener('embedded-terminal-open', onTermOpen);
    window.desktopApi?.onTerminalOpen?.();
    return () => window.removeEventListener('embedded-terminal-open', onTermOpen);
  }, []);

  // 键盘快捷键（终端风格）
  const handleKey = useCallback((e: KeyboardEvent) => {
    const t = e.target as HTMLElement;
    const inInput = t.tagName === 'TEXTAREA' || t.tagName === 'INPUT';
    // Ctrl/Cmd + B 折叠左侧栏
    if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
      e.preventDefault();
      useUIStore.getState().toggleLeftPanel();
    }
    // Ctrl/Cmd + \ 折叠右侧栏
    if ((e.ctrlKey || e.metaKey) && e.key === '\\') {
      e.preventDefault();
      useUIStore.getState().toggleRightPanel();
    }
    // Ctrl/Cmd + N 新对话（走后端生成 id，离线回退本地）
    if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
      e.preventDefault();
      void createNewSession();
    }
    // Ctrl/Cmd + K 本地全文搜索（会话历史 + 工作区文件）
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      setSearchOpen(true);
    }
    // 未聚焦输入框时，直接敲字 → 聚焦输入框
    if (!inInput && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      window.dispatchEvent(new CustomEvent('focus-input'));
    }
  }, []);

  useEffect(() => {
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [handleKey]);

  const activeMessages = activeSessionId ? messages[activeSessionId] || [] : [];

  const handleSend = async (text: string, attachments: { name: string; content: string }[]) => {
    if (!text.trim() && attachments.length === 0) return;
    if (!activeSessionId) return;
    await sendGoal(text, attachments);
  };

  return (
    <div data-theme={theme} className="h-full">
      <div className="flex h-full flex-col">
        {/* 标题栏行（横跨全宽）：状态栏兼任拖拽区，原生 overlay 按钮永远落在其右端 */}
        <StatusBar sessionId={activeSessionId} />
        <div className="flex min-h-0 flex-1">
          {leftPanelOpen ? (
            <>
              <LeftPanel />
              <ResizeHandle side="left" />
            </>
          ) : (
            <RailPanel />
          )}
          {/* 中央列最低宽度 520px（兜底）：窗口过小时侧栏被裁也不压缩对话流，
              防止输入工具栏/发送键溢出到第三栏 */}
          <div className="flex min-h-0 flex-1 flex-col bg-app" style={{ minWidth: 520 }}>
            <AgentTabs />
            <ChatFlow messages={activeMessages} sessionId={activeSessionId} />
            <InputArea onSend={handleSend} />
            <PetBridge />
          </div>
          {rightPanelOpen && (
            <>
              <ResizeHandle side="right" />
              <RightDock />
            </>
          )}
        </div>
      </div>
      {!onboarded && (
        <OnboardingModal
          firstRun={!cfg.onboarded}
          onDone={(c) => { setCfg(c); setOnboarded(true); }}
          onClose={() => setOnboarded(true)}
        />
      )}
      <SearchPalette open={searchOpen} onClose={() => setSearchOpen(false)} />
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
