/**
 * 工具行配对：同名批次必须按 call_id 精确配对，不能靠"最近一条同名"猜。
 *
 * 历史问题：同批两条 terminal 时，后端把整批 header 一次性发出来，
 * 而"从后往前找最近一条同名未完成"的规则会把后续的流式输出与结果
 * 全部贴到最后一张卡片上 —— 结果与命令完全错位。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __applyToolEvent, __setRunSessionId, eventToMessage } from './backend';
import { useSessionStore } from '../store';
import type { ChatMessage } from './types';

const SID = 'sess-pairing';

/** 造两条同名 terminal 的调用卡片；withId=false 模拟旧后端（事件里没 call_id） */
function seed(calls: Array<{ id: string; cmd: string }>, withId = true): void {
  useSessionStore.setState({ messages: { [SID]: [] } });
  __setRunSessionId(SID);
  for (const c of calls) {
    const msg = eventToMessage({
      type: 'tool_call',
      data: {
        tool: 'terminal',
        arguments: { command: c.cmd },
        ...(withId ? { call_id: c.id } : {}),
      },
      timestamp: Date.now(),
    });
    if (msg) useSessionStore.getState().addMessage(SID, msg);
  }
}

const rows = (): ChatMessage[] => useSessionStore.getState().messages[SID] || [];

beforeEach(() => {
  useSessionStore.setState({ messages: {}, activeSessionId: null });
  __setRunSessionId(null);
});

describe('tool_call 卡片', () => {
  it('把事件里的 call_id 存进 meta', () => {
    seed([{ id: 'c1', cmd: 'dir /b' }]);
    expect((rows()[0].meta as any).callId).toBe('c1');
  });
});

describe('同名批次按 call_id 配对', () => {
  it('两条 terminal 的结果各归各家', () => {
    seed([{ id: 'c1', cmd: 'dir /b' }, { id: 'c2', cmd: 'dir /b skills' }]);
    __applyToolEvent('tool_result',
      { tool: 'terminal', call_id: 'c2', success: true, output: 'skills 的输出' });
    __applyToolEvent('tool_result',
      { tool: 'terminal', call_id: 'c1', success: true, output: 'root 的输出' });
    const r = rows();
    expect(r[0].content).toBe('root 的输出');
    expect(r[1].content).toBe('skills 的输出');
    expect((r[0].meta as any).success).toBe(true);
    expect((r[1].meta as any).success).toBe(true);
  });

  it('流式输出也只追加到 call_id 相符的那张卡', () => {
    seed([{ id: 'c1', cmd: 'dir /b' }, { id: 'c2', cmd: 'dir /b skills' }]);
    __applyToolEvent('tool_output', { tool: 'terminal', call_id: 'c1', text: 'AAA' });
    __applyToolEvent('tool_output', { tool: 'terminal', call_id: 'c2', text: 'BBB' });
    const r = rows();
    expect(r[0].content).toBe('AAA');
    expect(r[1].content).toBe('BBB');
  });

  it('乱序到达也能配对（并行批先完成先回）', () => {
    seed([{ id: 'c1', cmd: 'a' }, { id: 'c2', cmd: 'b' }]);
    __applyToolEvent('tool_result',
      { tool: 'terminal', call_id: 'c2', success: true, output: 'B' });
    __applyToolEvent('tool_result',
      { tool: 'terminal', call_id: 'c1', success: true, output: 'A' });
    expect(rows()[0].content).toBe('A');
    expect(rows()[1].content).toBe('B');
  });
});

describe('旧事件流（无 call_id）回退', () => {
  it('仍按工具名配对，不因缺 id 而丢结果', () => {
    seed([{ id: 'c1', cmd: 'dir /b' }], false);
    __applyToolEvent('tool_result', { tool: 'terminal', success: true, output: 'ok' });
    expect(rows()[0].content).toBe('ok');
    expect((rows()[0].meta as any).success).toBe(true);
  });
});
