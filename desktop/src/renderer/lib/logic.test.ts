/**
 * 前端纯逻辑冒烟测试（vitest，jsdom 环境）：
 * 只测不依赖 UI 渲染的纯函数——工具命令格式化、run_end 文案、元素树文本提取、键名解析。
 */
import { describe, expect, it } from 'vitest';
import { formatToolCall, statusText, turnStartView, skillsMatchedView, __setRunSessionId, __eventSessionId } from './backend';
import { useSessionStore } from '../store';
import { extractText } from '../components/ChatFlow';
import { normalizeCopiedText } from './clipboard';

describe('formatToolCall', () => {
  it('terminal/python 取 command/code；字符串 args 原样返回（demo 兼容）', () => {
    expect(formatToolCall('terminal', { command: 'pytest -q' })).toBe('pytest -q');
    expect(formatToolCall('python', { code: 'print(1)' })).toBe('print(1)');
    expect(formatToolCall('python', 'print(2)')).toBe('print(2)'); // demo 把 input 字符串直接当 args
  });

  it('file 输出 操作+路径，edit 只给路径（避免与行首工具名重复）', () => {
    expect(formatToolCall('file', { operation: 'write', path: 'a.txt' })).toBe('write a.txt');
    expect(formatToolCall('edit', { file_path: 'src/x.py' })).toBe('src/x.py');
    expect(formatToolCall('edit', {})).toBe('');
  });

  it('think 返回空串（思考卡不显示命令行）', () => {
    expect(formatToolCall('think', { anything: 1 })).toBe('');
  });

  it('未知工具回退为 工具名+参数 JSON 摘要', () => {
    const out = formatToolCall('mcp_x', { a: 1 });
    expect(out.startsWith('mcp_x ')).toBe(true);
    expect(out).toContain('"a"');
  });
});

describe('statusText', () => {
  it('三种结束状态', () => {
    expect(statusText({ status: 'completed' })).toContain('任务完成');
    expect(statusText({ status: 'stopped' })).toContain('已停止');
    expect(statusText({ status: 'failed', error: '炸了' })).toContain('炸了');
  });
});

describe('turnStartView', () => {
  it('正常载荷 → 轮次/总轮/聚合文本', () => {
    expect(turnStartView({ turn: 3, max_ops: 80, status_text: '轮 3 · 沙箱 workspace-write' })).toEqual({
      turn: 3, maxOps: 80, statusText: '轮 3 · 沙箱 workspace-write',
    });
  });

  it('max_ops 缺失/非法 → 0（无限轮次语义）', () => {
    expect(turnStartView({ turn: 1 })).toEqual({ turn: 1, maxOps: 0, statusText: '' });
    expect(turnStartView({ turn: 1, max_ops: -5 })).toEqual({ turn: 1, maxOps: 0, statusText: '' });
  });

  it('无效载荷 → null（不渲染状态条）', () => {
    expect(turnStartView(null)).toBeNull();
    expect(turnStartView({})).toBeNull();
    expect(turnStartView({ turn: 0 })).toBeNull();
    expect(turnStartView({ turn: 'abc' })).toBeNull();
  });
});

describe('skillsMatchedView', () => {
  it('命中列表 → 技能名数组 + 目标', () => {
    expect(skillsMatchedView({ skills: ['excel', 'pdf'], goal: '生成 Excel 报表' })).toEqual({
      skills: ['excel', 'pdf'], goal: '生成 Excel 报表',
    });
  });

  it('过滤空串/非字符串元素', () => {
    expect(skillsMatchedView({ skills: ['excel', '', 42, null] })).toEqual({
      skills: ['excel', '42'], goal: '',
    });
  });

  it('无命中/非法载荷 → null（不展示提示）', () => {
    expect(skillsMatchedView(null)).toBeNull();
    expect(skillsMatchedView({})).toBeNull();
    expect(skillsMatchedView({ skills: [] })).toBeNull();
    expect(skillsMatchedView({ skills: 'excel' })).toBeNull(); // 非数组
  });
});

describe('extractText', () => {
  it('扁平字符串/数字', () => {
    expect(extractText('abc')).toBe('abc');
    expect(extractText(['a', 1, 'b'])).toBe('a1b');
  });

  it('嵌套 React 元素（模拟高亮 span 树）', () => {
    const span = { props: { children: ['const x = ', { props: { children: '1' } }] } };
    expect(extractText(span)).toBe('const x = 1');
  });

  it('空值安全', () => {
    expect(extractText(null)).toBe('');
    expect(extractText(undefined)).toBe('');
    expect(extractText({})).toBe('');
  });
});

describe('任务归属会话（多会话隔离）', () => {
  it('任务进行中切换会话：事件仍落到发起任务的会话', () => {
    const a = useSessionStore.getState().createSession('A');
    const b = useSessionStore.getState().createSession('B');
    useSessionStore.getState().selectSession(a.id);
    __setRunSessionId(a.id);
    // 用户切到 B：事件不能跟着跑过去（否则 A 的提问/回答会串进 B）
    useSessionStore.getState().selectSession(b.id);
    expect(__eventSessionId()).toBe(a.id);
    // 任务结束后解锁 → 回到当前选中会话
    __setRunSessionId(null);
    expect(__eventSessionId()).toBe(b.id);
  });
});

describe('normalizeCopiedText', () => {
  it('折叠 3+ 连续换行（两行以上空白 → 一个空行）', () => {
    expect(normalizeCopiedText('a\n\n\n\nb')).toBe('a\n\nb');
    expect(normalizeCopiedText('a\n\n\nb')).toBe('a\n\nb');
  });

  it('保留正常段落空行', () => {
    expect(normalizeCopiedText('a\n\nb')).toBe('a\n\nb');
    expect(normalizeCopiedText('a\nb')).toBe('a\nb');
  });

  it('去掉行尾空白与末尾换行', () => {
    expect(normalizeCopiedText('a  \nb\t\n\n\n')).toBe('a\nb');
    expect(normalizeCopiedText('正文。')).toBe('正文。');
  });
});
