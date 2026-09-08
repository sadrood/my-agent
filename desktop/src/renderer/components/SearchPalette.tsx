/**
 * 本地全文搜索浮层（Ctrl+K 打开）：搜会话历史 + 工作区文件。
 * 点会话 → 切换到该会话；点文件 → 右侧打开该文件。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Search, FileText, MessageSquare, X } from 'lucide-react';
import { searchLocal } from '../lib/backend';
import { useSessionStore, useUIStore } from '../store';
import { useFileTabsStore } from '../store/fileTabs';

interface SearchResult {
  conversations: { session_id: string; title: string; role: string; snippet: string }[];
  files: { path: string; match: string; snippet?: string }[];
}

export function SearchPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [q, setQ] = useState('');
  const [res, setRes] = useState<SearchResult>({ conversations: [], files: [] });
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (open) {
      setQ('');
      setRes({ conversations: [], files: [] });
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  // 防抖搜索
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    const term = q.trim();
    if (!term) { setRes({ conversations: [], files: [] }); return; }
    setLoading(true);
    timerRef.current = setTimeout(async () => {
      const r = await searchLocal(term, 'all');
      setRes(r);
      setLoading(false);
    }, 250);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [q]);

  if (!open) return null;

  const selectSession = (id: string) => {
    useSessionStore.getState().selectSession(id);
    onClose();
  };
  const openFile = (path: string) => {
    void useFileTabsStore.getState().openFile(path);
    useUIStore.getState().setRightTab(`file:${path}`);
    onClose();
  };

  return (
    <div className="search-mask" onClick={onClose}>
      <div className="search-palette" onClick={(e) => e.stopPropagation()}>
        <div className="search-input">
          <Search size={15} />
          <input
            ref={inputRef}
            value={q}
            placeholder="搜索会话历史与工作区文件…（Esc 关闭）"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') onClose(); }}
          />
          <button className="collapse-btn" onClick={onClose}><X size={13} /></button>
        </div>
        <div className="search-results">
          {loading && <div className="search-empty">搜索中…</div>}
          {!loading && q.trim() && res.conversations.length === 0 && res.files.length === 0 && (
            <div className="search-empty">无匹配结果</div>
          )}
          {res.conversations.length > 0 && (
            <>
              <div className="search-group">会话</div>
              {res.conversations.map((c) => (
                <button key={c.session_id} className="search-item" onClick={() => selectSession(c.session_id)}>
                  <MessageSquare size={13} />
                  <span className="si-title">{c.title || c.session_id}</span>
                  <span className="si-snippet">{c.snippet}</span>
                </button>
              ))}
            </>
          )}
          {res.files.length > 0 && (
            <>
              <div className="search-group">文件</div>
              {res.files.map((f) => (
                <button key={f.path} className="search-item" onClick={() => openFile(f.path)}>
                  <FileText size={13} />
                  <span className="si-title">{f.path}</span>
                  {f.snippet ? <span className="si-snippet">{f.snippet}</span> : <span className="si-tag">文件名</span>}
                </button>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
