import { describe, expect, it } from 'vitest';
import { aggregateOverview, storeOverview, loadStoredOverview, latestStoredOverview } from './SessionOverview';
import type { AgentEvent } from '../lib/types';

function ev(type: string, data: Record<string, unknown>, ts = 0): AgentEvent {
  return { type: type as AgentEvent['type'], data, timestamp: ts };
}

describe('aggregateOverview（真实事件流 → 概览统计）', () => {
  it('无任何运行事件时返回 null（空态）', () => {
    expect(aggregateOverview([], false)).toBeNull();
  });

  it('结构化 metrics 事件优先（run_end 后精确值）', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', { turn: 1, status_text: '轮 1 · ↑1.2K ↓300 tok · 缓存 50%' }, 1200),
      ev('run_end', { status: 'completed' }, 5000),
      ev('metrics', {
        line: '2 轮 · 3 步',
        turns: 2,
        steps: 3,
        input_tokens: 18400,
        output_tokens: 4600,
        cached_tokens: 12000,
        cache_hit_rate: 65.22,
        llm_seconds: 12.4,
        model: 'deepseek-chat',
        context_tokens: 13502,
      }, 5100),
    ];
    const a = aggregateOverview(events, false);
    expect(a).not.toBeNull();
    expect(a!.inputTokens).toBe(18400);
    expect(a!.totalTokens).toBe(23000);
    expect(a!.cacheHitPct).toBe(65.22);
    expect(a!.requestCount).toBe(2);
    expect(a!.model).toBe('deepseek-chat');
    expect(a!.runtimeMs).toBe(4000);
    expect(a!.costYuan).toBeGreaterThan(0);
    expect(a!.limit).toBe(1_000_000);
    // 窗口水位 = context_tokens（最近一次请求），而非累计 input（18400）
    expect(a!.contextTokens).toBe(13502);
  });

  it('老后端无 context_tokens：水位回落为最近一次 ↑ 实时值，不取累计', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', { turn: 1, status_text: '轮 1 · ↑5K ↓1K tok · 缓存 40%' }, 1200),
      ev('turn_start', { turn: 2, status_text: '轮 2 · ↑7K ↓2K tok · 缓存 62%' }, 3000),
      ev('run_end', { status: 'completed' }, 4000),
      ev('metrics', { turns: 2, input_tokens: 12000, output_tokens: 3000, cached_tokens: 0, cache_hit_rate: 62 }, 4100),
    ];
    const a = aggregateOverview(events, false);
    expect(a).not.toBeNull();
    // 无 context_tokens 字段：水位=0（宁缺毋滥，不拿累计 12000 冒充窗口水位）
    expect(a!.contextTokens).toBe(0);
  });

  it('运行中：turn_start 结构化字段优先（后端每轮实时下发）', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', {
        turn: 1, status_text: '轮 1 · ↑500 ↓100 tok · 缓存 40%',
        input_tokens: 500, output_tokens: 100, cached_tokens: 200,
        cache_hit_rate: 40, turns: 1, steps: 2,
      }, 1200),
    ];
    const a = aggregateOverview(events, true);
    expect(a).not.toBeNull();
    expect(a!.inputTokens).toBe(500);
    expect(a!.outputTokens).toBe(100);
    expect(a!.cachedTokens).toBe(200);
    expect(a!.cacheHitPct).toBe(40);
  });

  it('运行中：turn_start 携带 context_tokens 时水位取真值（不用累计 ↑ 冒充）', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', {
        turn: 3, status_text: '轮 3 · ↑48K ↓2K tok · 缓存 90%',
        input_tokens: 48000, output_tokens: 2000, cached_tokens: 43000,
        cache_hit_rate: 90, context_tokens: 51000,
        compact_threshold_tokens: 45000,
      }, 1200),
    ];
    const a = aggregateOverview(events, true);
    // 真水位 = context_tokens（51000），不是累计 input（48000）
    expect(a!.contextTokens).toBe(51000);
    expect(a!.compactThresholdTokens).toBe(45000);
  });

  it('运行中：老后端仅 status_text 时其余 KPI 照常刷新，水位宁缺毋滥', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', { turn: 1, status_text: '轮 1 · ↑500 ↓100 tok · 缓存 40%' }, 1200),
      ev('turn_start', { turn: 2, status_text: '轮 2 · ↑1.2K ↓300 tok · 缓存 62%' }, 3000),
    ];
    const a = aggregateOverview(events, true);
    expect(a).not.toBeNull();
    expect(a!.inputTokens).toBe(1200);
    expect(a!.outputTokens).toBe(300);
    expect(a!.requestCount).toBe(2);
    expect(a!.running).toBe(true);
    expect(a!.cacheHitPct).toBe(62);
    expect(a!.runtimeMs).toBeGreaterThan(0); // 相对 run_start 实时增长
    expect(a!.contextTokens).toBe(0); // ↑ 是累计值≠窗口水位，宁缺毋滥（新后端发 context_tokens）
  });

  it('K/M 单位解析', () => {
    const events = [
      ev('run_start', {}, 1000),
      ev('turn_start', { status_text: '轮 1 · ↑1.5M ↓2.3K tok · 缓存 0%' }, 1200),
    ];
    const a = aggregateOverview(events, true);
    expect(a!.inputTokens).toBe(1_500_000);
    expect(a!.outputTokens).toBe(2300);
  });

  it('按会话隔离：只聚合当前会话事件（session_id / side_of），不串台', () => {
    const myRun = [
      ev('run_start', { session_id: 'conv-aaa' }, 1000),
      ev('turn_start', { session_id: 'conv-aaa', status_text: '轮 1 · ↑500 ↓100 tok · 缓存 40%' }, 1200),
      ev('metrics', {
        session_id: 'conv-aaa', turns: 1, input_tokens: 500, output_tokens: 100,
        cached_tokens: 200, cache_hit_rate: 40, context_tokens: 500, model: 'deepseek-chat',
      }, 3000),
    ];
    const otherRun = [
      ev('run_start', { session_id: 'conv-bbb' }, 1000),
      ev('metrics', {
        session_id: 'conv-bbb', turns: 9, input_tokens: 999999, output_tokens: 999999,
        cached_tokens: 0, cache_hit_rate: 0, context_tokens: 999999, model: 'other',
      }, 3000),
    ];
    const a = aggregateOverview([...myRun, ...otherRun], false, 'conv-aaa');
    expect(a).not.toBeNull();
    expect(a!.inputTokens).toBe(500);          // 只含 conv-aaa
    expect(a!.model).toBe('deepseek-chat');
    expect(a!.requestCount).toBe(1);

    const b = aggregateOverview([...myRun, ...otherRun], false, 'conv-bbb');
    expect(b!.inputTokens).toBe(999999);
    expect(b!.model).toBe('other');

    expect(aggregateOverview([...myRun, ...otherRun], false, 'conv-none')).toBeNull();
  });

  it('概览持久化：按会话 store/load 往返（重启后仍显示上次运行）', () => {
    localStorage.removeItem('my-agent-overview-v1');
    const data = {
      model: 'deepseek-v4-flash', inputTokens: 5000, outputTokens: 1200, cachedTokens: 3000,
      cacheHitPct: 60, requestCount: 3, runtimeMs: 9000, costYuan: 0.001, totalTokens: 6200,
      running: false, limit: 1_000_000, contextTokens: 5000, summaryLine: '2 轮 · 3 步 · LLM 12.4s', steps: 3, llmSeconds: 12.4, compactThresholdTokens: 45000,
    };
    storeOverview('conv-persist', data);
    const other = loadStoredOverview('conv-other');
    expect(other).toBeNull();                       // 会话隔离
    const got = loadStoredOverview('conv-persist');
    expect(got).not.toBeNull();
    expect(got!.data.model).toBe('deepseek-v4-flash');
    expect(got!.data.totalTokens).toBe(6200);
    expect(typeof got!.ts).toBe('number');
    localStorage.removeItem('my-agent-overview-v1');
  });

  it('空态回退：当前会话无数据时展示最近一次运行（任意会话）', () => {
    localStorage.removeItem('my-agent-overview-v1');
    const mk = (n: number) => ({
      model: 'deepseek-v4-flash', inputTokens: n, outputTokens: 1, cachedTokens: 0,
      cacheHitPct: 0, requestCount: 1, runtimeMs: 1, costYuan: 0, totalTokens: n + 1,
      running: false, limit: 1_000_000, contextTokens: n, summaryLine: '', steps: 0, llmSeconds: 0, compactThresholdTokens: 45000,
    });
    storeOverview('conv-old', mk(100));
    // 最新写入另一会话
    const newer = { ...mk(999), model: 'my-new' };
    storeOverview('conv-newest', newer);
    const latest = latestStoredOverview();
    expect(latest).not.toBeNull();
    expect(latest!.sid).toBe('conv-newest');
    expect(latest!.stored.data.model).toBe('my-new');
    expect(latest!.stored.data.inputTokens).toBe(999);
    localStorage.removeItem('my-agent-overview-v1');
  });
});

  it('同一会话多次发送：会话累计不随新 run 重置（回归）', () => {
    const sid = 'conv-multi';
    const mk = (s: string, in_: number, out: number, steps: number, sec: number) =>
      ev('metrics', { session_id: sid, turns: 1, input_tokens: in_, output_tokens: out,
                      cached_tokens: 0, cache_hit_rate: 0, steps, llm_seconds: sec,
                      model: 'deepseek-v4-flash', context_tokens: in_ });
    const events = [
      ev('run_start', { session_id: sid }, 1000),
      ev('turn_start', { session_id: sid, status_text: '轮 1 · ↑0 ↓0 tok' }, 1200),
      ev('run_end', { status: 'completed', session_id: sid }, 3000),
      mk(sid, 8000, 300, 4, 10),        // 第 1 次发送：8K in / 300 out
      ev('run_start', { session_id: sid }, 4000),
      ev('turn_start', { session_id: sid, status_text: '轮 1 · ↑0 ↓0 tok' }, 4200),
      ev('run_end', { status: 'completed', session_id: sid }, 6000),
      mk(sid, 6000, 200, 3, 8),         // 第 2 次发送：6K in / 200 out
    ];
    const a = aggregateOverview(events, false, sid);
    expect(a).not.toBeNull();
    // 会话累计 = 两次运行之和（而非第二次覆盖第一次）
    expect(a!.inputTokens).toBe(14000);
    expect(a!.outputTokens).toBe(500);
    expect(a!.steps).toBe(7);
    expect(a!.llmSeconds).toBe(18);
    expect(a!.requestCount).toBe(2);
    expect(a!.contextTokens).toBe(6000); // 窗口水位 = 最近一次
  });

  it('裁剪窗口：只剩 metrics 缺 run_start 时也能聚合（回归：缓存率外全 0）', () => {
    const sid = 'conv-clipped';
    const events = [
      ev('metrics', { session_id: sid, turns: 3, input_tokens: 25000, output_tokens: 3000,
                      cached_tokens: 22000, cache_hit_rate: 88.0, steps: 6, llm_seconds: 42,
                      model: 'deepseek-v4-flash', context_tokens: 9000 }),
    ];
    const a = aggregateOverview(events, false, sid);
    expect(a).not.toBeNull();
    expect(a!.inputTokens).toBe(25000);
    expect(a!.outputTokens).toBe(3000);
    expect(a!.cachedTokens).toBe(22000);
    expect(a!.steps).toBe(6);
    expect(a!.llmSeconds).toBe(42);
    expect(a!.requestCount).toBe(3);
    expect(a!.contextTokens).toBe(9000);
  });
