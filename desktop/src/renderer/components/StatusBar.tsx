/**
 * 顶部状态栏（克制单条）：品牌 + 后端连接状态 + 面板/主题控制；
 * 同时承担自定义标题栏的拖拽区（窗口控制按钮由 Windows 原生 overlay 提供）。
 * 运行中额外显示当前轮次与停止入口（输入区在运行时会变「停止」，此处为备用）。
 */
import React, { useEffect, useState } from 'react';
import { PanelRight, PanelLeft, Shield, ShieldCheck, ShieldOff, Wifi, WifiOff, Moon, Sun } from 'lucide-react';
import { useUIStore, useBackendStore, setCurrentWorkspace } from '../store';
import { displayAgentName } from '../lib/appConfig';
import { stopRun } from '../lib/backend';
import type { PermissionMode } from '../lib/types';

/** 权限模式的展示元数据（状态栏与 composer 共用） */
export const MODE_META: Record<PermissionMode, { label: string; dot: string; icon: React.ReactNode }> = {
  auto: { label: '自动执行', dot: 'green', icon: <ShieldCheck size={13} /> },
  ask: { label: '每次询问', dot: 'yellow', icon: <Shield size={13} /> },
  block: { label: '禁止', dot: 'red', icon: <ShieldOff size={13} /> },
};

export function StatusBar({ sessionId }: { sessionId: string | null }) {
  // Agent 显示名：设置改名后全壳跟随（空 = 默认「小悟」）
  const shellName = displayAgentName(useUIStore((s) => s.agentName));
  const { theme, setTheme, toggleRightPanel, toggleLeftPanel } = useUIStore();
  const { connected, running, turnStart } = useBackendStore();
  const [version, setVersion] = useState('');
  // git 安装版更新检测：启动 6s 后静默 fetch，落后则显示「有更新」chip
  const [upd, setUpd] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    const t = setTimeout(async () => {
      try {
        const r = await window.desktopApi?.updateCheck?.();
        if (r && r.git && Number(r.behind) > 0) setUpd(r);
      } catch { /* 非 git/无网：安静跳过 */ }
    }, 6000);
    return () => clearTimeout(t);
  }, []);
  const doUpdate = () => {
    if (!upd) return;
    if (!window.confirm(
      `发现新版本：${String(upd.current)} → ${String(upd.latest)}（落后 ${String(upd.behind)} 个提交）。
立即更新并自动重启？`,
    )) return;
    void window.desktopApi?.updateNow?.().then((r) => {
      if (r && !r.ok) window.alert(`更新失败：${String(r.error || '未知')}`);
      // ok：主进程 0.8s 后自动 relaunch
    });
  };

  // 应用版本号（悬停品牌可见：帮用户确认是否跑的是最新构建；
  // 看到旧版本 = 没重启到新 dist，需 Ctrl+C 后 npm start）
  useEffect(() => {
    window.desktopApi?.getVersion?.().then(setVersion).catch(() => {});
  }, []);

  // 当前工作目录（多工作区：仅同步到 store，不再占顶栏空间）
  useEffect(() => {
    void window.desktopApi?.getWorkdir?.().then((d) => {
      setCurrentWorkspace(d || '');
    }).catch(() => {});
  }, []);

  // 启动时按持久化主题同步标题栏 overlay 颜色：否则浅色主题下
  // Windows 原生按钮区仍是 createWindow 时的深色条（颜色"怪"的元凶之一）
  useEffect(() => {
    void window.desktopApi?.setTitleBarOverlay?.(theme).catch(() => {});
    // 仅挂载时同步一次，之后由 toggleTheme 负责
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleTheme = () => {
    setTheme(theme === 'dark' ? 'light' : 'dark');  // 悬浮层同步由 App 统一 effect 处理
  };

  const brandTitle = [
    `${shellName} Desktop`,
    version ? `构建 v${version}` : '',
    sessionId ? `会话 #${sessionId.slice(-8)}` : '',
  ].filter(Boolean).join(' · ');

  return (
    <div className="status-bar">
      <span className="brand" title={brandTitle}>{shellName}</span>
      <span className="spacer" />
      {running && turnStart && (
        <button className="status-chip running-chip" title={`任务运行中（第 ${turnStart.turn} 轮）；点击停止`} onClick={() => void stopRun()}>
          <span className="status-dot green" /> 运行中 · 第 {turnStart.turn} 轮
        </button>
      )}
      {upd && (
        <button className="status-chip update-chip" title={`新版本 ${String(upd.latest)}（当前 ${String(upd.current)}），点击更新`} onClick={doUpdate}>
          <span className="status-dot yellow" /> 有更新 ⬆ {String(upd.behind)}
        </button>
      )}
      <span className="status-chip" title={connected ? '后端已连接' : '后端未连接'}>
        {connected ? <Wifi size={13} style={{ color: 'var(--color-success)' }} /> : <WifiOff size={13} style={{ color: 'var(--color-error)' }} />}
        {connected ? '已连接' : '未连接'}
      </span>
      <button className="collapse-btn" onClick={toggleLeftPanel} title="折叠/展开左侧栏"><PanelLeft size={14} /></button>
      <button className="collapse-btn" onClick={toggleRightPanel} title="折叠/展开右侧栏"><PanelRight size={14} /></button>
      <button className="collapse-btn" title="切换亮/暗主题" onClick={toggleTheme}>
        {theme === 'dark' ? <Sun size={13} /> : <Moon size={13} />}
      </button>
    </div>
  );
}
