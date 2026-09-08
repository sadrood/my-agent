/**
 * 内置终端面板（桥接回显式）：Agent（经 Python → Electron 桥）与用户共用
 * 同一条命令流——这里按时间序回显每条命令与输出（命令来自 Agent 或用户本人）。
 *
 * 说明：非 PTY——面向"看得见命令流、可补跑/复跑"，不支持 vim/htop 等交互程序；
 * 交互式程序请改用命令行窗口或后台任务（bg）。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Play, Trash2, Terminal as TerminalIcon } from 'lucide-react';

interface TermEntry {
  ts: number;
  from: 'user' | 'agent';
  command: string;
  ok: boolean;
  output: string;
}

function fmtTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function TerminalPane() {
  const [entries, setEntries] = useState<TermEntry[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [lastError, setLastError] = useState('');
  const bodyRef = useRef<HTMLDivElement>(null);

  // 挂载：取历史 + 订阅回显（Agent 与用户自己的命令都会经 terminal:echo 回来）
  useEffect(() => {
    void window.desktopApi?.terminalSnapshot?.()
      .then((list) => setEntries((list || []) as TermEntry[]))
      .catch(() => {});
    const cb = (entry: unknown) => {
      const e = entry as TermEntry;
      if (!e || typeof e.ts !== 'number') return;
      setEntries((prev) => [...prev.slice(-199), e]);
    };
    window.desktopApi?.onTerminalEcho?.(cb);
    return () => window.desktopApi?.offTerminalEcho?.();
  }, []);

  // 自动滚到底（新命令/输出到达时）
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [entries.length]);

  const run = async () => {
    const cmd = input.trim();
    if (!cmd || busy) return;
    setInput('');
    setBusy(true);
    setLastError('');
    try {
      const r = await window.desktopApi?.terminalUserRun?.(cmd);
      if (r && !r.ok) setLastError(r.error || '执行失败（exit != 0）');
    } catch (e) {
      setLastError(`调用终端失败：${String((e as Error)?.message || e).slice(0, 300)}`);
    } finally {
      setBusy(false);
    }
  };

  const clearAll = () => setEntries([]);

  return (
    <div className="term-pane">
      <div className="term-head">
        <TerminalIcon size={13} />
        <span className="term-title">内置终端{entries.length > 0 ? `（${entries.length} 条）` : ''}</span>
        <span style={{ flex: 1 }} />
        <button className="collapse-btn" title="清空回显" onClick={clearAll}><Trash2 size={12} /></button>
      </div>
      <div className="term-body" ref={bodyRef}>
        {lastError && (
          <div className="term-error">⚠️ {lastError}</div>
        )}
        {entries.length === 0 && (
          <div className="term-empty">暂无命令。Agent 执行 terminal 命令时会实时显示在这里；你也可以在下面输入框直接跑命令。</div>
        )}
        {entries.map((e, i) => (
          <div key={`${e.ts}-${i}`} className="term-entry">
            <div className={`term-cmd ${e.from === 'agent' ? 'agent' : 'user'}`}>
              <span className="term-from">{e.from === 'agent' ? 'Agent' : '你'}</span>
              <span className="term-ts">{fmtTime(e.ts)}</span>
              <span className="term-ps">$</span>
              <span className="term-text">{e.command}</span>
            </div>
            {e.output.trim() && (
              <div className={`term-output ${e.ok ? 'ok' : 'fail'}`}>
                {e.ok ? '' : '✗ '}{e.output}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="term-input-row">
        <span className="term-ps">$</span>
        <input
          className="term-input"
          placeholder="输入命令（与 Agent 共用同一终端，回车执行）"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); void run(); }
          }}
          disabled={busy}
        />
        <button className="btn primary" style={{ padding: '4px 12px' }} onClick={() => void run()} disabled={busy || !input.trim()}>
          <Play size={12} /> {busy ? '执行中…' : '执行'}
        </button>
      </div>
    </div>
  );
}
