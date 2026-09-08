/**
 * 改动（Git Changes）—— 右栏「🔀 改动」页：
 * 列出工作区 git modified/untracked 文件，点击在文件预览中打开。
 * 数据源：/api/git（porcelain 解析，含分支与文件状态）。
 */
import React, { useCallback, useEffect, useState } from 'react';
import { GitBranch, RefreshCw, FileDiff, FolderOpen } from 'lucide-react';
import { fetchGit } from '../lib/backend';
import { useFileTabsStore } from '../store/fileTabs';
import { useUIStore } from '../store';

interface ChangeFile {
  path: string;
  state: string; // M / A / D / ?? (untracked) / R…
}

const STATE_META: Record<string, { label: string; cls: string }> = {
  M: { label: '修改', cls: 'm' },
  A: { label: '新增', cls: 'a' },
  D: { label: '删除', cls: 'd' },
  '??': { label: '未跟踪', cls: 'u' },
  R: { label: '重命名', cls: 'm' },
};

function base(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

export function GitChanges() {
  const [branch, setBranch] = useState('');
  const [files, setFiles] = useState<ChangeFile[]>([]);
  const [loading, setLoading] = useState(false);
  const openFile = useFileTabsStore((s) => s.openFile);

  const load = useCallback(async () => {
    setLoading(true);
    const g = await fetchGit();
    setBranch(g.branch);
    setFiles((g.files || []).map((f) => ({ path: f.path, state: String(f.state || '??') })));
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const open = (path: string) => {
    void openFile(path);
    useUIStore.getState().setRightTab(`file:${path}`);
  };

  const dir = (path: string) => {
    const i = path.lastIndexOf('/');
    return i > 0 ? path.slice(0, i) : '';
  };

  return (
    <div className="git-changes">
      <div className="gc-head">
        <span className="gc-title">
          <GitBranch size={11} /> {branch || '未检测到仓库'}
        </span>
        <button className="collapse-btn" title="刷新" onClick={() => void load()}><RefreshCw size={12} className={loading ? 'spin' : ''} /></button>
      </div>
      {files.length === 0 ? (
        <div className="gc-empty">
          <FileDiff size={18} />
          <span>{loading ? '读取中…' : '工作区干净，没有改动'}</span>
        </div>
      ) : (
        <div className="gc-list">
          {files.map((f) => {
            const meta = STATE_META[f.state] || { label: f.state, cls: 'm' };
            return (
              <button key={f.path} className="gc-item" onClick={() => open(f.path)} title={f.path}>
                <span className={`gc-badge ${meta.cls}`}>{meta.label}</span>
                <span className="gc-name" title={f.path}>{base(f.path)}</span>
                <span className="gc-dir">{dir(f.path) || '·'}</span>
              </button>
            );
          })}
          <div className="gc-count">共 {files.length} 处改动 · 点击行打开文件</div>
        </div>
      )}
      <span className="gc-hint"><FolderOpen size={10} /> 双击文件树同样可预览</span>
    </div>
  );
}
