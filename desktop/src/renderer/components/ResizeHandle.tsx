/**
 * 面板宽度拖拽分割线：贴在左/右栏内侧边缘，拖动调节该栏宽度。
 * 宽度实时写入 UI store（持久化，重启保留）；双击恢复默认宽度。
 */
import React, { useCallback, useRef } from 'react';
import { PANEL_DEFAULT_WIDTHS, useUIStore } from '../store';

export function ResizeHandle({ side }: { side: 'left' | 'right' }) {
  const draggingRef = useRef(false);
  const setWidth = useUIStore(
    side === 'left' ? (s) => s.setLeftWidth : (s) => s.setRightWidth,
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.button !== 0 || draggingRef.current) return;
      e.preventDefault();
      draggingRef.current = true;
      // 拖拽期间禁用文本选择，避免鼠标划过聊天区时乱选中文本
      document.body.classList.add('panel-resizing');
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);

      const onMove = (ev: PointerEvent) => {
        if (!draggingRef.current) return;
        const w = side === 'left' ? ev.clientX : window.innerWidth - ev.clientX;
        setWidth(w);
      };
      const cleanup = () => {
        if (!draggingRef.current) return;
        draggingRef.current = false;
        document.body.classList.remove('panel-resizing');
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onCancel);
      };
      const onUp = () => cleanup();
      const onCancel = () => cleanup();
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onCancel);
      // 兜底：指针捕获意外丢失（窗口失焦/系统手势中断）时同样清理，避免拖拽卡死
      (e.currentTarget as HTMLElement).addEventListener('lostpointercapture', cleanup, { once: true });
    },
    [side, setWidth],
  );

  const onDoubleClick = useCallback(() => {
    setWidth(PANEL_DEFAULT_WIDTHS[side]);
  }, [side, setWidth]);

  return (
    <div
      className={`resize-handle resize-${side}`}
      title="拖拽调节宽度（双击恢复默认）"
      onPointerDown={onPointerDown}
      onDoubleClick={onDoubleClick}
    />
  );
}
