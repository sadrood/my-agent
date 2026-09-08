/**
 * 侧栏内嵌浏览器：多标签（每 tab 一个 <webview>）。Agent 与用户看同一个 webview 集合（所见即所控）。
 * - 每个 tab 独立 webview（persist:agent-embedded 共享登录态，各 tab 页面独立）。
 * - Agent 经 Python 桥 → Electron 主进程 → 按 tab_id 定位对应 webview 执行命令。
 * - 渲染层把每个 webview 的 tabId + webContentsId 注册给主进程（IPC webview:register）。
 */
import React, { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, RotateCw, CornerDownLeft, Plus, X, Globe } from 'lucide-react';
import { useUIStore } from '../store';

const WV = 'webview' as unknown as string;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36';
const READY_TIMEOUT = 12000;

interface WebviewEvent extends Event {
  url?: string;
  title?: string;
  errorCode?: number;
  errorDescription?: string;
  isMainFrame?: boolean;
}

interface BrowserTab {
  id: string;
  url: string;
  title: string;
}

function normalizeInputUrl(raw: string): string {
  const u = raw.trim();
  if (!u) return '';
  if (/^https?:\/\//i.test(u)) return u;
  if (/^[\w-]+(\.[\w-]+)+/.test(u)) return 'https://' + u;
  return 'https://www.bing.com/search?q=' + encodeURIComponent(u);
}

/** 单个标签页的 webview：挂载时注册到主进程，卸载注销；页面导航同步标题/地址。 */
function TabWebview({ tab, active, onState }: {
  tab: BrowserTab;
  active: boolean;
  onState: (tabId: string, patch: Partial<{ title: string; url: string; loading: boolean; ready: boolean; error: string | null; crashed?: boolean }>) => void;
}) {
  const wvRef = useRef<HTMLElement | null>(null);
  const registeredRef = useRef(false);
  const urlRef = useRef(tab.url);        // 最新目标 URL（事件回调闭包用）
  const navDoneRef = useRef(tab.url);    // 已实际导航完成的 URL（did-navigate 更新）
  const crashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => { urlRef.current = tab.url; }, [tab.url]);

  // 注册到主进程：dom-ready / did-attach 后附着完成；失败有退避重试（共 3 次）
  const register = () => {
    if (registeredRef.current) return;
    const wv = wvRef.current as unknown as { getWebContentsId?: () => number } | null;
    if (!wv) return;
    try {
      const wcId = wv.getWebContentsId?.();
      if (typeof wcId === 'number') {
        registeredRef.current = true;
        void window.desktopApi?.registerWebview?.(tab.id, wcId);
      }
    } catch { /* 未附着，由调用方重试 */ }
  };

  useEffect(() => {
    const wv = wvRef.current;
    if (!wv) return;
    const onAttach = () => register();
    const onNav = (e: Event) => {
      const ev = e as WebviewEvent;
      if (ev.url) { onState(tab.id, { url: ev.url }); navDoneRef.current = ev.url; }
    };
    const onTitle = (e: Event) => {
      const ev = e as WebviewEvent;
      if (ev.title) onState(tab.id, { title: ev.title });
    };
    const onReady = () => {
      onState(tab.id, { ready: true, error: null });
      register();
      // dom-ready 时补偿未生效的导航（open about:blank 后立即 navigate 的竞态）
      const target = urlRef.current;   // 取最新目标，避免闭包旧值
      if (target && target !== 'about:blank' && navDoneRef.current !== target) {
        (wv as unknown as { loadURL?: (u: string) => void }).loadURL?.(target);
        navDoneRef.current = target;
      }
    };
    const onStart = () => onState(tab.id, { loading: true });
    const onStop = () => onState(tab.id, { loading: false });
    const onFail = (e: Event) => {
      const ev = e as WebviewEvent;
      if (ev.isMainFrame === false || ev.errorCode === -3) return;   // -3=aborted
      onState(tab.id, {
        error: `页面加载失败（${ev.errorCode ?? '?'}）：${ev.errorDescription || '未知原因'}。`,
        loading: false,
      });
    };
    // webview 进程崩溃：白屏会永久卡住——标记 crashed，渲染层提示并可一键恢复
    const onCrashed = () => {
      onState(tab.id, { crashed: true, ready: false, loading: false });
      // 2s 后自动重载恢复（常见瞬时崩溃）
      if (crashTimer.current) clearTimeout(crashTimer.current);
      crashTimer.current = setTimeout(() => {
        const w = wvRef.current as unknown as { reload?: () => void } | null;
        (w as { reload?: () => void })?.reload?.();
        onState(tab.id, { crashed: false, loading: true });
      }, 2000);
    };
    // 配置 webview 事件
    (wv as unknown as { addEventListener: (t: string, fn: (e: Event) => void) => void }).addEventListener('did-attach', onAttach);
    wv.addEventListener('did-navigate', onNav);
    wv.addEventListener('did-navigate-in-page', onNav);
    wv.addEventListener('page-title-updated', onTitle);
    wv.addEventListener('dom-ready', onReady);
    wv.addEventListener('did-start-loading', onStart);
    wv.addEventListener('did-stop-loading', onStop);
    wv.addEventListener('did-fail-load', onFail);
    wv.addEventListener('render-process-gone', onCrashed);
    return () => {
      (wv as unknown as { removeEventListener: (t: string, fn: (e: Event) => void) => void }).removeEventListener('did-attach', onAttach);
      wv.removeEventListener('did-navigate', onNav);
      wv.removeEventListener('did-navigate-in-page', onNav);
      wv.removeEventListener('page-title-updated', onTitle);
      wv.removeEventListener('dom-ready', onReady);
      wv.removeEventListener('did-start-loading', onStart);
      wv.removeEventListener('did-stop-loading', onStop);
      wv.removeEventListener('did-fail-load', onFail);
      wv.removeEventListener('render-process-gone', onCrashed);
      if (crashTimer.current) clearTimeout(crashTimer.current);
      if (registeredRef.current) {
        registeredRef.current = false;
        void window.desktopApi?.unregisterWebview?.(tab.id);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.id]);

  // 导航可靠化：优先 loadURL() —— React 更新 <webview src> 属性不可靠，
  // 是"地址栏输入没反应/不导航"的主因。仅首次用 src 初始加载。
  useEffect(() => {
    const target = tab.url;
    if (!target || target === 'about:blank' || target === navDoneRef.current) return;
    const wv = wvRef.current as unknown as { loadURL?: (u: string) => void } | null;
    navDoneRef.current = target;   // 先标记，防并发重复触发
    onState(tab.id, { loading: true, error: null, crashed: false });
    try { wv?.loadURL?.(target); } catch { /* 未就绪：等 dom-ready 补加载 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.url]);

  return React.createElement(WV, {
    ref: wvRef,
    src: tab.url,
    partition: 'persist:agent-embedded',
    useragent: UA,
    allowpopups: false,
    className: 'browser-view',
    'data-tab-id': tab.id,
    // 用 visibility+z-index 而非 display:none 切换：display 切换会触发 webview
    // 不重绘/尺寸错乱的经典 bug，导致"界面展示不完整"
    style: { position: 'absolute', inset: 0, visibility: active ? 'visible' : 'hidden', zIndex: active ? 2 : 0 },
  });
}

export function BrowserPane() {
  const { embeddedNav } = useUIStore();
  const [tabs, setTabs] = useState<BrowserTab[]>([]);
  const [activeTabId, setActiveTabId] = useState('');
  const [perTab, setPerTab] = useState<Record<string, { loading: boolean; ready: boolean; error: string | null; crashed?: boolean }>>({});
  const [notInitialized, setNotInitialized] = useState(false);
  const [inputUrl, setInputUrl] = useState('');
  // 用当前 embeddedNav.seq 初始化：持久化残留的旧 nav（来自之前会话）启动时直接消费掉，不重放
  const appliedSeq = useRef(embeddedNav?.seq ?? 0);
  const tabSeq = useRef(0);

  const activeTab = tabs.find((t) => t.id === activeTabId);
  const activeState = (activeTabId && perTab[activeTabId]) || { loading: false, ready: false, error: null, crashed: false };

  const patchTab = (tabId: string, patch: Partial<BrowserTab>) =>
    setTabs((prev) => prev.map((t) => (t.id === tabId ? { ...t, ...patch } : t)));
  const patchState = (tabId: string, patch: Partial<{ title: string; url: string; loading: boolean; ready: boolean; error: string | null; crashed?: boolean }>) => {
    if (patch.title !== undefined) patchTab(tabId, { title: patch.title });
    if (patch.url !== undefined) patchTab(tabId, { url: patch.url });
    if (patch.loading !== undefined || patch.ready !== undefined || patch.error !== undefined || patch.crashed !== undefined) {
      setPerTab((prev) => ({
        ...prev,
        [tabId]: {
          loading: patch.loading ?? prev[tabId]?.loading ?? false,
          ready: patch.ready ?? prev[tabId]?.ready ?? false,
          error: patch.error !== undefined ? patch.error : prev[tabId]?.error ?? null,
          crashed: patch.crashed !== undefined ? patch.crashed : prev[tabId]?.crashed ?? false,
        },
      }));
    }
  };

  const openTab = (url = 'about:blank'): string => {
    const id = `tab-${Date.now().toString(36)}-${++tabSeq.current}`;
    setTabs((prev) => [...prev, { id, url, title: url === 'about:blank' ? '新标签页' : '' }]);
    setActiveTabId(id);
    setPerTab((prev) => ({ ...prev, [id]: { loading: url !== 'about:blank', ready: false, error: null } }));
    void window.desktopApi?.setActiveWebview?.(id);
    return id;
  };

  const closeTab = (id: string) => {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.id === id);
      const next = prev.filter((t) => t.id !== id);
      if (next.length === 0) {
        setActiveTabId('');
        setInputUrl('');
      } else if (activeTabId === id) {
        const nextActive = next[Math.min(idx, next.length - 1)];
        setActiveTabId(nextActive.id);
        setInputUrl(nextActive.url === 'about:blank' ? '' : nextActive.url);
        void window.desktopApi?.setActiveWebview?.(nextActive.id);
      }
      return next;
    });
  };

  const switchTab = (id: string) => {
    setActiveTabId(id);
    const t = tabs.find((x) => x.id === id);
    setInputUrl(t && t.url !== 'about:blank' ? t.url : '');
    void window.desktopApi?.setActiveWebview?.(id);
  };

  /** 当前活动标签页的 webview 元素（供工具栏返回/前进/刷新用） */
  const activeWebviewEl = () => {
    if (!activeTabId) return null;
    return document.querySelector(`webview[data-tab-id="${activeTabId}"]`) as unknown as
      { goBack(): void; goForward(): void; reload(): void } | null;
  };

  const navigate = (raw: string) => {
    const url = normalizeInputUrl(raw);
    if (!url) return;
    const id = activeTabId || openTab('about:blank');
    patchTab(id, { url });
    patchState(id, { error: null, loading: true });
    setInputUrl(url);
  };

  // Agent 桥请求：切换/关闭标签（open 由 App 经 embeddedNav store 传递，避免面板未挂载丢事件）
  useEffect(() => {
    const onSwitch = (e: Event) => {
      let target = '';
      try { target = String((JSON.parse((e as CustomEvent).detail) || {}).tab || ''); } catch { target = ''; }
      if (!target) return;
      const t = tabs.find((x) => x.id === target || x.title === target);
      if (t) switchTab(t.id);
    };
    const onCloseTab = (e: Event) => {
      let target = '';
      try { target = String((JSON.parse((e as CustomEvent).detail) || {}).tab || ''); } catch { target = ''; }
      if (!target) return;
      const t = tabs.find((x) => x.id === target);
      if (t) closeTab(t.id);
    };
    window.addEventListener('embedded-browser-switch', onSwitch);
    window.addEventListener('embedded-browser-close-tab', onCloseTab);
    return () => {
      window.removeEventListener('embedded-browser-switch', onSwitch);
      window.removeEventListener('embedded-browser-close-tab', onCloseTab);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);

  // webview 初始化超时检测（旧构建未开 webviewTag）
  useEffect(() => {
    const t = setTimeout(() => {
      if (tabs.length > 0 && !perTab[activeTabId]?.ready) setNotInitialized(true);
    }, READY_TIMEOUT);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.length, activeTabId, perTab]);

  // 暴露给上层（App 打开浏览器面板）的 Agent 导航请求：embeddedNav 里带新 URL → 新标签
  useEffect(() => {
    if (!embeddedNav || embeddedNav.seq === appliedSeq.current) return;
    appliedSeq.current = embeddedNav.seq;
    if (embeddedNav.url && embeddedNav.url !== 'about:blank') {
      const norm = embeddedNav.url;
      const existing = tabs.find((t) => t.url === norm);
      if (existing) { switchTab(existing.id); return; }
      const id = openTab(norm);
      setInputUrl(norm);
      void window.desktopApi?.setActiveWebview?.(id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [embeddedNav]);

  return (
    <div className="browser-pane">
      <div className="browser-toolbar">
        <button className="collapse-btn" title="返回" disabled={!activeWebviewEl()} onClick={() => activeWebviewEl()?.goBack()}><ArrowLeft size={13} /></button>
        <button className="collapse-btn" title="前进" disabled={!activeWebviewEl()} onClick={() => activeWebviewEl()?.goForward()}><ArrowRight size={13} /></button>
        <button className="collapse-btn" title="刷新当前页" disabled={!activeWebviewEl()} onClick={() => { patchState(activeTabId, { loading: true, error: null, crashed: false }); activeWebviewEl()?.reload(); }}><RotateCw size={12} /></button>
        <div className="address-bar">
          <span className={`addr-dot ${activeState.loading ? 'loading' : activeState.error || activeState.crashed ? 'error' : 'ok'}`} title={activeState.loading ? '加载中' : activeState.error ? '加载失败' : activeState.crashed ? '进程崩溃' : '就绪'} />
          <input
            className="search-input"
            style={{ margin: 0, flex: 1, border: 'none', background: 'transparent' }}
            placeholder="输入网址或搜索词…"
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) navigate(inputUrl);
            }}
          />
        </div>
        <button className="collapse-btn" title="转到" onClick={() => navigate(inputUrl)}><CornerDownLeft size={13} /></button>
        <button className="collapse-btn" title="新标签页" onClick={() => { const id = openTab('about:blank'); setInputUrl(''); void window.desktopApi?.setActiveWebview?.(id); }}>
          <Plus size={13} />
        </button>
      </div>
      {tabs.length > 0 && (
        <div className="browser-tabs">
          {tabs.map((t) => (
            <span key={t.id} className={`browser-tab ${t.id === activeTabId ? 'active' : ''}`} onClick={() => switchTab(t.id)}>
              <Globe size={10} />
              <span className="name">{t.title || t.url || '新标签页'}</span>
              <span className="x" title="关闭" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}><X size={9} /></span>
            </span>
          ))}
        </div>
      )}
      <div className="browser-body">
        {tabs.length === 0 && (
          <div className="browser-overlay">
            <Globe size={22} />
            <span>没有打开的页面。点 + 新建标签，或让 Agent 用 browser open 打开。</span>
          </div>
        )}
        {tabs.map((t) => (
          <TabWebview key={t.id} tab={t} active={t.id === activeTabId} onState={patchState} />
        ))}
        {activeState.loading && !activeState.error && !notInitialized && (
          <div className="browser-overlay">加载中…</div>
        )}
        {/* webview 进程崩溃：白屏时给出可操作提示，避免"看起来坏了" */}
        {activeState.crashed && !activeState.loading && (
          <div className="browser-overlay">
            <span className="err-title">⚠️ 页面进程崩溃</span>
            <span>浏览器渲染进程意外退出（已自动尝试重载）。</span>
            <button className="btn" style={{ justifyContent: 'center' }} onClick={() => { patchState(activeTabId, { crashed: false, loading: true }); activeWebviewEl()?.reload(); }}>重新加载</button>
          </div>
        )}
        {activeState.error && (
          <div className="browser-overlay">
            <span className="err-title">⚠️ 打不开这个页面</span>
            <span>{activeState.error}</span>
            <button className="btn" style={{ justifyContent: 'center' }} onClick={() => navigate(activeTab?.url || inputUrl)}>重试</button>
          </div>
        )}
        {notInitialized && tabs.length > 0 && (
          <div className="browser-overlay">
            <span className="err-title">内嵌浏览器未初始化</span>
            <span>主进程可能运行的是旧构建（未开启 webviewTag）。请在 desktop/ 目录重新构建并重启。</span>
            <code>npm run build && npm run desktop:dev</code>
          </div>
        )}
      </div>
    </div>
  );
}
