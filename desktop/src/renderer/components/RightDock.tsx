/**
 * 右侧面板：三主 tab 分栏式 —— ⚡概览（默认）/ 📄文件 / 🔀改动，
 * 附加功能（浏览器/任务/辅助/技能）收纳进「⋯ 更多」菜单。
 * dock-body 单一区域，所有 pane 撑满高度。
 */
import React, { useRef, useState } from 'react';
import { FileViewPane, basename } from './FileTabs';
import { SideChat } from './SideChat';
import { BrowserPane } from './BrowserPane';
import { TerminalPane } from './TerminalPane';
import { TaskCenter } from './TaskCenter';
import { SkillPane } from './SkillPane';
import { SessionOverview } from './SessionOverview';
import { GitChanges } from './GitChanges';
import { WorkspaceHeader } from './WorkspaceHeader';
import { FileTree } from './FileTree';
import { useUIStore } from '../store';
import { useTasksStore } from '../store/tasks';
import { useFileTabsStore } from '../store/fileTabs';
import {
  Globe, X, ListChecks, MessageSquare, Zap, FileText, GitBranch, Plus, Activity, Terminal as TerminalIcon,
} from 'lucide-react';

const OVERVIEW_TABS = ['overview', 'files', 'changes'] as const;
const ADDABLE_TABS: { id: string; label: string; icon: React.ReactNode; title: string }[] = [
  { id: 'browser', label: '浏览器', icon: <Globe size={12} />, title: '内嵌浏览器（Agent 可操控）' },
  { id: 'terminal', label: '终端', icon: <TerminalIcon size={12} />, title: '内置终端（Agent 与用户共用）' },
  { id: 'tasks', label: '任务', icon: <ListChecks size={12} />, title: '任务中心' },
  { id: 'side', label: '辅助', icon: <MessageSquare size={12} />, title: '辅助 Agent' },
  { id: 'skills', label: '技能', icon: <Zap size={12} />, title: '技能包（SKILL.md）' },
];

export function RightDock() {
  const { rightTab, setRightTab, rightWidth } = useUIStore();
  const files = useFileTabsStore((s) => s.files);
  const closeFile = useFileTabsStore((s) => s.closeFile);
  const tasks = useTasksStore((s) => s.tasks);
  const pendingCount = tasks.filter((t) => t.status === 'todo' || t.status === 'in_progress').length;
  // 「+」添加的面板：固定为可关闭的导航栏 tab（本次会话内记忆）
  const [pinnedTabs, setPinnedTabs] = useState<string[]>([]);
  const [moreOpen, setMoreOpen] = useState(false);
  const [morePos, setMorePos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });
  const moreRef = useRef<HTMLDivElement>(null);

  const fileTabPath = rightTab.startsWith('file:') ? rightTab.slice(5) : '';
  const fileTab = files.find((f) => f.path === fileTabPath);
  const KNOWN = new Set(['overview', 'files', 'changes', 'browser', 'terminal', 'side', 'tasks', 'skills']);
  const bottomMode = fileTab
    ? 'file'
    : (KNOWN.has(rightTab) ? rightTab : 'overview');

  // Agent（Python 桥）请求打开浏览器/终端时：自动把对应 tab 固定进导航栏，
  // 否则面板切过去了但导航栏没有按钮（用户不知道面板在哪、怎么关）
  React.useEffect(() => {
    if ((bottomMode === 'browser' || bottomMode === 'terminal') && !pinnedTabs.includes(bottomMode)) {
      setPinnedTabs((p) => [...p, bottomMode]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bottomMode]);

  // 点外部关闭更多菜单
  React.useEffect(() => {
    if (!moreOpen) return;
    const onDown = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [moreOpen]);

  const tabBtn = (id: string, icon: React.ReactNode, label: string, title: string, extra?: React.ReactNode) => (
    <button
      key={id}
      className={`dock-tab ${bottomMode === id ? 'active' : ''}`}
      onClick={() => setRightTab(id)}
      title={title}
    >
      {icon} {label}{extra}
    </button>
  );

  return (
    <div className="panel-right" style={{ width: rightWidth, minWidth: rightWidth }}>
      <div className="dock-bottom">
        {/* 工作区头（⚡项目 + Git 分支 + 文件数）：所有 tab 共享 */}
        <WorkspaceHeader />
        <div className="dock-tabbar">
          {tabBtn('overview', <Activity size={11} />, '概览', '会话概览与指标监控')}
          {tabBtn('files', <FileText size={11} />, '文件', '工作区文件')}
          {tabBtn('changes', <GitBranch size={11} />, '改动', 'Git 工作区改动')}
          {/* 「+」添加的可关闭面板 tab（浏览器/任务/辅助/技能） */}
          {pinnedTabs.map((id) => {
            const meta = ADDABLE_TABS.find((t) => t.id === id);
            if (!meta) return null;
            return (
              <button
                key={id}
                className={`dock-tab ${bottomMode === id ? 'active' : ''}`}
                title={meta.title}
                onClick={() => setRightTab(id)}
              >
                {meta.icon} {meta.label}
                <span
                  className="x"
                  title="从导航栏移除"
                  onClick={(e) => {
                    e.stopPropagation();
                    setPinnedTabs((p) => p.filter((x) => x !== id));
                    if (bottomMode === id) setRightTab('overview');
                  }}
                >
                  <X size={10} />
                </span>
              </button>
            );
          })}
          {files.map((f) => (
            <button
              key={f.path}
              className={`dock-tab ${bottomMode === 'file' && fileTabPath === f.path ? 'active' : ''}`}
              title={f.path}
              onClick={() => setRightTab(`file:${f.path}`)}
            >
              <span className="name">{basename(f.path)}</span>
              <span className="x" title="关闭" onClick={(e) => { e.stopPropagation(); closeFile(f.path); if (fileTabPath === f.path) setRightTab('overview'); }}>
                <X size={10} />
              </span>
            </button>
          ))}
          <div className="dock-spacer" />
          <div className="dock-more" ref={moreRef}>
            <button
              className="dock-tab"
              onClick={(e) => {
                // tab 栏 overflow-x:auto 会裁剪 absolute 子元素，菜单改用 fixed 按按钮坐标弹出
                const r = e.currentTarget.getBoundingClientRect();
                setMorePos({ left: Math.max(8, r.right - 180), top: r.bottom + 4 });
                setMoreOpen((v) => !v);
              }}
              title="添加面板到导航栏（浏览器 / 任务 / 辅助 / 技能）"
            >
              <Plus size={13} />
              {pendingCount > 0 && <span className="dock-tab-badge">{pendingCount > 99 ? '99+' : pendingCount}</span>}
            </button>
            {moreOpen && (
              <div className="dock-more-menu" style={{ left: morePos.left, top: morePos.top }}>
                <div className="dock-more-title">添加面板（生成新标签）</div>
                {ADDABLE_TABS.map((t) => (
                  <button
                    key={t.id}
                    className="prompt-tpl"
                    title={t.title}
                    onClick={() => {
                      // 未固定的先加入导航栏，然后激活
                      setPinnedTabs((p) => (p.includes(t.id) ? p : [...p, t.id]));
                      setRightTab(t.id);
                      setMoreOpen(false);
                    }}
                  >
                    {t.icon} {t.label}
                    {pinnedTabs.includes(t.id) && <span className="dock-more-check">已添加</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="dock-body">
          {bottomMode === 'file' && fileTab && (
            <div className="dock-pane" style={{ display: 'flex' }}>
              <FileViewPane tab={fileTab} />
            </div>
          )}
          <div className="dock-pane" style={{ display: bottomMode === 'overview' ? 'flex' : 'none' }}>
            <SessionOverview />
          </div>
          <div className="dock-pane dock-pane-files" style={{ display: bottomMode === 'files' ? 'flex' : 'none' }}>
            <FileTree />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'changes' ? 'flex' : 'none' }}>
            <GitChanges />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'browser' ? 'flex' : 'none' }}>
            <BrowserPane />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'terminal' ? 'flex' : 'none' }}>
            <TerminalPane />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'side' ? 'flex' : 'none' }}>
            <SideChat />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'tasks' ? 'flex' : 'none' }}>
            <TaskCenter />
          </div>
          <div className="dock-pane" style={{ display: bottomMode === 'skills' ? 'flex' : 'none' }}>
            <SkillPane />
          </div>
        </div>
      </div>
    </div>
  );
}
