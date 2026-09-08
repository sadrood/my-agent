/**
 * 文件标签栏 + 文件视图（标签页式）：
 * - FileTabBar：中央列顶部的标签条（💬 对话 + 已打开文件），点标签切换
 * - FileViewPane：只读代码视图（行号 + 复制 + 差异 Tab），随文件内容更新
 * 点击文件树中的文件 → useFileTabsStore.openFile → 中央列切到该文件 Tab。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Copy, FileText, MessagesSquare, X } from 'lucide-react';
import { fetchDiff, type FileDiff } from '../lib/backend';
import { copyTextSmart } from '../lib/clipboard';
import { useFileTabsStore, type OpenFileTab } from '../store/fileTabs';

export function basename(p: string): string {
  return p.split(/[\\/]/).filter(Boolean).pop() || p;
}

/** 行内 diff 渲染（绿增红减灰上下文）——从文件树弹层迁移而来 */
function DiffPane({ path }: { path: string }) {
  const [diff, setDiff] = useState<FileDiff | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    fetchDiff(path).then((d) => {
      if (!alive) return;
      setDiff(d);
      setLoading(false);
    });
    return () => { alive = false; };
  }, [path]);

  if (loading) return <pre className="file-view-body">加载中…</pre>;
  if (!diff?.ok) {
    return <pre className="file-view-body">{`⚠️ ${diff?.error || '无法生成 diff'}`}</pre>;
  }
  const lines = (diff.unified_diff || '').split('\n');
  return (
    <div className="file-view-body diff-body">
      <div className="diff-meta">
        <span className="diff-stat diff-add">+{diff.added ?? 0}</span>
        <span className="diff-stat diff-del">−{diff.removed ?? 0}</span>
        <span className="diff-meta-text">
          {diff.is_new ? '新建文件' : diff.deleted ? '已删除' : `由 ${diff.tool} 修改${(diff.writes ?? 1) > 1 ? `（累计 ${diff.writes} 次写入）` : ''}`}
        </span>
      </div>
      <pre className="diff-lines">
        {lines.map((line, i) => {
          let cls = 'diff-line diff-ctx';
          if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-line diff-head';
          else if (line.startsWith('@@')) cls = 'diff-line diff-hunk';
          else if (line.startsWith('+')) cls = 'diff-line diff-add';
          else if (line.startsWith('-')) cls = 'diff-line diff-del';
          return <div key={i} className={cls}>{line || ' '}</div>;
        })}
        {lines.length <= 1 && <div className="diff-line diff-ctx">（内容与快照一致，无差异）</div>}
      </pre>
    </div>
  );
}

/** 文件视图：只读代码（行号）+ 预览/差异切换 + 复制 */
export function FileViewPane({ tab }: { tab: OpenFileTab }) {
  const [mode, setMode] = useState<'preview' | 'diff'>('preview');
  const [copied, setCopied] = useState(false);
  const lines = useMemo(
    () => (tab.content ? tab.content.replace(/\n$/, '').split('\n') : []),
    [tab.content],
  );

  const copy = () => {
    // 文件内容逐字精确复制（不做空白规范化）
    void copyTextSmart(tab.content, { normalize: false }).then((ok) => {
      if (ok) {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }
    });
  };

  return (
    <div className="file-view-pane">
      <div className="file-view-head">
        <FileText size={12} style={{ flexShrink: 0 }} />
        <span className="path" title={tab.path}>{tab.path}</span>
        <div className="diff-tabs">
          <button className={`diff-tab ${mode === 'preview' ? 'active' : ''}`} onClick={() => setMode('preview')}>代码</button>
          <button className={`diff-tab ${mode === 'diff' ? 'active' : ''}`} onClick={() => setMode('diff')} title="与本次会话快照对比">差异</button>
        </div>
        <button className="code-copy" onClick={copy}>{copied ? '✓ 已复制' : '复制全文'}</button>
      </div>
      {tab.error ? (
        <pre className="file-view-body">⚠️ {tab.error}</pre>
      ) : mode === 'diff' ? (
        <DiffPane path={tab.path} />
      ) : (
        <div className="file-view-code">
          <div className="code-gutter">
            {lines.map((_, i) => <div key={i} className="code-ln">{i + 1}</div>)}
          </div>
          <pre className="code-content">{tab.content || '(空文件)'}</pre>
        </div>
      )}
      {tab.truncated && (
        <div className="file-view-note">文件过大，仅加载前 200KB</div>
      )}
    </div>
  );
}

/** 中央列顶部的标签条：对话 Tab + 已打开文件 Tabs（无文件时不渲染） */
export function FileTabBar() {
  const { files, activePath, setActive, closeFile } = useFileTabsStore();
  if (files.length === 0) return null;
  return (
    <div className="file-tabbar">
      <button
        className={`file-tab chat-tab ${activePath === null ? 'active' : ''}`}
        onClick={() => setActive(null)}
        title="返回对话"
      >
        <MessagesSquare size={12} /> 对话
      </button>
      {files.map((f) => (
        <button
          key={f.path}
          className={`file-tab ${activePath === f.path ? 'active' : ''}`}
          title={f.path}
          onClick={() => setActive(f.path)}
        >
          <FileText size={11} />
          <span className="name">{basename(f.path)}</span>
          <span
            className="x"
            title="关闭标签"
            onClick={(e) => { e.stopPropagation(); closeFile(f.path); }}
          ><X size={10} /></span>
        </button>
      ))}
    </div>
  );
}
