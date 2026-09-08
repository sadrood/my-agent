/**
 * 工作区文件树（点击 → 中央列文件 Tab 打开；不再用弹层）。
 *
 * 数据源：后端 /api/files（绑定工作目录，不暴露系统盘）。
 * 联动：监听工具事件流——file/edit/patch 等写操作成功后自动刷新，
 * 并把被改动的文件高亮（橙色圆点）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Folder, RefreshCw, GitBranch } from 'lucide-react';
import { fetchFileTree, fetchGit, fileSearch, type FileTreeNode } from '../lib/backend';
import { FileTypeIcon, gitColorFor } from '../lib/fileIcon';
import { useBackendStore, useUIStore } from '../store';
import { useFileTabsStore } from '../store/fileTabs';

/** 会影响文件系统的工具（其成功结果触发树刷新） */
const FS_TOOLS = new Set(['file', 'edit', 'patch', 'python', 'terminal', 'image_gen']);

/** 从事件流提取「被成功修改的文件路径」集合 + 最近一次写操作事件计数（刷新信号） */
function useFileChanges(): { changed: Set<string>; writeTick: number } {
  const events = useBackendStore((s) => s.events);
  return useMemo(() => {
    const changed = new Set<string>();
    const pending: { tool: string; args: Record<string, any> }[] = [];
    let writeTick = 0;
    for (const e of events) {
      if (e.type === 'tool_call') {
        const args = e.data.args ?? e.data.input ?? {};
        pending.push({ tool: String(e.data.tool || ''), args: typeof args === 'object' ? args : {} });
      } else if (e.type === 'tool_result') {
        const tool = String(e.data.tool || '');
        const success = Boolean(e.data.success);
        // 配对最近一个同名 pending 调用
        for (let i = pending.length - 1; i >= 0; i--) {
          if (pending[i].tool === tool) {
            const call = pending.splice(i, 1)[0];
            if (success && FS_TOOLS.has(tool)) {
              writeTick++;
              // file 工具：write 操作才算修改；edit/patch 直接取路径
              if (tool === 'file') {
                const op = String(call.args.operation || '');
                const p = String(call.args.path || '');
                if (p && (op === 'write' || op === 'append')) changed.add(p.replace(/\\/g, '/'));
              } else if (tool === 'edit') {
                const p = String(call.args.file_path || '');
                if (p) changed.add(p.replace(/\\/g, '/'));
              }
            }
            break;
          }
        }
      }
    }
    return { changed, writeTick };
  }, [events]);
}

/** 路径是否命中变更集合（容忍 ./ 前缀与大小写差异） */
function isChanged(path: string, changed: Set<string>): boolean {
  if (changed.size === 0) return false;
  const norm = path.replace(/\\/g, '/').replace(/^\.\//, '').toLowerCase();
  for (const c of changed) {
    const cn = c.replace(/^\.\//, '').toLowerCase();
    if (cn === norm || cn.endsWith('/' + norm) || norm.endsWith('/' + cn)) return true;
  }
  return false;
}

/** git 状态查找（按相对路径，容忍反斜杠/前缀差异）。 */
function gitStateFor(path: string, gitFiles: { path: string; state: string }[]): string {
  const norm = path.replace(/\\/g, '/').replace(/^\.\//, '').toLowerCase();
  for (const f of gitFiles) {
    if (f.path.replace(/\\/g, '/').replace(/^\.\//, '').toLowerCase() === norm) return f.state;
  }
  return '';
}

function TreeNodeView({
  node, depth, changed, onOpen, gitFiles, selectedPath, onSelect,
}: {
  node: FileTreeNode; depth: number; changed: Set<string>; onOpen: (path: string) => void;
  gitFiles: { path: string; state: string }[]; selectedPath: string; onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(depth === 0);
  // 树线：每级缩进 + 竖线（终端风格层级感）
  const pad = { paddingLeft: 6 + depth * 14 };
  const dirOpen = (node: FileTreeNode) => (
    <div
      className={`tree-item tree-dir ${open ? 'open' : ''}`}
      style={pad}
      onClick={() => setOpen((v) => !v)}
    >
      <span className="tree-guide" style={{ width: 12 }} />
      {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
      <Folder size={12} style={{ color: 'var(--accent)', flexShrink: 0 }} />
      <span className="title">{node.name || node.path || '.'}</span>
    </div>
  );
  if (node.type === 'dir') {
    return (
      <div>
        {dirOpen(node)}
        {open && (node.children || []).map((c) => (
          <TreeNodeView key={c.path} node={c} depth={depth + 1} changed={changed}
                         onOpen={onOpen} gitFiles={gitFiles} selectedPath={selectedPath}
                         onSelect={onSelect} />
        ))}
      </div>
    );
  }
  const hot = isChanged(node.path, changed);
  const gstate = gitStateFor(node.path, gitFiles);
  const selected = selectedPath === node.path;
  return (
    <div
      className={`tree-item tree-file ${hot ? 'tree-changed' : ''} ${selected ? 'tree-selected' : ''}`}
      style={pad}
      title={`${node.path}${gstate ? `（git: ${gstate}）` : ''}${hot ? ' · 本次会话修改' : ''}`}
      onClick={() => { onSelect(node.path); onOpen(node.path); }}
    >
      <span className="tree-guide" style={{ width: 12 }} />
      <FileTypeIcon path={node.path} size={11} />
      <span className="title">{node.name}</span>
      {gstate && <span className="tree-git" style={{ color: gitColorFor(gstate) }}>{gstate}</span>}
      {hot && <span className="tree-dot" />}
    </div>
  );
}

export function FileTree() {
  const [data, setData] = useState<{ root: string; tree: FileTreeNode } | null>(null);
  const [selectedPath, setSelectedPath] = useState('');
  const openFile = useFileTabsStore((s) => s.openFile);
  const setRightTab = useUIStore((s) => s.setRightTab);
  const openInDock = (path: string) => {
    openFile(path);
    setRightTab(`file:${path}`);
  };
  const { changed, writeTick } = useFileChanges();
  const connected = useBackendStore((s) => s.connected);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [gitInfo, setGitInfo] = useState<{ branch: string; in_repo: boolean; files: { path: string; state: string }[] } | null>(null);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{ path: string; match: string; snippet?: string }[]>([]);
  const [searching, setSearching] = useState(false);

  // Git 分支/状态（连接 + 写操作后刷新）
  useEffect(() => { if (connected) void fetchGit().then(setGitInfo); }, [connected]);
  useEffect(() => {
    if (writeTick > 0) { const t = setTimeout(() => void fetchGit().then(setGitInfo), 500); return () => clearTimeout(t); }
  }, [writeTick]);

  const refresh = useCallback(async () => {
    const d = await fetchFileTree();
    if (d) setData(d);
  }, []);

  // 初次加载 + 后端重连后刷新
  useEffect(() => { if (connected) refresh(); }, [connected, refresh]);

  // 写操作成功后防抖刷新（避免一次任务里高频抖动）
  useEffect(() => {
    if (writeTick === 0) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(refresh, 600);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [writeTick, refresh]);

  const onSearch = async (q: string) => {
    setQuery(q);
    const term = q.trim();
    if (!term) { setSearching(false); setResults([]); return; }
    setSearching(true);
    setResults(await fileSearch(term));
  };

  const rootName = data ? data.root.split(/[\\/]/).filter(Boolean).pop() || data.root : '';
  const modifiedCount = gitInfo?.files.length ?? 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, flex: 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '0 8px 4px' }}>
        {gitInfo?.in_repo && (
          <span className="status-chip git-badge" title={`Git 分支 ${gitInfo.branch} · ${modifiedCount} 处改动`}>
            <GitBranch size={11} /> {gitInfo.branch || '(无分支)'}{modifiedCount > 0 ? ` · ${modifiedCount}` : ''}
          </span>
        )}
        <span style={{ flex: 1 }} />
        <button className="collapse-btn" title="刷新文件树" onClick={refresh}>
          <RefreshCw size={12} />
        </button>
      </div>
      <div style={{ padding: '0 8px 6px' }}>
        <input
          className="search-input"
          style={{ margin: 0 }}
          placeholder="搜索文件名/内容…"
          value={query}
          onChange={(e) => void onSearch(e.target.value)}
        />
      </div>
      <div className="session-list tree-list">
        {!data && <div style={{ padding: 12, fontSize: 12, color: 'var(--text-muted)' }}>
          {connected ? '加载中…' : '后端未连接'}
        </div>}
        {searching && (
          <>
            <div style={{ padding: '2px 10px 6px', fontSize: 11, color: 'var(--text-muted)' }}>
              文件匹配：{results.length} 条
            </div>
            {results.length === 0 && <div style={{ padding: 10, fontSize: 12, color: 'var(--text-muted)' }}>无匹配</div>}
            {results.map((f) => (
              <div key={f.path} className="tree-item tree-file" title={f.path} onClick={() => openInDock(f.path)}>
                <FileTypeIcon path={f.path} size={11} />
                <span className="title">{f.path}</span>
                <span className="si-tag">{f.match === 'filename' ? '文件名' : ''}</span>
              </div>
            ))}
          </>
        )}
        {!searching && data && (
          <>
            <div style={{ padding: '2px 10px 6px', fontSize: 11, color: 'var(--text-muted)' }} title={data.root}>
              📂 {rootName}
            </div>
            {(data.tree.children || []).map((c) => (
              <TreeNodeView key={c.path} node={c} depth={0} changed={changed}
                             onOpen={openInDock} gitFiles={gitInfo?.files || []}
                             selectedPath={selectedPath} onSelect={setSelectedPath} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}