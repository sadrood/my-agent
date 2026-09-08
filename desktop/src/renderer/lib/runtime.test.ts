/**
 * RuntimePane.summarizeRun 纯函数测试：事件流 → 运行聚合信息。
 */
import { describe, expect, it } from 'vitest';
import { summarizeRun } from '../components/RuntimePane';

describe('summarizeRun', () => {
  it('解析 turn_start 状态文本中的 token 计数与策略', () => {
    const events = [
      { type: 'turn_start', data: { turn: 2, status_text: '轮 2 · ↑1.2K ↓340 tok · 缓存 45% · 沙箱 workspace-write · 策略 never' } },
    ];
    const agg = summarizeRun(events);
    expect(agg.maxTurn).toBe(2);
    expect(agg.inputTokens).toBe(1200);
    expect(agg.outputTokens).toBe(340);
    expect(agg.cachedTokens).toBe(45);
    expect(agg.policy).toBe('never');
    expect(agg.sandbox).toBe('workspace-write');
  });

  it('统计工具调用成功/失败并维护时间线', () => {
    const events = [
      { type: 'tool_call', data: { tool: 'file' } },
      { type: 'tool_call', data: { tool: 'python' } },
      { type: 'tool_result', data: { tool: 'file', success: true } },
      { type: 'tool_result', data: { tool: 'python', success: false } },
    ];
    const agg = summarizeRun(events);
    expect(agg.toolsOk).toBe(1);
    expect(agg.toolsFail).toBe(1);
    expect(agg.toolLog).toHaveLength(2);
    expect(agg.toolLog[0].ok).toBe(true);
    expect(agg.toolLog[1].ok).toBe(false);
  });

  it('忽略 think 伪工具', () => {
    const events = [
      { type: 'tool_call', data: { tool: 'think' } },
      { type: 'tool_call', data: { tool: 'terminal' } },
    ];
    const agg = summarizeRun(events);
    expect(agg.toolLog).toHaveLength(1);
    expect(agg.toolLog[0].name).toBe('terminal');
  });

  it('空事件流返回空聚合', () => {
    const agg = summarizeRun([]);
    expect(agg.maxTurn).toBe(0);
    expect(agg.toolsOk).toBe(0);
    expect(agg.toolLog).toHaveLength(0);
  });
});
