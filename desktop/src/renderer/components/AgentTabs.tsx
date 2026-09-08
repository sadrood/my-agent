/**
 * 多标签 Agent：每个 Tab = 一个独立会话（可并行，各自运行状态）。
 * 「+」新标签（Ctrl+N）；点 Tab 切换；运行中的 Tab 显示圆点；× 关闭。
 */
import React from 'react';
import { Plus, X } from 'lucide-react';
import { useUIStore, useSessionStore, useBackendStore } from '../store';
import { createNewSession } from '../lib/backend';
import type { Session } from '../lib/types';

/** 引擎短标（对齐 InputArea 的 RUNTIME_LABELS） */
const ENGINE_SHORT: Record<string, string> = {
  myagent: '内置', claude: 'CLI', 'claude-acp': 'ACP', codex: 'JSON', custom: '自定义',
};

export function AgentTabs() {
  const { tabIds, removeTab } = useUIStore();
  const { sessions, activeSessionId, selectSession } = useSessionStore();
  const runningSessionId = useBackendStore((s) => s.runningSessionId);
  const tabs = tabIds.map((id) => sessions.find((s) => s.id === id)).filter(Boolean) as Session[];

  const newTab = () => { void createNewSession(); };

  const closeTab = (id: string) => {
    removeTab(id);
    if (activeSessionId === id) {
      const idx = tabs.findIndex((t) => t.id === id);
      const next = tabs[idx + 1]?.id ?? tabs[idx - 1]?.id;
      if (next) selectSession(next);
    }
  };

  return (
    <div className="agent-tabs">
      {tabs.map((t) => (
        <span
          key={t.id}
          className={`agent-tab ${t.id === activeSessionId ? 'active' : ''}`}
          title={`${t.title} · 引擎: ${ENGINE_SHORT[t.runtime || 'myagent'] || t.runtime}`}
          onClick={() => selectSession(t.id)}
        >
          {runningSessionId === t.id && <span className="at-dot" title="运行中" />}
          <span className="at-title">{t.title || '新对话'}</span>
          {t.runtime && t.runtime !== 'myagent' && (
            <span className="at-engine">{ENGINE_SHORT[t.runtime] || t.runtime}</span>
          )}
          <button className="collapse-btn at-close" title="关闭标签" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}>
            <X size={10} />
          </button>
        </span>
      ))}
      <button className="agent-tab-new" title="新标签（Ctrl+N）" onClick={newTab}><Plus size={13} /></button>
    </div>
  );
}
