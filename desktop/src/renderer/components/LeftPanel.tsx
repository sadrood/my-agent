/**
 * 左侧导航栏：项目（工作目录）→ 会话 两级。
 * - 顶部项目栏：显示当前项目名，点击展开项目切换菜单（切换项目 = 以该目录重启后端）。
 * - 会话归属于当前项目；「新对话」在**当前项目**下创建。
 * - 添加项目：原生目录选择器选择路径。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MessageSquare, Plus, Pin, Trash2, Settings, FolderOpen, ChevronDown, FolderPlus, X, Search } from 'lucide-react';
import { useSessionStore, useUIStore, getCurrentWorkspace } from '../store';
import { useProjectStore } from '../store/project';
import { deleteBackendSession, createNewSession, switchProject } from '../lib/backend';
import type { Project } from '../lib/types';

export function LeftPanel() {
  const { sessions, activeSessionId, selectSession, renameSession, togglePin, deleteSession } = useSessionStore();
  const { projects, activeProjectId, addProject, setActive, removeProject, getActive } = useProjectStore();
  const activeProject = getActive();
  const [query, setQuery] = useState('');
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState('');
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // 点击外部关闭项目菜单
  useEffect(() => {
    if (!projectMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setProjectMenuOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [projectMenuOpen]);

  // 显示有内容/置顶/当前激活的会话；纯空「新对话」默认隐藏（减少噪音），
  // 列表项上挂项目名徽章，标识归属。会话是用户的，不属于某个项目。
  const filtered = useMemo(
    () => sessions.filter(
      (s) =>
        (s.messageCount ?? 0) > 0 || s.pinned || s.id === activeSessionId,
    ).filter((s) => s.title.toLowerCase().includes(query.toLowerCase())),
    [sessions, query, activeSessionId],
  );
  /** 用项目 path 末段做短标签（如 D:\aaa\work\work\my_agent → my_agent） */
  const projectTag = (s: import('../lib/types').Session): string | undefined => {
    const p = s.workspace || '';
    if (!p) return undefined;
    const parts = p.split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] || undefined;
  };

  /** 行内相对时间（deepseek 式：刚刚/N 分钟前/N 小时前/昨天/MM-DD） */
  const relTime = (s: import('../lib/types').Session): string => {
    const t = Date.parse(s.updatedAt || s.createdAt || '');
    if (Number.isNaN(t)) return '';
    const min = Math.floor((Date.now() - t) / 60000);
    if (min < 1) return '刚刚';
    if (min < 60) return `${min} 分钟前`;
    const h = Math.floor(min / 60);
    if (h < 24) return `${h} 小时前`;
    if (h < 48) return '昨天';
    const d = new Date(t);
    return `${d.getMonth() + 1}-${d.getDate()}`;
  };

  /** 会话按更新时间分组（今天 / 昨天 / N 天前 / 更早） */
  const grouped = useMemo(() => {
    const now = Date.now();
    const day = 24 * 60 * 60 * 1000;
    const buckets: Record<string, import('../lib/types').Session[]> = {
      今天: [], 昨天: [], '近 7 天': [], '近 30 天': [], 更早: [],
    };
    for (const s of filtered) {
      const t = Date.parse(s.updatedAt || s.createdAt || '');
      const days = Number.isFinite(t) ? Math.floor((now - t) / day) : 99;
      if (days <= 0) buckets['今天'].push(s);
      else if (days <= 1) buckets['昨天'].push(s);
      else if (days <= 7) buckets['近 7 天'].push(s);
      else if (days <= 30) buckets['近 30 天'].push(s);
      else buckets['更早'].push(s);
    }
    return Object.entries(buckets).filter(([, arr]) => arr.length > 0);
  }, [filtered]);

  // 切换项目：激活 + 以该目录重启后端
  const switchTo = (p: Project) => {
    setProjectMenuOpen(false);
    setActive(p.id);
    void switchProject(p.path);
  };

  // 添加项目：目录选择器 → 激活 + 切换
  const addNewProject = async () => {
    const res = await window.desktopApi?.pickDirectory?.();
    const path = res?.path;
    if (!path) return;
    const p = addProject(path);
    setProjectMenuOpen(false);
    if (p) void switchProject(p.path);
  };

  // 删除项目（不删除会话数据，仅移出项目列表）
  const doRemoveProject = (id: string) => {
    setRemoving(id);
    removeProject(id);
    setTimeout(() => setRemoving(null), 100);
  };

  const renderItem = (s: import('../lib/types').Session) => (
    <div
      key={s.id}
      className={`session-item session-card ${s.id === activeSessionId ? 'active' : ''}`}
      onClick={() => selectSession(s.id)}
      onDoubleClick={() => { setRenamingId(s.id); setRenameText(s.title); }}
    >
      {renamingId === s.id ? (
        <input
          className="search-input"
          style={{ margin: 0, flex: 1 }}
          autoFocus
          value={renameText}
          onChange={(e) => setRenameText(e.target.value)}
          onBlur={() => { renameSession(s.id, renameText.trim() || s.title); setRenamingId(null); }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) { renameSession(s.id, renameText.trim() || s.title); setRenamingId(null); }
            if (e.key === 'Escape') setRenamingId(null);
          }}
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <div className="session-card-body">
          <div className="sc-top">
            <span className="title" title={s.title}>{s.title}</span>
            {/* 会话项目标签——不属于当前项目的会话显示归属项目，避免"切项目就消失" */}
            {projectTag(s) && projectTag(s) !== activeProject?.name && (
              <span className="session-project" title={`归属：${s.workspace}`}>{projectTag(s)}</span>
            )}
            {s.pinned && <Pin size={11} className="pin" />}
          </div>
          {/* 预览：最后一条消息片段（后端 /api/sessions 提供） */}
          <div className="sc-preview">{s.preview || '（暂无内容）'}</div>
          <div className="sc-meta">
            <span className="session-rel" title={s.updatedAt || s.createdAt || ''}>{relTime(s)}</span>
            <span className="sc-dot">·</span>
            <span>{s.messageCount} 条</span>
          </div>
        </div>
      )}
      {s.id === activeSessionId && (
        <span className="session-actions">
          <button className="collapse-btn" title="置顶" onClick={(e) => { e.stopPropagation(); togglePin(s.id); }}>
            <Pin size={12} />
          </button>
          <button className="collapse-btn" title="删除（同时清理后端对话记录）" onClick={(e) => { e.stopPropagation(); deleteSession(s.id); void deleteBackendSession(s.id); }}>
            <Trash2 size={12} />
          </button>
        </span>
      )}
    </div>
  );

  const { leftWidth } = useUIStore();

  return (
    <div className="panel-left" style={{ width: leftWidth, minWidth: leftWidth }}>
      {/* 项目栏 */}
      <div className="project-bar" ref={menuRef}>
        <button className="project-current" onClick={() => setProjectMenuOpen((v) => !v)}>
          <FolderOpen size={14} />
          <span className="pname">{activeProject?.name || '选择项目'}</span>
          <ChevronDown size={12} className="chevron" />
        </button>
        {projectMenuOpen && (
          <div className="project-menu">
            {projects.length === 0 && (
              <div style={{ padding: '8px 12px', fontSize: 12, color: 'var(--text-muted)' }}>还没有项目，点下方"添加项目"选一个目录。</div>
            )}
            {projects.map((p) => (
              <div key={p.id} className={`project-item ${p.id === activeProjectId ? 'active' : ''}`} onClick={() => switchTo(p)}>
                <FolderOpen size={13} />
                <span className="pname">{p.name}</span>
                <span className="ppath" title={p.path}>{p.path}</span>
                {p.id === activeProjectId && <span className="pcheck">✓</span>}
                {removing !== p.id && p.id !== activeProjectId && (
                  <button className="collapse-btn" title="移出项目" onClick={(e) => { e.stopPropagation(); doRemoveProject(p.id); }}><X size={11} /></button>
                )}
              </div>
            ))}
            <button className="btn" style={{ width: '100%', justifyContent: 'center', marginTop: 6 }} onClick={addNewProject}>
              <FolderPlus size={14} /> 添加项目
            </button>
          </div>
        )}
      </div>

      <div className="sidebar-section-title">
        会话
        <button className="collapse-btn" style={{ marginLeft: 'auto' }} title="搜索会话历史与文件（Ctrl+K）" onClick={() => window.dispatchEvent(new CustomEvent('open-search'))}>
          <Search size={13} />
        </button>
      </div>
      <div className="left-session-actions">
        <input
          className="search-input"
          placeholder="搜索会话…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          className="btn left-new-btn"
          title="新对话"
          onClick={() => void createNewSession()}
        >
          <Plus size={14} /> 新对话
        </button>
      </div>
      <div className="session-list">
        {grouped.map(([label, arr]) => (
          <div key={label} className="session-group">
            <div className="session-group-title">{label}</div>
            {arr.map(renderItem)}
          </div>
        ))}
        {filtered.length === 0 && (
          <div style={{ padding: 16, fontSize: 12.5, color: 'var(--text-muted)', textAlign: 'center' }}>
            {query ? '没有匹配的会话' : activeProject ? '暂无会话，点击"新对话"开始' : '请先选择或添加一个项目'}
          </div>
        )}
      </div>

      <div style={{ padding: '8px 12px 12px', borderTop: '1px solid var(--border)' }}>
        <button
          className="btn"
          style={{ width: '100%', justifyContent: 'center' }}
          title="打开配置（模型 / API Key / 工作目录）"
          onClick={() => window.dispatchEvent(new CustomEvent('open-settings'))}
        >
          <Settings size={14} /> 设置
        </button>
      </div>
    </div>
  );
}
