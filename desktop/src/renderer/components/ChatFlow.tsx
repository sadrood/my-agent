/**
 * 对话流：Markdown 渲染 + 代码高亮 + 特殊消息卡片。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, X, ListChecks, ShieldAlert, Copy, Sparkles, Brain, RotateCw, ThumbsUp, ThumbsDown } from 'lucide-react';
import type { ChatMessage } from '../lib/types';
import { sendApprovalResponse, fetchDiff, rollbackToCheckpoint, alwaysAllowTool, retryLastRun, type FileDiff } from '../lib/backend';
import { useTasksStore } from '../store/tasks';
import { copyTextSmart } from '../lib/clipboard';
import { useBackendStore, useSessionStore } from '../store';
import { useFileTabsStore } from '../store/fileTabs';
import { TurnNavigator } from './TurnNavigator';

/** 相对时间（欢迎屏卡片用）：刚刚 / N 分钟前 / N 小时前 / 昨天 / MM-DD */
function relativeTime(iso: string): string {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return '';
  const diff = Date.now() - t;
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  if (h < 48) return '昨天';
  const d = new Date(t);
  return `${d.getMonth() + 1}-${d.getDate()}`;
}

/** 欢迎屏（新会话空状态）：问候 + 快速开始 + 最近工作卡片网格 */
function WelcomeScreen() {
  const sessions = useSessionStore((s) => s.sessions);
  const selectSession = useSessionStore((s) => s.selectSession);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // 最近 6 个有内容的会话（排除当前空会话），按更新时间倒序
  const recent = sessions
    .filter((s) => s.id !== activeSessionId && (s.messageCount ?? 0) > 0)
    .slice(0, 6);

  return (
    <div className="welcome">
      <div className="welcome-hero">
        <Sparkles size={30} />
        <div className="welcome-title">开始一个新任务</div>
        <div className="welcome-sub">
          直接输入目标，我会自动规划并执行；支持 <code>/help</code> 查看全部命令
        </div>
      </div>
      {recent.length > 0 && (
        <div className="welcome-recent">
          <div className="welcome-recent-label">最近的工作</div>
          <div className="welcome-grid">
            {recent.map((s) => (
              <button key={s.id} className="welcome-card" onClick={() => selectSession(s.id)}>
                <div className="wc-title">{s.title || '未命名对话'}</div>
                <div className="wc-meta">
                  <span>{s.messageCount} 条</span>
                  <span className="wc-dot">·</span>
                  <span>{relativeTime(s.updatedAt)}</span>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** Diff 卡片：展示文件修改前后差异 + 接受/拒绝（本地确认，后端 diff 审批协议待扩展） */
export function DiffCard({ content }: { content: string }) {
  const [decision, setDecision] = useState<'accepted' | 'rejected' | null>(null);
  const lines = content.split('\n');
  if (decision) {
    return (
      <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 12.5, margin: '10px 0' }}>
        {decision === 'accepted' ? '✅ 已接受修改' : '🚫 已拒绝修改'}
      </div>
    );
  }
  return (
    <div className="card">
      <div className="card-header">
        <span className="status-tag pending">diff</span>
        文件修改
      </div>
      <div className="card-body">
        <div className="diff-view">
          {lines.map((ln, i) => {
            const sig = ln.startsWith('+') ? '+' : ln.startsWith('-') ? '-' : ' ';
            const cls = ln.startsWith('+') ? 'add' : ln.startsWith('-') ? 'del' : '';
            return (
              <div key={i} className={`diff-line ${cls}`}>
                <span className="ln">{i + 1}</span>
                <span className="sig">{sig}</span>
                <span>{ln.slice(1)}</span>
              </div>
            );
          })}
        </div>
      </div>
      <div className="diff-actions">
        <button className="btn success" onClick={() => setDecision('accepted')}><Check size={14} /> 接受</button>
        <button className="btn danger" onClick={() => setDecision('rejected')}><X size={14} /> 拒绝</button>
      </div>
    </div>
  );
}

/** 工具调用行：紧凑式单行（状态符号 + 工具名 + 参数摘要 + 耗时），点击展开/收起输出 */
export function ToolRow({ cmd, output, success, tool, durationMs, path, commit }: {
  cmd: string; output?: string; success?: boolean; tool?: string; durationMs?: number; path?: string; commit?: string;
}) {
  const [open, setOpen] = useState(false);
  const openFile = useFileTabsStore((s) => s.openFile);
  const canExpand = Boolean(output);
  const outputLines = output ? output.replace(/\n+$/, '').split('\n') : [];
  // 失败预览：压成一行（CLI 式「⎿ ✗ 原因」直显，不用点开才知道错在哪）
  const errPreview = output
    ? `${output.replace(/\s+/g, ' ').trim().slice(0, 240)}${output.replace(/\s+/g, ' ').trim().length > 240 ? '…' : ''}`
    : '';
  const glyph = success === undefined ? '▶' : success ? '✓' : '✗';
  const glyphCls = success === undefined ? 'running' : success ? 'success' : 'failed';
  const stateText = success === undefined ? '执行中' : success ? '' : '失败';

  // 真实 diff：文件写入成功后可从后端变更追踪器（/api/diff）拉取 unified diff
  const canDiff = success === true && Boolean(path);
  const [diff, setDiff] = useState<FileDiff | null>(null);
  const [diffState, setDiffState] = useState<'idle' | 'loading' | 'ready' | 'none'>('idle');
  const [diffError, setDiffError] = useState('');
  const toggleDiff = () => {
    if (diffState === 'ready') { setDiffState('idle'); setDiff(null); setDiffError(''); return; }
    if (diffState === 'loading' || !path) return;
    setDiffState('loading');
    setDiffError('');
    void fetchDiff(path).then((d) => {
      if (d.ok && d.unified_diff) { setDiff(d); setDiffState('ready'); }
      else { setDiffState('none'); setDiffError(String(d.error || '该文件无法生成 diff')); }
    }).catch(() => { setDiffState('none'); setDiffError('后端不可达'); });
  };

  // 回滚到此处：把工作区恢复到这个检查点（后端以新提交方式保存，不丢历史）
  const canRollback = success === true && Boolean(commit);
  const [rollbackState, setRollbackState] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const doRollback = () => {
    if (!commit || rollbackState === 'busy' || rollbackState === 'done') return;
    if (!window.confirm(
      `把工作区回滚到这个检查点？\n\n  ${cmd || tool || ''}\n\n此之后的修改会被撤销（以新提交方式保存，历史不会丢失，之后仍可回滚到更早的检查点）。`,
    )) return;
    setRollbackState('busy');
    void rollbackToCheckpoint(commit).then((r) => setRollbackState(r.ok ? 'done' : 'error'));
  };

  return (
    <div className="tool-row">
      <div
        className="tool-row-head"
        onClick={() => canExpand && setOpen((v) => !v)}
        title={canExpand ? '点击展开/收起输出' : undefined}
      >
        <span className={`tool-glyph ${glyphCls}`}>{glyph}</span>
        <span className="tool-name">
          {tool || 'tool'}
          {stateText && <span className="tool-state"> {stateText}</span>}
        </span>
        {path ? (
          // 文件名即链接：点击在右侧面板打开（与文件树同一条 openFile 通道）
          <span
            className="tool-filelink"
            title={`点击在右侧打开 ${path}`}
            onClick={(e) => { e.stopPropagation(); void openFile(path); }}
          >
            {path}
          </span>
        ) : (cmd && <span className="tool-arg">{cmd}</span>)}
        {durationMs !== undefined && (
          <span className="tool-duration">{durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`}</span>
        )}
        {canDiff && (
          <button
            className="tool-diff-btn"
            onClick={(e) => { e.stopPropagation(); toggleDiff(); }}
          >
            {diffState === 'loading' ? 'diff…' : diffState === 'ready' ? '收起 diff' : '查看 diff'}
          </button>
        )}
        {canRollback && (
          <button
            className={`tool-diff-btn ${rollbackState === 'error' ? 'tool-rollback-error' : ''}`}
            title="把工作区恢复到这个检查点"
            onClick={(e) => { e.stopPropagation(); doRollback(); }}
          >
            {rollbackState === 'idle' ? '回滚到此处'
              : rollbackState === 'busy' ? '回滚中…'
                : rollbackState === 'done' ? '✓ 已回滚' : '回滚失败'}
          </button>
        )}
      </div>
      {open && canExpand && <div className="cmd-output">{output}</div>}
      {!open && canExpand && outputLines.length > 0 && (
        // 单行概要：失败行直显错误预览（红），成功行只显行数提示——信息量与 CLI 一致
        success === false ? (
          <div className="tool-row-error" onClick={() => setOpen(true)} title="点击查看完整错误">
            <span className="err-mark">⎿ ✗</span> {errPreview}
          </div>
        ) : (
          <div className="tool-row-count" onClick={() => setOpen(true)} title="点击查看完整输出">
            <span>{outputLines.length} 行</span>
          </div>
        )
      )}
      {diffState === 'none' && diffError && (
        <div className="tool-diff-error" title={diffError}>⚠️ {diffError}</div>
      )}
      {diffState === 'ready' && diff?.unified_diff && (
        <div className="diff-lines tool-diff">
          {diff.unified_diff.split('\n').map((ln, i) => (
            <div key={i} className={
              ln.startsWith('+++') || ln.startsWith('---') ? 'diff-line diff-head'
                : ln.startsWith('@@') ? 'diff-line diff-hunk'
                  : ln.startsWith('+') ? 'diff-line diff-add'
                    : ln.startsWith('-') ? 'diff-line diff-del'
                      : 'diff-line diff-ctx'
            }>{ln || ' '}</div>
          ))}
        </div>
      )}
      {diffState === 'none' && <div className="tool-diff-empty">没有可用的变更记录（可能不是本轮运行中的修改）</div>}
    </div>
  );
}

/** 应用记录卡片（产出高亮）：Agent 写文件后用突出样式展示路径+耗时+输出 */
function FileOutputCard({ path, cmd, tool, durationMs, output }: {
  path: string; cmd?: string; tool?: string; durationMs?: number; output?: string;
}) {
  const [open, setOpen] = useState(false);
  const openFile = useFileTabsStore((s) => s.openFile);
  const canExpand = Boolean(output && output.trim());
  return (
    <div className="file-output-card">
      <div className="fo-head" onClick={() => canExpand && setOpen((v) => !v)}>
        <span className="fo-glyph"><Check size={12} /></span>
        <span className="fo-title">已生成文件</span>
        <span className="fo-tool">{tool}</span>
        <span
          className="fo-path fo-path-link"
          title={`点击在右侧打开 ${path}`}
          onClick={(e) => { e.stopPropagation(); void openFile(path); }}
        >{path}</span>
        {durationMs !== undefined && (
          <span className="tool-duration">{durationMs < 1000 ? `${durationMs}ms` : `${(durationMs / 1000).toFixed(1)}s`}</span>
        )}
      </div>
      {cmd && <div className="fo-cmd">{cmd}</div>}
      {open && canExpand && <div className="cmd-output">{output}</div>}
    </div>
  );
}

/** 任务步骤卡片：多步骤进度列表 */
export function StepsCard({ steps, current }: { steps: string[]; current: number }) {
  return (
    <div className="card">
      <div className="card-header">
        <ListChecks size={13} />
        任务步骤
      </div>
      <div className="card-body">
        <ul className="steps-list">
          {steps.map((s, i) => (
            <li key={i} className="step-item">
              <span className={`step-dot ${i < current ? 'done' : i === current ? 'active' : 'todo'}`}>
                {i < current ? '✓' : i + 1}
              </span>
              <span style={{ color: i <= current ? 'var(--text-primary)' : 'var(--text-muted)' }}>{s}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** 待办清单卡：模型用 todo_write 维护的会话级任务列表（✓ 完成 / ▶ 进行中 / ○ 待办） */
export function TodoCard({ todos }: { todos: { id: string; title: string; status: string }[] }) {
  if (!todos.length) return null;
  return (
    <div className="todo-card">
      <div className="todo-head"><ListChecks size={13} /> 待办清单</div>
      <ul className="todo-list">
        {todos.map((t) => (
          <li key={t.id} className={`todo-item ${t.status || 'todo'}`}>
            <span className="todo-mark">{t.status === 'done' ? '✓' : t.status === 'in_progress' ? '▶' : '○'}</span>
            <span className="todo-title">{t.title}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 权限确认卡片：AI 请求执行受限操作 */
export function ApprovalCard({ tool, command, risk, onResolve, onAlways }: {
  tool: string; command: string; risk: string;
  onResolve: (allow: boolean) => void;
  onAlways?: () => void;
}) {
  return (
    <div className="card approval-card">
      <div className="card-header">
        <ShieldAlert size={13} />
        <span className="status-tag pending">需要确认</span>
        {tool} · 风险: {risk}
      </div>
      <div className="card-body">
        <div className="cmd-cmd">$ {command}</div>
      </div>
      <div className="approval-actions">
        <button className="btn success" onClick={() => onResolve(true)}>允许</button>
        {onAlways && tool !== 'tool' && (
          <button className="btn" title="本次应用运行期间，该工具的后续请求不再询问" onClick={onAlways}>
            总是允许 {tool}
          </button>
        )}
        <button className="btn danger" onClick={() => onResolve(false)}>拒绝</button>
      </div>
    </div>
  );
}

/** 从 React 元素树里提取纯文本（复制/行数统计用，高亮 span 不影响；导出供测试） */
export function extractText(node: unknown): string {
  if (node == null) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join('');
  const props = (node as { props?: { children?: unknown } }).props;
  if (props?.children !== undefined) return extractText(props.children);
  return '';
}

/** 代码块：语言标签 + 行数 + 复制 + 超长折叠（阅读体验优化） */
function CodeBlock(props: { children?: React.ReactNode }) {
  const child = Array.isArray(props.children) ? props.children[0] : props.children;
  const cls = ((child as { props?: { className?: string } })?.props?.className) || '';
  const langMatch = /language-([\w+-]+)/.exec(cls);
  const lang = langMatch ? langMatch[1] : 'code';
  const raw = extractText(props.children).replace(/\n$/, '');
  const lineCount = raw ? raw.split('\n').length : 0;
  const LONG = 60;
  const tooLong = lineCount > LONG;
  const [expanded, setExpanded] = useState(!tooLong);
  const [copied, setCopied] = useState(false);

  const copy = () => {
    // 代码逐字精确复制（不做空白规范化）
    void copyTextSmart(raw, { normalize: false }).then((ok) => {
      if (ok) {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }
    });
  };

  return (
    <div className="code-block">
      <div className="code-block-head">
        <span className="code-lang">{lang}</span>
        <span className="code-lines">{lineCount} 行</span>
        {tooLong && (
          <button className="code-toggle" onClick={() => setExpanded((v) => !v)}>
            {expanded ? '折叠' : '展开'}
          </button>
        )}
        <button className="code-copy" onClick={copy}>
          {copied ? '✓ 已复制' : '复制'}
        </button>
      </div>
      <pre className={tooLong && !expanded ? 'code-collapsed' : ''}>{props.children}</pre>
      {tooLong && !expanded && (
        <button className="code-expand-more" onClick={() => setExpanded(true)}>
          展开全部 {lineCount} 行 ↓
        </button>
      )}
    </div>
  );
}

/** Markdown 渲染（统一挂代码块增强） */
function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{ pre: CodeBlock }}
    >{text}</ReactMarkdown>
  );
}

/** 思考行（借鉴主流实现）：独立可折叠、带摘要行。
 *  默认收起（含运行中）：只显示单行摘要，用户手动展开看全文；
 *  运行中摘要行实时显示最新思考。用户手动开合优先。 */
function ReasoningDisclosure({ reasoning, running }: { reasoning: string; running?: boolean }) {
  const [manual, setManual] = useState<boolean | null>(null);
  const open = manual ?? false;
  const trimmed = reasoning.trimEnd();
  const lines = trimmed ? trimmed.split('\n') : [];
  const summary = running ? lines[lines.length - 1] || '' : lines[0] || '';
  return (
    <details
      className="reasoning-disclosure"
      open={open}
      onToggle={(e) => setManual((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        <Brain size={12} className="rd-icon" />
        <span className="rd-label">思考</span>
        <span className="rd-summary">{summary}</span>
      </summary>
      <div className="rd-body">{reasoning}</div>
    </details>
  );
}

/** 单条消息（memo 化：流式期间只重渲染"正在变长"的那一条，避免全列表重渲染卡顿） */
function MessageBase({ msg, runningHere, sessionId, hideReasoning }: {
  msg: ChatMessage; runningHere?: boolean; sessionId?: string | null;
  /** 回合结束后：思考内容收进「已工作」，正文旁不再显示思考行 */
  hideReasoning?: boolean;
}) {
  // 按 meta.kind 分发特殊卡片
  if (msg.kind === 'diff') return <DiffCard content={msg.content} />;
  if (msg.kind === 'command') {
    const meta = (msg.meta || {}) as { cmd?: string; success?: boolean; tool?: string; durationMs?: number; path?: string; commit?: string };
    // 文件产出场景：工具产出文件路径 + 成功 → 用专门的应用记录卡片（产出高亮）
    // 比通用 ToolRow 更突出，让 Agent 实际产出"可见"
    if (meta.path && meta.tool && /^(file|edit|patch|image_gen)$/.test(meta.tool) && meta.success === true) {
      return <FileOutputCard path={meta.path} cmd={meta.cmd} tool={meta.tool} durationMs={meta.durationMs} output={msg.content} />;
    }
    return <ToolRow cmd={meta.cmd || ''} output={msg.content} success={meta.success} tool={meta.tool} durationMs={meta.durationMs} path={meta.path} commit={meta.commit} />;
  }
  if (msg.kind === 'steps') {
    const meta = (msg.meta || {}) as { steps?: string[]; current?: number };
    return <StepsCard steps={meta.steps || []} current={meta.current || 0} />;
  }
  if (msg.kind === 'approval') {
    const meta = (msg.meta || {}) as { tool?: string; command?: string; risk?: string; id?: string };
    return (
      <ApprovalCard
        tool={meta.tool || 'tool'}
        command={meta.command || msg.content}
        risk={meta.risk || 'medium'}
        onResolve={(allow) => { void sendApprovalResponse(meta.id, allow); }}
        onAlways={() => { alwaysAllowTool(meta.tool || '', meta.id); }}
      />
    );
  }
  if (msg.kind === 'todo') {
    return <TodoCard todos={((msg.meta || {}).todos || []) as { id: string; title: string; status: string }[]} />;
  }
  if (msg.role === 'user') {
    return (
      <div className="msg user" data-mid={msg.id}>
        <div className="msg-avatar">你</div>
        <div className="msg-body">
          <div className="markdown"><Markdown text={msg.content} /></div>
        </div>
        <CopyButton text={msg.content} />
      </div>
    );
  }
  if (msg.role === 'tool') {
    // tool 结果：紧凑展示，不占主视觉
    return (
      <div className="msg assistant">
        <div className="msg-avatar" style={{ background: 'transparent', border: 'none', fontSize: 11 }}>⎿</div>
        <div className="msg-body">
          <div className="cmd-output">{msg.content}</div>
        </div>
      </div>
    );
  }
  if (msg.role === 'system') {
    // 历史数据里的成功收尾提示不再展示（成功本身即结果）
    if (msg.content === '✅ 任务完成') return null;
    // 系统消息（错误/完成提示）弱化展示；失败时带「重试」续跑按钮
    const meta = (msg.meta || {}) as { retryable?: boolean; goal?: string; runStatus?: string };
    if (meta.retryable && meta.goal && sessionId) {
      return (
        <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 12.5, margin: '10px 0' }}>
          <div>{msg.content}</div>
          <div style={{ marginTop: 8 }}>
            <button className="btn" onClick={() => retryLastRun(sessionId, meta.goal!)}>
              <RotateCw size={13} /> 重试（续跑）
            </button>
          </div>
        </div>
      );
    }
    return (
      <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 12.5, margin: '10px 0' }}>
        {msg.content}
      </div>
    );
  }
  if (msg.role === 'assistant' || (msg.role !== 'user' && msg.role !== 'tool' && msg.role !== 'system')) {
    return (
      <div className="msg assistant">
        <div className="msg-avatar">悟</div>
        <div className="msg-body">
          {msg.reasoning && !hideReasoning ? (
            <ReasoningDisclosure reasoning={msg.reasoning} running={runningHere} />
          ) : null}
          <div className="markdown"><Markdown text={msg.content} /></div>
        </div>
        <div className="msg-side">
          <CopyButton text={msg.content} />
          <FeedbackButtons msg={msg} />
        </div>
      </div>
    );
  }
  return null;
}

/** 悬停复制按钮：复制整条回答原文（规范化空白 + 剪贴板兜底） */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  if (!text.trim()) return null;
  return (
    <button
      className="msg-copy"
      title={failed ? '复制失败（剪贴板被拒绝）' : '复制回答'}
      onClick={() => {
        void copyTextSmart(text).then((ok) => {
          if (ok) {
            setCopied(true);
            setFailed(false);
            setTimeout(() => setCopied(false), 1200);
          } else {
            setFailed(true);
            setTimeout(() => setFailed(false), 2000);
          }
        });
      }}
    >
      {failed ? <X size={13} /> : copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}

/** 消息反馈（👍👎 评级，存后端 memory/feedback.json） */
function FeedbackButtons({ msg }: { msg: ChatMessage }) {
  const [state, setState] = useState<'' | 'up' | 'down'>('');
  const rateMessage = useTasksStore((s) => s.rateMessage);
  const fb = (rating: 'up' | 'down') => {
    if (state === rating) { setState(''); return; }
    setState(rating);
    void rateMessage(msg.id, rating);
  };
  return (
    <div className="msg-feedback">
      <button className={`fb-btn ${state === 'up' ? 'active' : ''}`} title="有用" onClick={() => fb('up')}><ThumbsUp size={12} /></button>
      <button className={`fb-btn ${state === 'down' ? 'active' : ''}`} title="没用" onClick={() => fb('down')}><ThumbsDown size={12} /></button>
    </div>
  );
}

/** 长会话懒渲染：一次只解析最近 N 条 markdown，更早的按需加载 */
const PAGE_SIZE = 50;

/** 对话流容器：智能跟随滚动（用户上翻阅读时不抢滚动条）+ 懒渲染 + 思考中占位 */
export function ChatFlow({ messages, sessionId }: { messages: ChatMessage[]; sessionId?: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const running = useBackendStore((s) => s.running);
  const runningSessionId = useBackendStore((s) => s.runningSessionId);
  // 多会话隔离：运行态只显示在发起任务的那个会话里（归属未知时不显示，避免处处可见）
  const runningHere = running && Boolean(sessionId) && runningSessionId === sessionId;

  // 引擎无关的收尾判定（逐回合）：
  // 一个回合"收尾" = 它自己的最后一条消息是 ✅/❌/⏹ 系统收尾（任务完成标记），
  // 且该会话当前没有正在运行的任务。只有收尾的回合才收进「已工作」——
  // 任务没完成（没有收尾消息 / 运行中）时，回合始终保持在「工作中」展开态。
  const isTerminalMsg = (m?: ChatMessage): boolean => {
    if (!m || m.role !== 'system') return false;
    const t = (m.content || '').trim();
    return /^(✅|❌|⏹|⚠️)/.test(t) || Boolean((m.meta as { runStatus?: string } | undefined)?.runStatus);
  };
  // runningHere：会话整体在跑 → 所有回合展开（含上一回合，供对照）；
  // 空闲时逐回合按自己的收尾消息判定（不再依赖"全局最后一条"，历史回放/孤立消息不会挡住折叠）
  const turnEnded = (turn: ChatMessage[]): boolean =>
    !runningHere && turn.length > 0 && isTerminalMsg(turn[turn.length - 1]);
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const loadingMoreRef = useRef(false);      // 顶部自动加载防重入
  const prevScrollHRef = useRef(0);          // 加载前 scrollHeight（滚动补偿用）
  const shown = messages.length > visibleCount ? messages.slice(-visibleCount) : messages;
  const hiddenCount = messages.length - shown.length;
  // 回合分组缓存：仅在渲染窗口变化时重算（长会话滚动不再每次重分组）
  const turnsCache = useMemo(() => buildTurns(shown), [shown]);


  // 切会话时重置滚动锚定与懒加载页数，避免上个会话的状态泄漏到新会话
  useEffect(() => {
    setPinnedToBottom(true);
    setVisibleCount(PAGE_SIZE);
  }, [sessionId]);

  // 关键：依赖最后一条消息的"内容+思考"总长度——流式回答是同一条消息变长，
  // 只看 messages.length 不会触发滚动，聊天区就会"卡"在旧位置。
  const last = messages[messages.length - 1];
  const lastLen = last ? last.content.length + (last.reasoning?.length || 0) : 0;
  useEffect(() => {
    const el = ref.current;
    if (el && pinnedToBottom) el.scrollTo({ top: el.scrollHeight });
  }, [messages.length, lastLen, pinnedToBottom]);

  const handleScroll = () => {
    const el = ref.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    // 轮次导航程序化跳转期间（jumpLock）：从底部跳走时滚动刚起步仍距底 <80px，
    // 若此刻重新吸底会把跳转动画抢回底部 —— 锁内只允许「离开底部」取消防守，
    // 不允许「回到底部」重新吸底
    if ((el as HTMLElement).dataset.jumpLock) {
      if (!atBottom) setPinnedToBottom(false);
      return;
    }
    setPinnedToBottom(atBottom);
    // 静默加载：滚到接近顶部且还有更早消息 → 自动补一页（防重入）
    if (el.scrollTop < 80 && hiddenCount > 0 && !loadingMoreRef.current) {
      loadingMoreRef.current = true;
      prevScrollHRef.current = el.scrollHeight;
      setVisibleCount((v) => v + PAGE_SIZE);
    }
  };

  // 顶部插入更早消息后补偿滚动位置：保持当前阅读行不跳动
  useEffect(() => {
    if (!loadingMoreRef.current) return;
    const el = ref.current;
    loadingMoreRef.current = false;
    if (el && prevScrollHRef.current) {
      const delta = el.scrollHeight - prevScrollHRef.current;
      prevScrollHRef.current = 0;
      if (delta > 0) el.scrollTop += delta;
    }
  }, [visibleCount]);

  /** 手动「加载更早」（按钮与静默自动加载共用同一补偿路径） */
  const loadEarlier = () => {
    const el = ref.current;
    if (!el || loadingMoreRef.current || hiddenCount <= 0) return;
    loadingMoreRef.current = true;
    prevScrollHRef.current = el.scrollHeight;
    setVisibleCount((v) => v + PAGE_SIZE);
  };

  const jumpToBottom = () => {
    setPinnedToBottom(true);
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  };

  // 轮次导航点到的提问在懒加载窗口外时：从该消息起扩窗
  const ensureShownFrom = (msgIndex: number) => {
    setPinnedToBottom(false);
    setVisibleCount((v) => Math.max(v, messages.length - msgIndex + 12));
  };

  return (
    <div className="chat-flow-wrap">
      <div className="chat-flow" ref={ref} onScroll={handleScroll}>
        {/* 「加载更早」是文档流元素，平铺在内容顶部（不悬浮遮挡正文）；
            滚到顶部时也会静默自动加载，按钮只是给用户一个明确入口 */}
        {hiddenCount > 0 && (
          <button className="load-earlier" onClick={loadEarlier}>
            加载更早的消息（还有 {hiddenCount} 条）
          </button>
        )}
        {messages.length === 0 && !runningHere && <WelcomeScreen />}
        {turnsCache.map((turn, i) => {
          // 运行期间：所有回合保持展开（会话整体处于「工作中」，
          // 上一轮的思考/说明/工具也在眼前，供对照过程）；
          // 整个会话运行结束后，各回合才统一收成「已工作」折叠
          return <TurnGroup key={turn[0]?.id || ('t' + i)} turn={turn} running={!turnEnded(turn)} sessionId={sessionId} />;
        })}
        {runningHere && (
          <div className="thinking">
            <span className="thinking-dot" />
            <span>思考中…</span>
          </div>
        )}
      </div>
      {messages.length > 0 && (
        <TurnNavigator messages={messages} scrollerRef={ref} runningHere={runningHere} onNeedMore={ensureShownFrom} />
      )}
      {!pinnedToBottom && (
        <button className="jump-bottom" onClick={jumpToBottom}>↓ 回到底部</button>
      )}
    </div>
  );
}

/** 单条消息组件（导出供辅助 Agent 面板复用渲染工具卡/思考块/消息流） */
export const Message = React.memo(MessageBase);

/* ------------------------------------------------------------------ */
/* 回合分组：「已工作」折叠                                      */
/* ------------------------------------------------------------------ */

/** 过程性消息：工具调用/输出/步骤等 —— 结束后的回合会收进「已工作」 */
function isProcessMsg(m: ChatMessage): boolean {
  if (m.role === 'tool') return true;
  if (m.kind === 'command' || m.kind === 'steps') return true;
  return false;
}

/** 一次工具工作项：调用（command/其它）与其输出（role tool）合并为一行（紧凑呈现） */
interface WorkItem {
  call?: ChatMessage;
  out?: ChatMessage;
}

function collectWork(msgs: ChatMessage[]): WorkItem[] {
  const items: WorkItem[] = [];
  let pending: WorkItem | null = null;
  for (const m of msgs) {
    if (!isProcessMsg(m)) continue;
    if (m.role === 'tool') {
      // 输出：并入前一个未配对的调用（若已配对则独立成行）
      if (pending && !pending.out) { pending.out = m; pending = null; }
      else { items.push({ out: m }); pending = null; }
    } else if (m.kind === 'command' || m.kind === 'steps') {
      if (pending) items.push(pending);
      pending = { call: m };
    }
  }
  if (pending) items.push(pending);
  return items;
}

function isFileOutput(call?: ChatMessage): boolean {
  if (!call) return false;
  const meta = (call.meta || {}) as { tool?: string; success?: boolean };
  return Boolean(call.kind === 'command' && meta.tool && /^(file|edit|patch|image_gen)$/.test(meta.tool) && meta.success !== false);
}

/** 渲染一个工作项 = 单行工具卡（图标+工具+命令摘要+状态+耗时，输出收起可展开） */
function renderWorkItem(item: WorkItem, sessionId?: string | null) {
  const call = item.call;
  const out = item.out;
  if (call) {
    const cMeta = (call.meta || {}) as { cmd?: string; tool?: string; success?: boolean; durationMs?: number; path?: string; commit?: string };
    const oMeta = (out?.meta || {}) as { cmd?: string; success?: boolean; durationMs?: number };
    const output = out?.content || call.content || undefined;
    const success = oMeta.success !== undefined ? oMeta.success : cMeta.success;
    const durationMs = oMeta.durationMs ?? cMeta.durationMs;
    // 命令摘要：call 缺时用 result 事件补的 cmd（同一工具行的参数不丢）
    const cmd = cMeta.cmd || oMeta.cmd || '';
    if (isFileOutput(call)) {
      return (
        <FileOutputCard
          key={call.id}
          path={cMeta.path || ''}
          cmd={cmd}
          tool={cMeta.tool}
          durationMs={durationMs}
          output={output}
        />
      );
    }
    return (
      <ToolRow
        key={call.id}
        cmd={cmd}
        output={output}
        success={success}
        tool={cMeta.tool}
        durationMs={durationMs}
        path={cMeta.path}
        commit={cMeta.commit}
      />
    );
  }
  if (out) {
    // 孤立输出（无配对调用）：不显示「xxx 输出」伪标题行——行首只留工具名+命令摘要，
    // 内容点击展开（避免一长列 "terminal 输出/task 输出" 标签墙）
    const oMeta = (out.meta || {}) as { cmd?: string; success?: boolean; tool?: string; durationMs?: number };
    return (
      <ToolRow
        key={out.id}
        cmd={oMeta.cmd || ''}
        output={out.content || ''}
        success={oMeta.success}
        tool={oMeta.tool}
        durationMs={oMeta.durationMs}
      />
    );
  }
  return null;
}

/** 以用户提问为界切分回合（首段可能是不完整的上半轮，正常展示） */
function buildTurns(msgs: ChatMessage[]): ChatMessage[][] {
  const turns: ChatMessage[][] = [];
  for (const m of msgs) {
    if (m.role === 'user' && turns.length > 0 && turns[turns.length - 1].some((x) => x.role !== 'user')) {
      turns.push([m]);
    } else if (m.role === 'user' && turns.length > 0 && turns[turns.length - 1][0].role === 'user') {
      // 连续多条 user（如排队指令）：并入同轮
      turns[turns.length - 1].push(m);
    } else if (m.role === 'user') {
      turns.push([m]);
    } else if (turns.length === 0) {
      turns.push([m]); // 窗口从历史中部开始：首段无提问
    } else {
      turns[turns.length - 1].push(m);
    }
  }
  return turns;
}

/**
 * 单个回合：按时间顺序交错呈现「正文 ↔ 已工作」；
 * 运行中的回合：过程实时按序展开（先干后答，不沉底）；
 * 已结束的回合：过程消息与思考收进一个「已工作」折叠块，
 * 折叠块插在**第一个过程发生的位置**（通常在最终回答之前）。
 */
function TurnGroup({ turn, running, sessionId }: {
  turn: ChatMessage[];
  running: boolean;
  sessionId?: string | null;
}) {
  const userMsg = turn.find((m) => m.role === 'user');
  const texts = turn.filter((m) => (m.role === 'assistant' || m.role === 'system') && !isProcessMsg(m) && (m.content || '').trim());
  const process = turn.filter((m) => isProcessMsg(m));
  const reasonings = turn
    .filter((m) => m.role === 'assistant' && m.reasoning && !(m.content || '').trim())
    .map((m) => m.reasoning || '');
  const hasReasoningOnText = turn.some((m) => m.role === 'assistant' && m.reasoning && (m.content || '').trim());
  const keyBase = userMsg?.id || turn[0]?.id || 't';

  // —— 工作中（running=true）：任务未完成，禁止出现「已工作」——
  // 按真实时间序实时展开：提问 → 思考/自言自语/工具行交错铺开 → 流式回答。
  // 工具调用与输出合并为紧凑单行（ToolRow），和结束后的折叠体同一渲染路径。
  if (running) {
    const liveNodes: React.ReactNode[] = [];
    let pendingCall: ChatMessage | null = null;
    const flush = () => {
      if (pendingCall) {
        liveNodes.push(renderWorkItem({ call: pendingCall }, sessionId));
        pendingCall = null;
      }
    };
    for (const m of turn) {
      if (m.role === 'user') {
        flush();
        liveNodes.push(<Message key={m.id} msg={m} runningHere sessionId={sessionId} />);
        continue;
      }
      if (isProcessMsg(m)) {
        if (m.role === 'tool') {
          // 输出：并入前一个未配对的调用（若已配对则独立成行）
          liveNodes.push(renderWorkItem({ call: pendingCall ?? undefined, out: m }, sessionId));
          pendingCall = null;
        } else {
          flush();
          pendingCall = m;
        }
        continue;
      }
      flush();
      if (m.role === 'assistant' && m.reasoning && !(m.content || '').trim()) {
        // 纯思考流消息：行内弱化展示（正文同条消息时走 Message 的思考块）
        liveNodes.push(<div key={`r-${m.id}`} className="wf-reason-text">{m.reasoning}</div>);
      } else {
        liveNodes.push(<Message key={m.id} msg={m} runningHere sessionId={sessionId} />);
      }
    }
    flush();
    return (
      <div className="turn-group" data-turn={keyBase}>
        {liveNodes}
      </div>
    );
  }

  const foldable = process.length > 0 || reasonings.length > 0 || hasReasoningOnText;
  if (!foldable) {
    return (
      <div className="turn-group" data-turn={keyBase}>
        {turn.map((m) => <Message key={m.id} msg={m} runningHere={false} sessionId={sessionId} />)}
      </div>
    );
  }

  // 已结束：正文只保留【最终回答】（回合尾部连续正文）。
  // 在此之前的一切——自言自语文本 / 思考 / 工具调用——按真实发生顺序收进「已工作」。
  let lastWork = -1;
  for (let i = 0; i < turn.length; i++) {
    const m = turn[i];
    if (isProcessMsg(m) || (m.role === 'assistant' && m.reasoning && !(m.content || '').trim())) lastWork = i;
  }
  const foldMsgs = lastWork >= 0 ? turn.slice(1, lastWork + 1) : [];
  const tailMsgs = lastWork >= 0 ? turn.slice(lastWork + 1) : turn.slice(1);

  // 折叠体：严格时间序（思考行 ↔ 自言自语 ↔ 工具行交错）
  const bodyNodes: React.ReactNode[] = [];
  let pendingCall: ChatMessage | null = null;
  const flushPending = () => {
    if (pendingCall) { bodyNodes.push(renderWorkItem({ call: pendingCall }, sessionId)); pendingCall = null; }
  };
  let narrCount = 0;
  let thinkCount = 0;
  let opCount = 0;
  for (const m of foldMsgs) {
    if (isProcessMsg(m)) {
      if (m.role === 'tool') {
        // 独立输出消息（旧数据/外部运行时）：并入前一个未配对的调用
        bodyNodes.push(renderWorkItem({ call: pendingCall ?? undefined, out: m }, sessionId));
        pendingCall = null;
        opCount++;
      } else if (m.kind === 'command' || m.kind === 'steps') {
        flushPending();
        pendingCall = m;
        opCount++;
      }
      continue;
    }
    flushPending();
    if (m.role === 'user') {
      // 罕见：同轮中部夹带提问（排队合并场景）——原样展示
      bodyNodes.push(<Message key={m.id} msg={m} runningHere={false} sessionId={sessionId} />);
      continue;
    }
    const text = (m.content || '').trim();
    if (m.reasoning && m.reasoning.trim()) {
      bodyNodes.push(<div key={`r-${m.id}`} className="wf-reason-text">{m.reasoning}</div>);
      thinkCount++;
    }
    if (text) {
      bodyNodes.push(<div key={`n-${m.id}`} className="wf-narr">{text}</div>);
      narrCount++;
    }
  }
  flushPending();

  const foldMeta = [
    opCount > 0 ? `${opCount} 次操作` : '',
    thinkCount > 0 ? '含思考' : '',
    narrCount > 0 ? `${narrCount} 段说明` : '',
  ].filter(Boolean).join(' · ');

  const foldBlock = (
    <details className="worked-fold" key="wf">
      <summary title="展开查看思考、过程说明与工具调用">
        <span className="wf-name">已工作</span>
        {foldMeta && <span className="wf-meta">{foldMeta}</span>}
        <span className="wf-chevron">▸</span>
      </summary>
      <div className="wf-body">{bodyNodes}</div>
    </details>
  );

  // 阅读流：提问 → 「已工作」→ 最终回答（及其它尾部系统提示）
  const nodes: React.ReactNode[] = [];
  const headEnd = Math.min(1, turn.length);
  for (const m of turn.slice(0, headEnd)) {
    nodes.push(<Message key={m.id} msg={m} runningHere={false} sessionId={sessionId} />);
  }
  if (foldMsgs.length > 0) nodes.push(foldBlock);
  for (const m of tailMsgs) {
    nodes.push(<Message key={m.id} msg={m} runningHere={false} sessionId={sessionId} />);
  }
  return (
    <div className="turn-group" data-turn={keyBase}>
      {nodes}
    </div>
  );
}

