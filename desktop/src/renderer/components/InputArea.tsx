/**
 * 输入区：多行文本（自动处理大段粘贴）、拖拽文件/图片、@ 文件选择器。
 */
import React, { useEffect, useRef, useState } from 'react';
import { Send, Paperclip, AtSign, X, Square, Clock, Zap, Cpu, Settings2 } from 'lucide-react';
import { stopRun, fetchFileTree, fetchRuntimes, fetchSkills, setPermissionMode, type FileTreeNode, type SkillInfo } from '../lib/backend';
import { useBackendStore, usePermissionStore, useSessionStore, useUIStore } from '../store';
import { loadConfig, saveConfig } from '../lib/appConfig';
import { MODE_META } from './StatusBar';
import type { PermissionMode } from '../lib/types';

/** 运行时引擎显示名 */
const RUNTIME_LABELS: Record<string, string> = {
  myagent: '内置 Agent',
  claude: '外部 CLI（文本输出）',
  'claude-acp': '外部 CLI · ACP（结构化事件）',
  codex: '外部 CLI（JSON 事件流）',
  custom: '自定义',
};

interface Props {
  onSend: (text: string, attachments: { name: string; content: string }[]) => void;
  disabled?: boolean;
}

export function InputArea({ onSend, disabled }: Props) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<{ name: string; content: string }[]>([]);
  const [showAtMenu, setShowAtMenu] = useState(false);
  const [atQuery, setAtQuery] = useState('');
  // `/` 技能引用：行尾输入 /name 触发，列出可用技能包（数据与右栏「技能」tab 同源）
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [showSkillMenu, setShowSkillMenu] = useState(false);
  const [skillQuery, setSkillQuery] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 后端是否正在运行任务：运行中发送按钮切换为「停止」，输入改为入队
  const running = useBackendStore((s) => s.running);
  const queue = useBackendStore((s) => s.queue);
  const enqueueGoal = useBackendStore((s) => s.enqueueGoal);
  const removeQueuedAt = useBackendStore((s) => s.removeQueuedAt);
  // 当前会话 id：入队时绑定归属会话，防止出队后落到别的会话
  const activeSessionId = useSessionStore((s) => s.activeSessionId);

  // 权限模式与模型（chips 常驻 composer 下沿，点模型可快改）
  const { mode, setMode } = usePermissionStore();
  const [modelOpen, setModelOpen] = useState(false);
  const [model, setModel] = useState(() => loadConfig().model);

  // 运行时引擎（多 Agent：内置 / 外部 CLI / 自定义）
  const { sessions, setSessionRuntime } = useSessionStore();
  const activeSess = sessions.find((s) => s.id === activeSessionId);
  const currentRuntime = activeSess?.runtime || 'myagent';
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const [runtimeAvail, setRuntimeAvail] = useState<Record<string, boolean>>({});
  const [customCmd, setCustomCmd] = useState(() => localStorage.getItem('my-agent-custom-runtime') || '');
  useEffect(() => {
    void fetchRuntimes().then((r) => setRuntimeAvail(r.available || {}));
  }, []);

  const cycleMode = () => {
    const order: PermissionMode[] = ['ask', 'auto', 'block'];
    const next = order[(order.indexOf(mode) + 1) % order.length];
    setMode(next);
    // 同步到后端：任务运行中也立即生效（审批桥按会话实时读取；也作为下次运行默认）
    void setPermissionMode(next);
  };

  const commitModel = (v: string) => {
    const name = v.trim();
    if (!name || name === model) return;
    setModel(name);
    saveConfig({ ...loadConfig(), model: name });
  };

  // 候选文件（@ 弹出）：从后端 /api/files 拉真实工作区文件树；失败时回退到常用文件清单
  const [candidates, setCandidates] = useState<string[]>(() => [
    'main.py', 'agent/agent.py', 'agent/executor.py', 'config.py',
    'tools/tool_manager.py', 'docs/BEST_PRACTICES.md',
  ]);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const res = await fetchFileTree();
      if (!res || cancelled) return;
      const out: string[] = [];
      const walk = (nodes: FileTreeNode[]) => {
        for (const n of nodes) {
          if (out.length >= 500) return;
          if (n.type === 'dir') walk(n.children || []);
          else out.push(n.path);
        }
      };
      walk([res.tree]);
      if (out.length > 0) setCandidates(out);
    })();
    return () => { cancelled = true; };
  }, []);
  const matched = candidates.filter((f) => f.includes(atQuery)).slice(0, 30);

  // 技能包列表：一次性拉取缓存；/name 过滤命中
  useEffect(() => {
    void fetchSkills().then(setSkills);
  }, []);
  const skillHits = skills
    .filter((s) => !skillQuery || s.name.includes(skillQuery) || (s.description || '').includes(skillQuery))
    .slice(0, 10);

  const insertSkill = (name: string) => {
    setText((prev) => prev.replace(/\/([\w-]*)$/, `/${name} `));
    setShowSkillMenu(false);
    textareaRef.current?.focus();
  };

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;
    if (running) {
      // 任务队列：运行中回车 = 排队，本轮结束自动下发（绑定发起会话）
      enqueueGoal({ text: trimmed, attachments, sessionId: activeSessionId || '' });
    } else {
      onSend(trimmed, attachments);
    }
    setText('');
    setAttachments([]);
    setShowAtMenu(false);
    setShowSkillMenu(false);
  };

  // 响应外部事件：聚焦输入框 + 插入提示词模板
  useEffect(() => {
    const focusHandler = () => textareaRef.current?.focus();
    const insertHandler = (e: Event) => {
      const tpl = (e as CustomEvent<string>).detail || '';
      setText((prev) => (prev ? prev + '\n' : '') + tpl);
      textareaRef.current?.focus();
    };
    window.addEventListener('focus-input', focusHandler);
    window.addEventListener('insert-prompt', insertHandler);
    return () => {
      window.removeEventListener('focus-input', focusHandler);
      window.removeEventListener('insert-prompt', insertHandler);
    };
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // 回车发送/排队；Shift+Enter 换行；输入法组词中的回车只确认候选词
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length > 0) {
      e.preventDefault();
      files.forEach((f) => {
        const reader = new FileReader();
        reader.onload = () => {
          const content = typeof reader.result === 'string' ? reader.result : '';
          const isImage = f.type.startsWith('image/');
          setAttachments((prev) => [...prev, {
            name: f.name,
            content: isImage ? `[图片] ${f.name} (${Math.round(f.size / 1024)}KB)` : content.slice(0, 5000),
          }]);
        };
        if (f.type.startsWith('image/')) reader.readAsDataURL(f);
        else reader.readAsText(f);
      });
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files || []);
    files.forEach((f) => {
      const reader = new FileReader();
      reader.onload = () => {
        const content = typeof reader.result === 'string' ? reader.result : '';
        setAttachments((prev) => [...prev, {
          name: f.name,
          content: f.type.startsWith('image/') ? `[图片] ${f.name}` : content.slice(0, 5000),
        }]);
      };
      if (f.type.startsWith('image/')) reader.readAsDataURL(f);
      else reader.readAsText(f);
    });
  };

  const insertAtFile = (path: string) => {
    setText((prev) => prev + ` @${path}`);
    setShowAtMenu(false);
    textareaRef.current?.focus();
  };

  return (
    <div className="input-area">
      <div className="input-box" onDrop={handleDrop} onDragOver={(e) => e.preventDefault()}>
        <textarea
          ref={textareaRef}
          className="input-textarea"
          placeholder={running
            ? `任务运行中…回车把新任务加入队列（已排 ${queue.length} 条）`
            : '输入消息，@ 引用文件，/ 调用技能 · 回车发送 · Shift+Enter 换行'}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            // 检测 @ 触发文件选择器；检测行尾 /name 触发技能选择器
            const m = e.target.value.match(/@(\w*)$/);
            setShowAtMenu(!!m);
            setAtQuery(m?.[1] || '');
            const sk = e.target.value.match(/(^|\s)\/([\w-]*)$/);
            setShowSkillMenu(!!sk);
            setSkillQuery(sk?.[2] || '');
          }}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          disabled={disabled}
        />
        {attachments.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
            {attachments.map((a, i) => (
              <span key={i} className="attach-chip">
                <Paperclip size={11} /> {a.name}
                <button
                  className="collapse-btn"
                  onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                ><X size={11} /></button>
              </span>
            ))}
          </div>
        )}
        {queue.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
            {queue.map((q, i) => (
              <span key={i} className="attach-chip" title={q.text}>
                <Clock size={11} /> {q.text.slice(0, 24) || '(附件任务)'}
                <button className="collapse-btn" title="移出队列"
                  onClick={() => removeQueuedAt(i)}><X size={11} /></button>
              </span>
            ))}
          </div>
        )}
        <div className="input-toolbar">
          <button className="collapse-btn" title="附加文件" onClick={() => fileInputRef.current?.click()}>
            <Paperclip size={16} />
          </button>
          <button className="collapse-btn" title="引用文件 (@)" onClick={() => setShowAtMenu((v) => !v)}>
            <AtSign size={16} />
          </button>
          <div style={{ flex: 1 }} />
          <button
            className="status-chip"
            data-chip="mode"
            onClick={cycleMode}
            title="点击切换权限模式（自动执行/每次询问/禁止）"
          >
            {MODE_META[mode].icon} <span className="sc-label">{MODE_META[mode].label}</span>
            <span className={`status-dot ${MODE_META[mode].dot}`} />
          </button>
          <button
            className="status-chip"
            data-chip="tasks"
            onClick={() => useUIStore.getState().setRightTab('tasks')}
            title="定时任务（点击直达任务中心）"
          >
            <Clock size={11} /> <span className="sc-label">定时</span>
          </button>
          <span style={{ position: 'relative' }}>
            <button
              className="status-chip"
              data-chip="runtime"
              onClick={() => setRuntimeOpen((v) => !v)}
              title="运行时引擎（驱动本会话的 Agent）"
            >
              <Settings2 size={13} /> <span className="sc-label">{RUNTIME_LABELS[currentRuntime] || currentRuntime}</span>
            </button>
            {runtimeOpen && (
              <div className="runtime-pop">
                {Object.entries(RUNTIME_LABELS).map(([k, label]) => (
                  <button key={k} className="prompt-tpl" onClick={() => { if (activeSessionId) setSessionRuntime(activeSessionId, k); setRuntimeOpen(false); }}>
                    {label}{k !== 'myagent' && !runtimeAvail[k] ? '（未安装）' : ''}
                  </button>
                ))}
                {currentRuntime === 'custom' && (
                  <div className="form-field" style={{ margin: 0, paddingTop: 6 }}>
                    <label>命令模板（含 {`{goal}`}）</label>
                    <input
                      value={customCmd}
                      onChange={(e) => { setCustomCmd(e.target.value); localStorage.setItem('my-agent-custom-runtime', e.target.value); }}
                      placeholder="python my_runner.py {goal}"
                    />
                  </div>
                )}
              </div>
            )}
          </span>
          <span style={{ position: 'relative' }}>
            <button
              className="status-chip"
              onClick={() => setModelOpen((v) => !v)}
              title={`当前模型：${model || '未配置'}（点击修改）`}
            >
              <Cpu size={13} /> <span className="sc-label">{model || '未配置'}</span>
            </button>
            {modelOpen && (
              <div className="model-pop">
                <div className="model-quick-label">常用模型</div>
                <div className="model-quick">
                  {['deepseek-chat', 'deepseek-reasoner', 'kimi-k2-0905-preview', 'glm-4.5', 'claude-sonnet-4-5'].map((m) => (
                    <button key={m} className={`prompt-tpl ${m === model ? 'active' : ''}`} onClick={() => { commitModel(m); setModelOpen(false); }}>
                      {m}
                    </button>
                  ))}
                </div>
                <div className="form-field" style={{ marginBottom: 4, marginTop: 6 }}>
                  <label>或输入自定义模型名</label>
                  <input
                    key={model}
                    defaultValue={model}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                        commitModel((e.target as HTMLInputElement).value);
                        setModelOpen(false);
                      }
                      if (e.key === 'Escape') setModelOpen(false);
                    }}
                    onBlur={(e) => { commitModel(e.target.value); setModelOpen(false); }}
                  />
                  <div className="form-hint">回车保存，下次发送即生效；端点 / API Key 在「设置」里改。</div>
                </div>
              </div>
            )}
          </span>
          <button
            className={running ? 'btn danger' : 'btn primary'}
            title={running ? '终止当前任务（后端会强杀正在运行的子进程）' : '发送'}
            onClick={running ? () => void stopRun() : send}
            disabled={!running && (disabled || (!text.trim() && attachments.length === 0))}
          >
            {running ? <><Square size={14} /> 停止</> : <><Send size={14} /> 发送</>}
          </button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: 'none' }}
          onChange={(e) => {
            Array.from(e.target.files || []).forEach((f) => {
              const reader = new FileReader();
              reader.onload = () => setAttachments((prev) => [...prev, {
                name: f.name,
                content: typeof reader.result === 'string' ? reader.result.slice(0, 5000) : '',
              }]);
              reader.readAsText(f);
            });
            e.target.value = '';
          }}
        />
      </div>
      {showAtMenu && (
        <div className="card at-menu">
          <div className="card-body" style={{ padding: 6 }}>
            {matched.map((f) => (
              <button key={f} className="prompt-tpl" onClick={() => insertAtFile(f)}>{f}</button>
            ))}
            {matched.length === 0 && <div style={{ padding: 8, fontSize: 12, color: 'var(--text-muted)' }}>无匹配文件</div>}
          </div>
        </div>
      )}
      {showSkillMenu && (
        <div className="card at-menu">
          <div className="card-body" style={{ padding: 6 }}>
            {skillHits.map((s) => (
              <button key={s.name} className="prompt-tpl" title={s.description} onClick={() => insertSkill(s.name)}>
                <Zap size={11} style={{ color: 'var(--accent)', marginRight: 5 }} />
                /{s.name}
                {s.description ? <span style={{ color: 'var(--text-muted)', marginLeft: 8 }}>· {s.description.slice(0, 36)}</span> : null}
              </button>
            ))}
            {skillHits.length === 0 && (
              <div style={{ padding: 8, fontSize: 12, color: 'var(--text-muted)' }}>
                无匹配技能（可在 skills/&lt;技能名&gt;/SKILL.md 创建）
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
