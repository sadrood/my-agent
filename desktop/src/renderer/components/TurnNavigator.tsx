/**
 * 「轮次导航」（turn navigator，对齐主流实现的 turnNavigator）。
 *
 * 贴对话流左侧的窄竖条：一次提问 = 一个 12×2px 小横杠，按紧凑等距槽位
 * （10px/条，紧凑槽位）排列——短会话全部常显且紧凑；
 * 提问很多时轨道内部同比例跟随聊天滚动（导航条跟随聊天滚动）。
 * - 悬停横杠：右侧弹出该轮问答预览卡（问题 2 行 + 回答 3 行）
 * - 点击横杠：先解除「吸底」再平滑定位到对应消息并闪烁高亮
 * - 视口顶部附近的提问为「当前轮次」（加亮）；运行中提问以强调色脉冲
 * - 竖条/横杠/预览卡出现均有动画
 *
 * 性能：滚动只写 inner 容器的 transform 与 active（直接 DOM，不重渲染）。
 */
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { RefObject } from 'react';
import type { ChatMessage } from '../lib/types';

/** 对话区宽度低于此值时不显示（参照主流实现 @min-[864px] 断点思路，取更宽松的 760） */
const MIN_WIDTH = 760;
/** 预览文案最长长度 */
const PREVIEW_MAX = 220;
/** 紧凑槽位：每个提问占高（px，参照主流实现 h-2.5 槽位） */
const SLOT_H = 10;
/** 轨道上下避让量（px）：max 可视列高 = 视口高 - 该值 */
const RAIL_VGAP = 64;

interface TurnUnit {
  msgId: string;
  /** 该提问在 messages 数组中的下标（懒加载扩窗用） */
  msgIndex: number;
  question: string;
  answer: string;
}

interface Tip {
  i: number;
  x: number;
  y: number;
}

/** 轻量清理 markdown/空白，产出可读的单行预览 */
function cleanPreview(raw: string, max = PREVIEW_MAX): string {
  const t = (raw || '')
    .replace(/```[\s\S]*?```/g, ' [代码] ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#>*_~|]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return t.length > max ? t.slice(0, max).trimEnd() + '…' : t;
}

interface Props {
  messages: ChatMessage[];
  /** 对话流滚动容器（.chat-flow） */
  scrollerRef: RefObject<HTMLDivElement | null>;
  /** 是否正在本会话运行（最后一条提问高亮为「运行中」） */
  runningHere: boolean;
  /** 懒加载窗口未覆盖到目标消息时扩窗；总是先调用以解除「吸底」 */
  onNeedMore: (msgIndex: number) => void;
}

export function TurnNavigator({ messages, scrollerRef, runningHere, onNeedMore }: Props) {
  const [tip, setTip] = useState<Tip | null>(null);
  const [activeIdx, setActiveIdx] = useState(-1);
  const [geo, setGeo] = useState<{ left: number; vh: number } | null>(null);
  const activeRef = useRef(-1);
  activeRef.current = activeIdx;
  const innerRef = useRef<HTMLDivElement>(null);

  /** 一次用户提问 = 一个轮次；answer 取紧随其后的第一条助手文本 */
  const units = useMemo<TurnUnit[]>(() => {
    const res: TurnUnit[] = [];
    for (let i = 0; i < messages.length; i++) {
      const m = messages[i];
      if (m.role !== 'user') continue;
      const q = cleanPreview(m.content);
      if (!q) continue;
      let answer = '';
      for (let j = i + 1; j < messages.length; j++) {
        const a = messages[j];
        if (a.role === 'user') break;
        if (a.role === 'assistant' && (a.content || '').trim()) {
          answer = cleanPreview(a.content);
          break;
        }
      }
      res.push({ msgId: m.id, msgIndex: i, question: q, answer });
    }
    return res;
  }, [messages]);
  const unitsRef = useRef(units);
  unitsRef.current = units;

  /**
   * 轨道内列跟随聊天滚动（同比例）：
   * inner 总高 = units × SLOT；可视列高 = min(视口-RAIL_VGAP, 总高)，
   * 聊天滚到底时内列也滚到底；提问不多时整列垂直居中（紧凑短条）。
   */
  const syncRail = useCallback(() => {
    const sc = scrollerRef.current;
    const inner = innerRef.current;
    if (!sc || !inner) return;
    const vh = sc.getBoundingClientRect().height;
    const total = unitsRef.current.length * SLOT_H;
    const colH = Math.min(Math.max(40, vh - RAIL_VGAP), total);
    let ratio = 0;
    const scrollable = sc.scrollHeight - sc.clientHeight;
    if (scrollable > 0) {
      ratio = Math.min(1, Math.max(0, sc.scrollTop / scrollable));
    }
    const off = total > colH ? (total - colH) * ratio : 0;
    const y = (vh - colH) / 2 - off;
    inner.style.transform = `translateY(${y}px)`;
  }, [scrollerRef]);

  // 测量容器并初始化；尺寸变化时重算
  useLayoutEffect(() => {
    const sc = scrollerRef.current;
    if (!sc) return;
    const measure = () => {
      if (sc.clientWidth >= MIN_WIDTH) {
        setGeo({ left: Math.max(6, sc.offsetLeft - 40), vh: sc.clientHeight });
      } else {
        setGeo((g) => (g ? { ...g, vh: sc.clientHeight } : null));
      }
      syncRail();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(sc);
    return () => ro.disconnect();
  }, [scrollerRef, syncRail]);

  // 滚动 → 更新「当前轮次」+ 同步内列
  useEffect(() => {
    const sc = scrollerRef.current;
    if (!sc) return;
    const onScroll = () => {
      setTip(null);
      const unitsNow = unitsRef.current;
      if (unitsNow.length) {
        const rect = sc.getBoundingClientRect();
        const nearTop = rect.top + 64;
        const byId = new Map(unitsNow.map((u, i) => [u.msgId, i]));
        let act = 0;
        sc.querySelectorAll<HTMLElement>('[data-mid]').forEach((el) => {
          const i = byId.get(el.dataset.mid || '');
          if (i === undefined) return;
          if (el.getBoundingClientRect().top <= nearTop) act = i;
        });
        // 已滚到底：当前轮次 = 最后一条提问
        if (sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4) act = unitsNow.length - 1;
        if (act !== activeRef.current) setActiveIdx(act);
      }
      syncRail();
    };
    sc.addEventListener('scroll', onScroll, { passive: true });
    return () => sc.removeEventListener('scroll', onScroll);
  }, [scrollerRef, syncRail]);

  // 消息/geo 就绪后初始化 active 与内列位置（新 DOM 无 scroll 事件）
  useEffect(() => {
    if (!geo) return;
    const sc = scrollerRef.current;
    const t = window.setTimeout(() => {
      syncRail();
      sc?.dispatchEvent(new Event('scroll'));
    }, 40);
    return () => window.clearTimeout(t);
  }, [units.length, geo, syncRail, scrollerRef]);

  if (!geo || geo.left < 0 || !units.length || geo.vh < 60) return null;

  const jumpTo = (u: TurnUnit, attempt = 0) => {
    const sc = scrollerRef.current;
    if (!sc) return;
    // 先解除「吸底」并扩窗（若目标在懒加载窗口外）；
    // 注意 setState 异步生效——滚动必须等 React 提交后再执行，
    // 否则 pinned=true 的吸底 effect 会把平滑滚动抢回底部。
    onNeedMore(u.msgIndex);
    const el = sc.querySelector<HTMLElement>(`[data-mid="${u.msgId}"]`);
    if (!el) {
      // 目标在懒加载窗口外：扩窗后重试（最多 ~2s，等旧消息渲染完成）
      if (attempt < 12) window.setTimeout(() => jumpTo(u, attempt + 1), 160);
      return;
    }
    // 程序化跳转期间禁止吸底重入（跳转起步时距底 <80px 会触发吸底抢回）
    sc.dataset.jumpLock = '1';
    window.setTimeout(() => {
      const el2 = sc.querySelector<HTMLElement>(`[data-mid="${u.msgId}"]`);
      if (!el2) {
        delete sc.dataset.jumpLock;
        return;
      }
      // center：把该提问滚到视口中部（位移明显，且底部视口内的条目也能被看到滚动）
      el2.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el2.classList.add('nav-flash');
      window.setTimeout(() => el2.classList.remove('nav-flash'), 1700);
      // 平滑动画通常 <1s；结束后解除锁，恢复正常的吸底判定
      window.setTimeout(() => { delete sc.dataset.jumpLock; }, 1400);
    }, 60);
  };

  const tipFrom = (el: HTMLElement, i: number) => {
    const r = el.getBoundingClientRect();
    const x = Math.min(r.right + 10, window.innerWidth - 330);
    const y = Math.max(8, Math.min(r.top - 6, window.innerHeight - 110));
    setTip({ i, x, y });
  };

  return (
    <nav
      className="turn-nav"
      role="navigation"
      aria-label="对话轮次导航（点击跳到对应提问）"
      style={{ left: geo.left, height: geo.vh }}
    >
      <div ref={innerRef} className="turn-nav-inner" style={{ height: units.length * SLOT_H }}>
        {units.map((u, i) => (
          <button
            key={u.msgId}
            type="button"
            className={`tn-slot${i === activeIdx ? ' active' : ''}`}
            style={{ top: i * SLOT_H }}
            data-running={runningHere && i === units.length - 1 ? 'true' : undefined}
            aria-label={`跳到第 ${i + 1} 个提问`}
            aria-current={i === activeIdx ? 'location' : undefined}
            onMouseEnter={(e) => tipFrom(e.currentTarget, i)}
            onMouseLeave={() => setTip(null)}
            onFocus={(e) => tipFrom(e.currentTarget, i)}
            onBlur={() => setTip(null)}
            onClick={() => {
              setTip(null);
              // 立即高亮目标（不等滚动事件），点击即有反馈
              activeRef.current = i;
              setActiveIdx(i);
              jumpTo(u);
            }}
          >
            <span className="tn-dash" />
          </button>
        ))}
      </div>
      {tip && units[tip.i] && (
        <div className="turn-nav-tip" style={{ left: tip.x, top: tip.y }}>
          <div className="tt-q">{units[tip.i].question}</div>
          {units[tip.i].answer && <div className="tt-a">{units[tip.i].answer}</div>}
        </div>
      )}
    </nav>
  );
}
