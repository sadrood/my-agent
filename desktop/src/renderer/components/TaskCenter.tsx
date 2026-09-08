/**
 * 任务中心（RightDock「任务」tab）：想法速记 + 任务状态机。
 * - 想法：输入速记 → POST /api/thoughts；列表展示。
 * - 任务：状态徽标（todo/进行中/完成/失败）+ 日志展开 + 操作（标记进行中/完成/失败/恢复）。
 */
import React, { useEffect, useState } from 'react';
import { Lightbulb, ListChecks, Plus, Eraser, ChevronRight, ChevronDown, Clock, Trash2 } from 'lucide-react';
import { useTasksStore, type TaskItem } from '../store/tasks';

const STATUS_META: Record<string, { label: string; cls: string }> = {
  todo: { label: '待办', cls: 'todo' },
  in_progress: { label: '进行中', cls: 'in_progress' },
  done: { label: '完成', cls: 'done' },
  failed: { label: '失败', cls: 'failed' },
};

function TaskRow({ t }: { t: TaskItem }) {
  const setTaskStatus = useTasksStore((s) => s.setTaskStatus);
  const addLog = useTasksStore((s) => s.addLog);
  const [open, setOpen] = useState(false);
  const [logText, setLogText] = useState('');
  const meta = STATUS_META[t.status] || STATUS_META.todo;

  const submitLog = () => {
    if (!logText.trim()) return;
    void addLog(t.id, logText.trim());
    setLogText('');
  };

  return (
    <div className={`task-row ${t.status}`}>
      <div className="task-row-head" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className={`task-status ${meta.cls}`}>{meta.label}</span>
        <span className="task-title">{t.title}</span>
      </div>
      {open && (
        <div className="task-row-body">
          <div className="task-actions">
            {t.status !== 'in_progress' && (
              <button className="btn" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => void setTaskStatus(t.id, 'in_progress')}>▶ 进行中</button>
            )}
            {t.status !== 'done' && (
              <button className="btn success" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => void setTaskStatus(t.id, 'done')}>✓ 完成</button>
            )}
            {t.status !== 'failed' && (
              <button className="btn danger" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => void setTaskStatus(t.id, 'failed')}>✗ 失败</button>
            )}
            {t.status === 'failed' && (
              <button className="btn" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => void setTaskStatus(t.id, 'todo')}>↻ 恢复</button>
            )}
          </div>
          <div className="task-logs">
            {(t.logs || []).length === 0 && <div className="task-log-empty">（暂无日志）</div>}
            {(t.logs || []).map((lg, i) => <div key={i} className="task-log">{lg}</div>)}
          </div>
          <div className="task-log-input">
            <input
              value={logText}
              placeholder="追加执行日志…"
              onChange={(e) => setLogText(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) submitLog(); }}
            />
            <button className="btn" style={{ padding: '2px 8px', fontSize: 11 }} onClick={submitLog}>记日志</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function TaskCenter() {
  const { tasks, thoughts, scheduled, addThought, refresh, refreshScheduled, addScheduled, removeScheduled } = useTasksStore();
  const [thoughtText, setThoughtText] = useState('');
  const [thoughtOpen, setThoughtOpen] = useState(true);
  const [schedOpen, setSchedOpen] = useState(true);
  const [schedGoal, setSchedGoal] = useState('');
  const [schedInterval, setSchedInterval] = useState(10);

  useEffect(() => { void refresh(); }, []);
  // 定时任务列表每 30s 刷新（next_run 变化 / 到期执行后更新）
  useEffect(() => {
    void refreshScheduled();
    const t = setInterval(() => void refreshScheduled(), 30000);
    return () => clearInterval(t);
  }, [refreshScheduled]);

  const submitThought = () => {
    if (!thoughtText.trim()) return;
    void addThought(thoughtText.trim());
    setThoughtText('');
  };

  const submitScheduled = () => {
    if (!schedGoal.trim()) return;
    void addScheduled(schedGoal.trim(), schedInterval);
    setSchedGoal('');
  };

  const fmtNext = (n: number | null) => {
    if (!n) return '—';
    const d = new Date(n * 1000);
    return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
  };

  return (
    <div className="task-center">
      {/* 想法 */}
      <div className="task-center-section">
        <div className="task-center-title" onClick={() => setThoughtOpen((v) => !v)}>
          {thoughtOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <Lightbulb size={13} /> 想法 <span className="count">{thoughts.length}</span>
        </div>
        {thoughtOpen && (
          <>
            <div className="thought-input">
              <input
                value={thoughtText}
                placeholder="速记一个想法…"
                onChange={(e) => setThoughtText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) submitThought(); }}
              />
              <button className="btn primary" style={{ padding: '2px 8px', fontSize: 11 }} onClick={submitThought}><Plus size={12} /></button>
            </div>
            <div className="thought-list">
              {thoughts.length === 0 && <div className="task-log-empty">（暂无想法）</div>}
              {thoughts.slice().reverse().map((th) => (
                <div key={th.id} className="thought-item">
                  <span className="thought-time">{th.created}</span>
                  <span className="thought-content">{th.content}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* 任务 */}
      <div className="task-center-section">
        <div className="task-center-title">
          <ListChecks size={13} /> 任务 <span className="count">{tasks.length}</span>
        </div>
        <div className="task-list">
          {tasks.length === 0 && <div className="task-log-empty">（暂无任务，可让 Agent 用 task create 建立）</div>}
          {tasks.slice().reverse().map((t) => <TaskRow key={t.id} t={t} />)}
        </div>
      </div>

      {/* 定时任务（周期调度） */}
      <div className="task-center-section">
        <div className="task-center-title" onClick={() => setSchedOpen((v) => !v)}>
          {schedOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          <Clock size={13} /> 定时任务 <span className="count">{scheduled.length}</span>
        </div>
        {schedOpen && (
          <>
            <div className="thought-input">
              <input
                value={schedGoal}
                placeholder="定时执行的目标…"
                onChange={(e) => setSchedGoal(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) submitScheduled(); }}
              />
              <input
                type="number" min={1} value={schedInterval} style={{ width: 54 }}
                onChange={(e) => setSchedInterval(Number(e.target.value) || 10)}
                title="间隔（分钟）"
              />
              <button className="btn primary" style={{ padding: '2px 8px', fontSize: 11 }} onClick={submitScheduled} title="添加（每隔 N 分钟自动跑一次）"><Plus size={12} /></button>
            </div>
            <div className="sched-list">
              {scheduled.length === 0 && <div className="task-log-empty">（暂无定时任务，设置间隔后添加）</div>}
              {scheduled.map((s) => (
                <div key={s.id} className="sched-item" title={s.goal}>
                  <Clock size={10} className="sched-icon" />
                  <span className="sched-goal">{s.goal.slice(0, 40)}</span>
                  <span className="sched-meta">每 {s.interval_minutes}min · {fmtNext(s.next_run)}</span>
                  <button className="collapse-btn" title="移除" onClick={() => void removeScheduled(s.id)}><Trash2 size={11} /></button>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
