/**
 * 运行面板（运行时透明度）：
 * 常驻展示当前任务的运行状态——轮次 / token 计数 / 沙箱策略 / 缓存命中
 * + 工具调用时间线 + 审批记录。数据全部来自 store 的事件流，纯展示。
 */
import React, { useMemo } from 'react';
import {
  Activity, Coins, ShieldCheck, Layers, TerminalSquare,
  CheckCircle2, XCircle, Clock, Brain,
} from 'lucide-react';
import { useBackendStore } from '../store';

/** 从事件流提取运行聚合信息（纯函数，便于测试）。 */
export function summarizeRun(events: { type: string; data: Record<string, unknown> }[]) {
  let maxTurn = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;
  let toolsOk = 0;
  let toolsFail = 0;
  let policy = '';
  let sandbox = '';
  const toolLog: { name: string; ok: boolean; ts: number }[] = [];
  for (const e of events) {
    const d = e.data || {};
    if (e.type === 'turn_start') {
      maxTurn = Math.max(maxTurn, Number(d.turn || 0));
    }
    if (e.type === 'tool_call' && d.tool && d.tool !== 'think') {
      toolLog.push({ name: String(d.tool), ok: true, ts: Date.now() });
    }
    if (e.type === 'tool_result' && d.tool && d.tool !== 'think') {
      const ok = Boolean(d.success);
      if (ok) toolsOk++; else toolsFail++;
      const last = toolLog.filter((t) => t.name === d.tool).pop();
      if (last) last.ok = ok;
    }
    if (e.type === 'turn_start' && (typeof d.status_text === 'string' || typeof d.statusText === 'string')) {
      // 后端 turn_start 附带 status_text（终端/桌面共用状态文本）：解析 token 计数
      const text = String(d.status_text ?? d.statusText ?? '');
      const m = text.match(/↑([\d.]+[KM]?)\s+↓([\d.]+[KM]?)\s+tok/);
      if (m) {
        inputTokens = _parseK(m[1]);
        outputTokens = _parseK(m[2]);
      }
      const cm = text.match(/缓存\s+(\d+)%/);
      if (cm) cachedTokens = Number(cm[1]);
      const pm = text.match(/策略\s+(\S+)\/?(\S*)/);
      if (pm) policy = pm[1] + (pm[2] ? '/' + pm[2] : '');
      const sm = text.match(/沙箱\s+(\S+)/);
      if (sm) sandbox = sm[1];
    }
  }
  return { maxTurn, inputTokens, outputTokens, cachedTokens, toolsOk, toolsFail, policy, sandbox, toolLog };
}

function _parseK(s: string): number {
  const n = parseFloat(s);
  if (s.endsWith('K')) return Math.round(n * 1000);
  if (s.endsWith('M')) return Math.round(n * 1e6);
  return Math.round(n || 0);
}

export function RuntimePane() {
  const events = useBackendStore((s) => s.events);
  const running = useBackendStore((s) => s.running);
  const agg = useMemo(() => summarizeRun(events), [events]);

  const stats = [
    { icon: Layers, label: '轮次', value: agg.maxTurn || '—' },
    { icon: Coins, label: '输入', value: agg.inputTokens ? (agg.inputTokens / 1000).toFixed(1) + 'K' : '—' },
    { icon: Coins, label: '输出', value: agg.outputTokens ? (agg.outputTokens / 1000).toFixed(1) + 'K' : '—' },
    { icon: Activity, label: '缓存', value: agg.cachedTokens ? agg.cachedTokens + '%' : '—' },
    { icon: CheckCircle2, label: '工具✓', value: agg.toolsOk || '—' },
    { icon: XCircle, label: '工具✗', value: agg.toolsFail || '—' },
  ];

  return (
    <div className="runtime-pane">
      {/* 状态灯 + 策略 */}
      <div className="rt-head">
        <span className={`rt-dot ${running ? 'rt-running' : ''}`} />
        <span className="rt-title">{running ? '运行中' : '空闲'}</span>
        {agg.policy && <span className="rt-chip" title="审批策略"><ShieldCheck size={10} /> {agg.policy}</span>}
        {agg.sandbox && <span className="rt-chip" title="沙箱等级"><ShieldCheck size={10} /> {agg.sandbox}</span>}
      </div>

      {/* 统计网格 */}
      <div className="rt-grid">
        {stats.map(({ icon: Icon, label, value }) => (
          <div key={label} className="rt-cell" title={label}>
            <Icon size={12} />
            <span className="rt-val">{value}</span>
            <span className="rt-label">{label}</span>
          </div>
        ))}
      </div>

      {/* 工具调用时间线 */}
      <div className="rt-timeline">
        <div className="rt-section-title"><Clock size={10} /> 工具调用</div>
        {agg.toolLog.length === 0 && (
          <div className="rt-empty">暂无工具调用</div>
        )}
        {agg.toolLog.slice(-20).map((t, i) => (
          <div key={i} className="rt-tool">
            {t.ok ? <CheckCircle2 size={11} className="rt-ok" /> : <XCircle size={11} className="rt-fail" />}
            <TerminalSquare size={11} />
            <span className="rt-tool-name">{t.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
}