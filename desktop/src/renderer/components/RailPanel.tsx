/**
 * 极窄图标导航栏（借鉴同类实现的 56px rail 设计，MIT 许可参考实现）。
 * 左栏折叠时替代 LeftPanel：保留项目/新对话/会话/搜索/设置/任务 的图标入口，
 * 点击图标展开左栏并可选触发对应动作。
 */
import React from 'react';
import {
  MessageSquare, Plus, Search, Settings, ListChecks, PanelLeft, Sparkles,
} from 'lucide-react';
import { useUIStore } from '../store';

interface RailAction {
  icon: React.ReactNode;
  title: string;
  onOpen?: () => void;
}

export function RailPanel() {
  const expand = () => useUIStore.getState().toggleLeftPanel();

  const actions: RailAction[] = [
    { icon: <MessageSquare size={17} />, title: '会话列表', onOpen: expand },
    { icon: <Plus size={17} />, title: '新对话（Ctrl+N）', onOpen: () => {
      void import('../lib/backend').then((m) => m.createNewSession());
      expand();
    } },
    { icon: <Search size={17} />, title: '搜索会话与文件（Ctrl+K）', onOpen: () => {
      window.dispatchEvent(new CustomEvent('open-search'));
      expand();
    } },
    { icon: <ListChecks size={17} />, title: '任务中心', onOpen: () => {
      useUIStore.getState().setRightTab('tasks');
      expand();
    } },
    { icon: <Settings size={17} />, title: '设置', onOpen: () => {
      window.dispatchEvent(new CustomEvent('open-settings'));
      expand();
    } },
  ];

  return (
    <div className="rail-panel">
      <div className="rail-brand" title="小悟 Desktop（展开侧栏）" onClick={expand}>
        <Sparkles size={15} />
      </div>
      <div className="rail-nav">
        {actions.map((a, i) => (
          <button
            key={i}
            className="rail-btn"
            title={a.title}
            onClick={() => { a.onOpen?.(); }}
          >
            {a.icon}
          </button>
        ))}
      </div>
      <div className="rail-footer">
        <button className="rail-btn" title="展开侧栏" onClick={expand}>
          <PanelLeft size={16} />
        </button>
      </div>
    </div>
  );
}
