/**
 * 会话概览（Session Overview）—— 右侧面板默认页（指标监控）。
 * 上下文窗口监控 + 核心 KPI 网格 + 用量分析（按模型/按类型分段）。
 *
 * 数据说明：当前为本地预置 Mock（OVERVIEW_MOCK），接口类型已定义好
 * （OverviewData / ContextWindowInfo / SessionMetrics / ModelUsageRow），
 * 后续接后端 usage 事件流时仅需替换数据源。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Activity } from 'lucide-react';
import { useBackendStore, useSessionStore } from '../store';
import type { AgentEvent } from '../lib/types';
import { lookupContextWindow } from '../lib/modelCatalog';

/* ------------------------------------------------------------------ */
/* 真实数据聚合                                                         */
/* ------------------------------------------------------------------ */

/** 估算单价（元 / 1M tokens）。默认主流近似，可经 localStorage
 *  'my-agent-prices' 覆盖：{"input":0.5,"cacheHit":0.05,"output":2.0} */
const DEFAULT_PRICE_PER_M = { input: 0.5, cacheHit: 0.05, output: 2.0 };
function pricePerM(): { input: number; cacheHit: number; output: number } {
  try {
    const p = JSON.parse(localStorage.getItem('my-agent-prices') || '{}');
    return {
      input: Number(p.input) || DEFAULT_PRICE_PER_M.input,
      cacheHit: Number(p.cacheHit) || DEFAULT_PRICE_PER_M.cacheHit,
      output: Number(p.output) || DEFAULT_PRICE_PER_M.output,
    };
  } catch { return DEFAULT_PRICE_PER_M; }
}
/** 模型 → 上下文窗口上限：先查精确目录（modelCatalog），未收录再按家族回退；
 *  用户环境按百万上下文，未知模型默认 1M。 */
export function windowLimit(model: string): number {
  const exact = lookupContextWindow(model);
  if (exact) return exact;
  const m = model.toLowerCase();
  if (m.includes('claude') || m.includes('o1') || m.includes('o3') || m.includes('chatgpt')) return 200_000;
  if (m.includes('gpt-4') || m.includes('gpt-3')) return 128_000;
  return 1_000_000;
}

/** 数值安全化：后端字段异常/缺失时归零，杜绝 NaN 传染渲染崩溃 */
function num(v: unknown): number {
  const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''));
  return Number.isFinite(n) && n > 0 ? n : 0;
}

interface AggregatedOverview {
  model: string;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
  cacheHitPct: number;
  requestCount: number;
  runtimeMs: number;
  costYuan: number;
  totalTokens: number;
  running: boolean;
  limit: number;
  /** 上下文窗口水位：最近一次请求的输入规模（后端 context_tokens；运行中用 ↑ 实时近似） */
  contextTokens: number;
  /** 后端权威摘要行（轮/步/LLM 与工具耗时/首 token/速率/缓存/输入输出），运行中用状态文本实时 */
  summaryLine: string;
  /** 工具调用步数（含 think） */
  steps: number;
  /** LLM 调用总耗时（秒） */
  llmSeconds: number;
  /** 后端压缩 token 阈值（COMPACT_CONFIG 下发；概览刻度用它而非死编码 80%） */
  compactThresholdTokens: number;
}

/** 从事件流聚合真实运行统计（纯函数，便于测试）。
 *  sessionId 传入时只聚合该会话的事件（主事件 session_id / 辅助 side_of），
 *  保证每个对话的概览相互独立不串台。 */
export function aggregateOverview(
  events: AgentEvent[],
  running: boolean,
  sessionId?: string | null,
): AggregatedOverview | null {
  if (sessionId) {
    events = events.filter((e) => {
      const d = (e.data || {}) as Record<string, unknown>;
      return String(d.session_id || d.side_of || '') === sessionId;
    });
  }
  // 会话累计（已收尾运行之和）——同一会话多次发送不再互相清零
  let totalIn = 0, totalOut = 0, totalCached = 0, totalSteps = 0, totalSeconds = 0;
  let requests = 0;            // 会话累计模型轮数
  let runReqs = 0;             // 当前运行内轮数
  let runTurnsFromMetrics = 0; // 当前运行由 metrics.turns 提供的权威轮数
  let runStart = 0, runEnd = 0, firstRunStart = 0;
  let model = '';
  let ctxTokens = 0;           // 窗口水位：最近一次请求输入（跨运行保留，宁缺毋滥）
  let summaryLine = '';
  let statusTextNow = '';
  let compactThresholdTokens = 0;
  let lastCacheRate = 0;       // 最近一次报告的缓存率（无 cached 明细时兜底展示）
  let curIn = 0, curOut = 0, curCached = 0, curSteps = 0, curSeconds = 0;
  let runOpen = false;
  let runEnded = false;    // run_end 已到但 metrics 未到：收尾延后，等 metrics/下一轮
  let seenRun = false;

  const finalizeRun = () => {
    if (!runOpen) return;
    totalIn += curIn; totalOut += curOut; totalCached += curCached;
    totalSteps += curSteps; totalSeconds += curSeconds;
    requests += runTurnsFromMetrics > 0 ? runTurnsFromMetrics : runReqs;
    runOpen = false; runReqs = 0; runTurnsFromMetrics = 0;
    curIn = curOut = curCached = curSteps = curSeconds = 0;
  };

  for (const e of events) {
    const d = (e.data || {}) as Record<string, unknown>;
    if (e.type === 'run_start') {
      finalizeRun();               // 上一轮若缺 run_end/metrics（窗口截断）也先收尾
      seenRun = true;
      runOpen = true;
      runEnded = false;
      runStart = e.timestamp;
      if (!firstRunStart) firstRunStart = e.timestamp;
    } else if (e.type === 'run_end') {
      // 真实事件序：metrics 在 run_end 之后（_finalize_run）——此处只标记，等 metrics 并入
      runEnd = e.timestamp;
      runEnded = true;
    } else if (e.type === 'turn_start') {
      runReqs++;
      if (runOpen && !runStart) runStart = e.timestamp;
      const st = String(d.status_text || d.statusText || '');
      if (st) statusTextNow = st;
      // 结构化累计字段（每轮单调增，取峰值 = 本轮累计）；旧后端回退解析状态文本
      if (typeof d.input_tokens === 'number' || d.input_tokens !== undefined) {
        curIn = Math.max(curIn, num(d.input_tokens));
        curOut = Math.max(curOut, num(d.output_tokens));
        curCached = Math.max(curCached, num(d.cached_tokens));
        if (typeof d.cache_hit_rate === 'number' && Number.isFinite(d.cache_hit_rate)) lastCacheRate = d.cache_hit_rate;
        curSteps = Math.max(curSteps, num(d.steps));
        curSeconds = Math.max(curSeconds, num(d.llm_seconds));
        compactThresholdTokens = num(d.compact_threshold_tokens) || compactThresholdTokens;
        ctxTokens = num(d.context_tokens) || ctxTokens;
      } else {
        const up = /↑([\d.]+)([KM]?)/.exec(st);
        const dn = /↓([\d.]+)([KM]?)/.exec(st);
        const ch = /缓存\s*([\d.]+)%/.exec(st);
        if (up) curIn = Math.max(curIn, parseTok(up[1], up[2]));
        if (dn) curOut = Math.max(curOut, parseTok(dn[1], dn[2]));
        if (ch) lastCacheRate = parseFloat(ch[1]);
      }
    } else if (e.type === 'compaction') {
      // 压缩完成：旧水位失效归零，下轮 turn_start 恢复；会话累计不回退
      ctxTokens = 0;
    } else if (e.type === 'metrics') {
      // 本轮结构化最终统计：并入会话累计
      const mt = num(d.turns);
      if (mt > 0) runTurnsFromMetrics = mt;
      curIn = Math.max(curIn, num(d.input_tokens) || 0);
      curOut = Math.max(curOut, num(d.output_tokens) || 0);
      curCached = Math.max(curCached, num(d.cached_tokens) || 0);
      curSteps = Math.max(curSteps, num(d.steps) || 0);
      curSeconds = Math.max(curSeconds, num(d.llm_seconds) || 0);
      if (typeof d.cache_hit_rate === 'number' && Number.isFinite(d.cache_hit_rate)) lastCacheRate = d.cache_hit_rate;
      compactThresholdTokens = num(d.compact_threshold_tokens) || compactThresholdTokens;
      model = String(d.model || model);
      ctxTokens = num(d.context_tokens) || ctxTokens;
      if (typeof d.line === 'string' && d.line) summaryLine = d.line;
      if (runOpen) {
        finalizeRun();
        runEnded = false;
      } else {
        // 事件窗口被裁剪/过滤后只剩 metrics（缺 run_start）：直接并入会话累计，
        // 否则会出现「缓存命中 89% 其余全 0」的残缺概览
        totalIn += curIn; totalOut += curOut; totalCached += curCached;
        totalSteps += curSteps; totalSeconds += curSeconds;
        requests += runTurnsFromMetrics > 0 ? runTurnsFromMetrics : runReqs;
        curIn = curOut = curCached = curSteps = curSeconds = 0;
        runReqs = 0; runTurnsFromMetrics = 0;
      }
    }
  }
  // 事件窗口末尾：若运行已结束但 metrics 缺失（截断/老后端），按现有峰值收尾
  if (runOpen && !running) finalizeRun();
  const live = runOpen;   // 运行未收尾（事件仍在进行）：实时并入展示
  const inputTokens = totalIn + (live ? curIn : 0);
  const outputTokens = totalOut + (live ? curOut : 0);
  const cachedTokens = totalCached + (live ? curCached : 0);
  const steps = totalSteps + (live ? curSteps : 0);
  const llmSeconds = totalSeconds + (live ? curSeconds : 0);
  const requestCount = requests + (live ? (runTurnsFromMetrics > 0 ? runTurnsFromMetrics : runReqs) : 0);
  if (!seenRun && requestCount === 0 && inputTokens === 0 && outputTokens === 0 && ctxTokens === 0) return null;
  // 缓存命中率：会话口径 cached/input；无明细时用最近报告值
  const hit = (cachedTokens > 0 && inputTokens > 0)
    ? (cachedTokens / inputTokens) * 100
    : lastCacheRate > 0 ? lastCacheRate : 0;
  const contextTokens = ctxTokens;
  const totalTokens = inputTokens + outputTokens;
  const summary = summaryLine || (running ? statusTextNow : '');
  const runtimeMs = running
    ? ((firstRunStart || runStart) ? Date.now() - (firstRunStart || runStart) : 0)
    : (runEnd && firstRunStart ? runEnd - firstRunStart : 0);
  const uncached = Math.max(0, inputTokens - cachedTokens);
  const p = pricePerM();
  const costYuan =
    (uncached / 1e6) * p.input +
    (cachedTokens / 1e6) * p.cacheHit +
    (outputTokens / 1e6) * p.output;
  const limit = windowLimit(model || 'deepseek-chat');
  return {
    model, inputTokens, outputTokens, cachedTokens,
    cacheHitPct: Math.round(hit * 100) / 100,
    requestCount, runtimeMs, costYuan, totalTokens, running,
    limit, contextTokens, summaryLine: summary, steps, llmSeconds,
    compactThresholdTokens,
  };
}

function parseTok(v: string, unit?: string): number {
  const n = parseFloat(v) || 0;
  return unit === 'M' ? n * 1_000_000 : unit === 'K' ? n * 1000 : n;
}

/* ------------------------------------------------------------------ */
/* 数据模型                                                             */
/* ------------------------------------------------------------------ */

export interface ContextWindowInfo {
  usedTokens: number;
  totalTokens: number;
  /** 触发压缩的阈值（百分比，如 80） */
  compactionThresholdPct: number;
}

export interface SessionMetrics {
  /** Prompt Cache 命中率（0~100） */
  cacheHitPct: number;
  /** 工具调用步数（含 think） */
  steps: number;
  /** LLM 调用总耗时（秒） */
  llmSeconds: number;
  /** 会话累计费用（元） */
  costYuan: number;
  /** 运行耗时 ms */
  runtimeMs: number;
  /** LLM 请求次数 */
  requestCount: number;
  /** 运行 tokens（含缓存） */
  totalTokens: number;
}

export interface UsageRequestDetail {
  ts: string;
  tokens: number;
  cacheHitPct: number;
  costYuan: number;
}

export interface ModelUsageRow {
  model: string;
  /** 主模型 / 辅助模型 */
  role: 'main' | 'sub';
  calls: number;
  tokens: number;
  cacheHitPct: number;
  costYuan: number;
  requests?: UsageRequestDetail[];
}

export type UsageKindRow = { kind: string; tokens: number; pct: number };

export interface OverviewData {
  context: ContextWindowInfo;
  metrics: SessionMetrics;
  /** 按来源（模型）分解 */
  byModel: ModelUsageRow[];
  /** 按类型分解 */
  byType: UsageKindRow[];
}

/** 本地预置 Mock：便于直接预览，后续对接后端 usage 数据流。
 *  数字口径自洽：metrics.totalTokens = 各模型 tokens 之和；
 *  cacheHitPct = Σ(tokens×缓存率)/Σtokens（加权）；费用 = 各模型费用之和；
 *  byType 各项之和 = totalTokens。 */
export const OVERVIEW_MOCK: OverviewData = {
  context: { usedTokens: 13502, totalTokens: 1_000_000, compactionThresholdPct: 80 },
  metrics: { cacheHitPct: 52.22, costYuan: 0.0141, runtimeMs: 99_000, requestCount: 2, totalTokens: 26993, steps: 16, llmSeconds: 143.9 },
  byModel: [
    {
      model: 'deepseek-chat', role: 'main', calls: 2, tokens: 18400, cacheHitPct: 71, costYuan: 0.0102,
      requests: [
        { ts: '15:02:11', tokens: 11500, cacheHitPct: 74, costYuan: 0.0061 },
        { ts: '15:02:48', tokens: 6900, cacheHitPct: 66, costYuan: 0.0041 },
      ],
    },
    {
      model: 'deepseek-reasoner', role: 'sub', calls: 1, tokens: 8593, cacheHitPct: 12, costYuan: 0.0039,
      requests: [
        { ts: '15:03:02', tokens: 8593, cacheHitPct: 12, costYuan: 0.0039 },
      ],
    },
  ],
  byType: [
    { kind: '缓存命中', tokens: 14095, pct: 52 },
    { kind: '输入', tokens: 8000, pct: 30 },
    { kind: '输出', tokens: 4898, pct: 18 },
  ],
};

/* ------------------------------------------------------------------ */
/* 工具函数                                                             */
/* ------------------------------------------------------------------ */

const fmt = (n: number) => n.toLocaleString('en-US');
/** token 数紧凑格式：800,000 → 800K，1,000,000 → 1.0M */
const fmtTokens = (n: number) =>
  n >= 1_000_000 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
const fmtMoney = (n: number) => {
  if (!n || n < 0.00005) return '¥0';
  return `¥${n.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
};
const fmtDuration = (ms: number) => {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}秒`;
  return `${Math.floor(s / 60)}分${s % 60}秒`;
};

function ctxState(pct: number): { label: string; cls: string } {
  if (!Number.isFinite(pct) || pct <= 0) return { label: '暂无用量', cls: 'muted' };
  if (pct >= 100) return { label: '超出估算窗口', cls: 'crit' };
  if (pct >= 90) return { label: '即将触发压缩', cls: 'crit' };
  if (pct >= 70) return { label: '注意容量', cls: 'warn' };
  return { label: '上下文充足', cls: 'ok' };
}

/* ------------------------------------------------------------------ */
/* 组件                                                                 */
/* ------------------------------------------------------------------ */

function Card({ title, right, children }: { title: React.ReactNode; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="ov-card">
      <div className="ov-card-head">
        <span className="ov-card-title">{title}</span>
        {right && <span className="ov-card-right">{right}</span>}
      </div>
      {children}
    </div>
  );
}

/** 上下文窗口监控卡 */
function ContextMonitor({ ctx }: { ctx: ContextWindowInfo }) {
  const usedSafe = Number.isFinite(ctx.usedTokens) ? Math.max(0, ctx.usedTokens) : 0;
  const totalSafe = Number.isFinite(ctx.totalTokens) && ctx.totalTokens > 0 ? ctx.totalTokens : 1;
  const pct = (usedSafe / totalSafe) * 100;
  const st = ctxState(pct);
  const remain = Math.max(0, Math.round(totalSafe * (ctx.compactionThresholdPct / 100)) - usedSafe);
  const thresholdX = ctx.compactionThresholdPct;
  return (
    <Card
      title={
        <>
          <span className="ov-ctx-title">上下文窗口</span>
          <span className={`ov-badge ${st.cls}`}>{st.label}</span>
        </>
      }
    >
      <div className="ov-ctx-nums">
        <span className="ov-ctx-used">{fmt(usedSafe)}</span>
        <span className="ov-ctx-total">/ {fmt(totalSafe)} tokens</span>
      </div>
      <div className="ov-bar">
        <div className="ov-bar-fill" style={{ width: `${pct <= 0 ? 0 : Math.max(0.6, Math.min(100, pct))}%` }} />
        <div className="ov-threshold" style={{ left: `${thresholdX}%` }} title={`压缩阈值 ${thresholdX}%`}>
          <i />
          <em>阈值 {thresholdX}%</em>
        </div>
      </div>
      <div className="ov-ctx-foot">
        <span>已用 {Number.isFinite(pct) ? pct.toFixed(1) : '0.0'}%</span>
        <span>距阈值 {fmtTokens(remain)}</span>
      </div>
    </Card>
  );
}

/** 会话核心指标网格 */
function MetricsGrid({ m }: { m: SessionMetrics }) {
  const cells: { label: string; value: string; strong?: boolean }[] = [
    { label: '缓存命中', value: `${m.cacheHitPct.toFixed(2)}%`, strong: true },
    { label: '会话费用', value: fmtMoney(m.costYuan) },
    { label: '运行时间', value: fmtDuration(m.runtimeMs) },
    { label: '请求数', value: String(m.requestCount) },
    { label: '工具步骤', value: String(m.steps) },
    { label: 'LLM 耗时', value: fmtDuration(Math.round((m.llmSeconds || 0) * 1000)) },
  ];
  return (
    <Card title="会话指标">
      <div className="ov-kpi-grid">
        {cells.map((c) => (
          <div key={c.label} className="ov-kpi">
            <div className={`ov-kpi-value ${c.strong ? 'strong' : ''}`}>{c.value}</div>
            <div className="ov-kpi-label">{c.label}</div>
          </div>
        ))}
      </div>
      <div className="ov-total-row">
        <span>运行 tokens</span>
        <b>{fmt(m.totalTokens)}</b>
      </div>
    </Card>
  );
}

/** 用量分析：按来源（模型）/ 按类型 */
function UsageBreakdown({ data }: { data: OverviewData }) {
  const [mode, setMode] = useState<'model' | 'type'>('model');
  const [openModel, setOpenModel] = useState<string | null>(null);
  const rows = mode === 'model' ? data.byModel : [];
  const typeRows = mode === 'type' ? data.byType : [];
  const maxTokens = Math.max(1, ...rows.map((r) => r.tokens), ...typeRows.map((r) => r.tokens));

  return (
    <Card
      title="用量分析"
      right={
        <span className="ov-seg">
          <button className={mode === 'model' ? 'on' : ''} onClick={() => setMode('model')}>按来源</button>
          <button className={mode === 'type' ? 'on' : ''} onClick={() => setMode('type')}>按类型</button>
        </span>
      }
    >
      {mode === 'model' ? (
        <div className="ov-models">
          {rows.map((r) => (
            <div key={r.model} className="ov-model">
              <div className="ov-model-top">
                <span className="ov-model-name">
                  <i className={`ov-dot ${r.role === 'main' ? 'main' : ''}`} />
                  {r.role === 'main' ? '主模型' : '辅助'} · {r.model}
                </span>
                <span className="ov-model-calls">{r.calls} 次</span>
              </div>
              <div className="ov-model-bar">
                <i style={{ width: `${(r.tokens / maxTokens) * 100}%` }} />
              </div>
              <div className="ov-model-sub">
                <span>总计 {fmt(r.tokens)} tokens</span>
                <span>缓存 {r.cacheHitPct}%</span>
                <span>{fmtMoney(r.costYuan)}</span>
              </div>
              {r.requests && r.requests.length > 0 && (
                <>
                  <button className="ov-detail-toggle" onClick={() => setOpenModel(openModel === r.model ? null : r.model)}>
                    {openModel === r.model ? '▾ 收起明细' : '▶ 明细'}
                  </button>
                  {openModel === r.model && (
                    <div className="ov-detail">
                      {r.requests.map((q, i) => (
                        <div key={i} className="ov-detail-row">
                          <span>{q.ts}</span>
                          <span>{fmt(q.tokens)} tok</span>
                          <span>缓存 {q.cacheHitPct}%</span>
                          <span>{fmtMoney(q.costYuan)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="ov-models">
          {typeRows.map((r) => (
            <div key={r.kind} className="ov-model">
              <div className="ov-model-top">
                <span className="ov-model-name"><i className="ov-dot" />{r.kind}</span>
                <span className="ov-model-calls">{r.pct}%</span>
              </div>
              <div className="ov-model-bar"><i style={{ width: `${r.pct}%` }} /></div>
              <div className="ov-model-sub">
                <span>总计 {fmt(r.tokens)} tokens</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/** 会话概览面板（右栏默认页） */
/** 会话概览面板（右栏默认页）：真实动态数据（后端 metrics 事件 + 实时 turn 兜底） */

/* ------------------------------------------------------------------ */
/* 概览持久化：events 是内存态，重启会清空——最近一次运行结果按会话落盘  */
/* ------------------------------------------------------------------ */

const OVERVIEW_STORE_KEY = 'my-agent-overview-v1';

interface StoredOverview {
  data: AggregatedOverview;
  ts: number;
}

export function loadStoredOverview(sessionId?: string | null): StoredOverview | null {
  if (!sessionId) return null;
  try {
    const all = JSON.parse(localStorage.getItem(OVERVIEW_STORE_KEY) || '{}');
    return all[sessionId] || null;
  } catch { return null; }
}

/** 最近一次运行（任意会话）：当前会话无数据时兜底展示，避免「概览没了」 */
export function latestStoredOverview(): { sid: string; stored: StoredOverview } | null {
  try {
    const all = JSON.parse(localStorage.getItem(OVERVIEW_STORE_KEY) || '{}') as Record<string, StoredOverview>;
    let best: { sid: string; stored: StoredOverview } | null = null;
    for (const [sid, v] of Object.entries(all)) {
      // 同毫秒写入的平局按插入顺序取后写入的（Object.entries 保持插入序）——
      // 严格大于会让同 ms 存储的"最新会话"永远选不中
      if (v && v.data && (!best || (v.ts || 0) >= (best.stored.ts || 0))) best = { sid, stored: v };
    }
    return best;
  } catch { return null; }
}

export function storeOverview(sessionId: string | null | undefined, data: AggregatedOverview) {
  if (!sessionId) return;
  try {
    const all = JSON.parse(localStorage.getItem(OVERVIEW_STORE_KEY) || '{}');
    all[sessionId] = { data, ts: Date.now() };
    // 只保留最近 50 个会话，防无限增长
    const keys = Object.keys(all);
    if (keys.length > 50) {
      keys.sort((a, b) => (all[a].ts || 0) - (all[b].ts || 0));
      for (const k of keys.slice(0, keys.length - 50)) delete all[k];
    }
    localStorage.setItem(OVERVIEW_STORE_KEY, JSON.stringify(all));
  } catch { /* 忽略 */ }
}

function fmtClock(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? `${p(d.getHours())}:${p(d.getMinutes())}`
    : `${d.getMonth() + 1}-${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function SessionOverview() {
  const events = useBackendStore((s) => s.events);
  const running = useBackendStore((s) => s.running);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const agg = useMemo(
    () => aggregateOverview(events, running, activeSessionId),
    [events, running, activeSessionId],
  );
  // 重启/刷新兜底：events 清空后读「上次运行」持久化结果；
  // 当前会话无数据时回退到本机最近一次运行（标注归属会话）
  const [stored, setStored] = useState<{ sid?: string; stored: StoredOverview } | null>(() => {
    const own = loadStoredOverview(activeSessionId);
    return own ? { stored: own } : latestStoredOverview();
  });
  const [storedSid, setStoredSid] = useState<string | null>(activeSessionId);
  if (storedSid !== activeSessionId) {
    setStoredSid(activeSessionId);
    const own = loadStoredOverview(activeSessionId);
    setStored(own ? { stored: own } : latestStoredOverview());
  }
  // 有新数据（且非运行中）→ 立即落盘，供下次启动显示
  useEffect(() => {
    if (agg && !running) storeOverview(activeSessionId, agg);
  }, [agg, running, activeSessionId]);

  const shown = agg ?? stored?.stored?.data ?? null;
  const fromStore = !agg && stored;

  if (!shown) {
    return (
      <div className="ov-root">
        <div className="ov-empty">
          <Activity size={22} />
          <span>当前对话暂无运行数据</span>
          <em>在这个对话里运行任务后，概览会展示该对话自己的 token / 缓存 / 费用统计</em>
        </div>
      </div>
    );
  }

  const total = shown.totalTokens;
  const uncached = Math.max(0, shown.inputTokens - shown.cachedTokens);
  const thresholdTokens = shown.compactThresholdTokens || Math.round(shown.limit * 0.8);
  const data: OverviewData = {
    context: {
      // 窗口水位 = 最近一次请求的输入规模（真水位）。不回退累计 input——
      // 多轮缓存命中下累计会虚高数倍，宁可显示 0 等下一轮 turn_start 纠正
      usedTokens: shown.contextTokens,
      totalTokens: shown.limit,
      // 刻度 = 后端真实压缩阈值（COMPACT_CONFIG），不再是死编码 80%
      compactionThresholdPct: shown.limit > 0 ? (thresholdTokens / shown.limit) * 100 : 80,
    },
    metrics: {
      cacheHitPct: shown.cacheHitPct,
      costYuan: shown.costYuan,
      runtimeMs: shown.runtimeMs,
      requestCount: shown.requestCount,
      totalTokens: total,
      steps: shown.steps || 0,
      llmSeconds: shown.llmSeconds || 0,
    },
    byModel: [{
      model: shown.model || '当前模型',
      role: 'main',
      calls: shown.requestCount,
      tokens: total,
      cacheHitPct: shown.cacheHitPct,
      costYuan: shown.costYuan,
    }],
    byType: [
      { kind: '输入', tokens: uncached, pct: total ? Math.round((uncached / total) * 100) : 0 },
      { kind: '输出', tokens: shown.outputTokens, pct: total ? Math.round((shown.outputTokens / total) * 100) : 0 },
      { kind: '缓存命中', tokens: shown.cachedTokens, pct: total ? Math.round((shown.cachedTokens / total) * 100) : 0 },
    ],
  };
  return (
    <div className="ov-root">
      <div className="ov-live">
        <Activity size={11} />
        {running ? '实时监测中' : fromStore ? `上次运行 · ${fmtClock(stored.stored.ts)}${stored.sid ? ` · ${stored.sid.slice(-10)}` : ''}` : '最近一次运行'}
        {shown.model && <span className="ov-live-model">{shown.model}</span>}
      </div>
      <ContextMonitor ctx={data.context} />
      <MetricsGrid m={data.metrics} />
      <UsageBreakdown data={data} />
      <div className="ov-footnote">费用为按主流价格估算（输入/缓存/输出）· 窗口上限按模型估算</div>
    </div>
  );
}
