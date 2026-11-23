'use client';

import { useRef, useState, type ReactNode } from 'react';
import { Trash2 } from 'lucide-react';

/**
 * iOS 风格左滑行：向右拖动时内容左移，露出右侧红色删除按钮。
 * 松手后：若滑出超过阈值(默认 72px)则吸附在打开态；否则回弹。
 * 点击已打开的行任意处或再次右滑 → 收起。
 */
export function SwipeRow({
  children,
  onDelete,
  deleteWidth = 76,
  className = '',
}: {
  children: ReactNode;
  onDelete: () => void;
  deleteWidth?: number;
  className?: string;
}) {
  const [offset, setOffset] = useState(0); // 0 → 关闭；-deleteWidth → 打开
  const [dragging, setDragging] = useState(false);
  const startX = useRef(0);
  const startY = useRef(0);
  const startOffset = useRef(0);
  const lockDir = useRef<'' | 'h' | 'v'>('');
  const moved = useRef(false);
  const draggingRef = useRef(false);

  const onTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0];
    startX.current = t.clientX;
    startY.current = t.clientY;
    startOffset.current = offset;
    lockDir.current = '';
    moved.current = false;
    draggingRef.current = true;
    setDragging(true);
  };

  const onTouchMove = (e: React.TouchEvent) => {
    if (!draggingRef.current) return;
    const t = e.touches[0];
    const dx = t.clientX - startX.current;
    const dy = t.clientY - startY.current;
    // 首次移动判定方向，纵向留给页面滚动
    if (!lockDir.current) {
      if (Math.abs(dx) > 6 && Math.abs(dx) > Math.abs(dy)) lockDir.current = 'h';
      else if (Math.abs(dy) > 6) lockDir.current = 'v';
    }
    if (lockDir.current === 'v') return;
    if (lockDir.current === 'h') {
      moved.current = true;
      // 只在关闭/打开之间拖动，带阻尼
      let next = startOffset.current + dx;
      next = Math.max(-deleteWidth - 12, Math.min(0, next));
      setOffset(next);
    }
  };

  const onTouchEnd = () => {
    draggingRef.current = false;
    setDragging(false);
    if (lockDir.current === 'h') {
      const open = offset < -deleteWidth / 2;
      setOffset(open ? -deleteWidth : 0);
    }
  };

  const handleClickContent = () => {
    // 已打开时点内容 → 收起；否则交给 children 自身 onClick
    if (offset < 0) {
      setOffset(0);
      return true; // 表示已消费
    }
    return false;
  };

  return (
    <div className={`baoyu-swipe ${className}`}>
      <button
        type="button"
        className="baoyu-swipe-del"
        style={{ width: deleteWidth }}
        onClick={() => {
          onDelete();
          setOffset(0);
        }}
        aria-label="删除"
      >
        <Trash2 size={18} />
      </button>
      <div
        className={`baoyu-swipe-content ${dragging ? 'dragging' : ''}`}
        style={{ transform: `translate3d(${offset}px,0,0)` }}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        onClickCapture={(e) => {
          if (handleClickContent()) {
            e.stopPropagation();
            e.preventDefault();
          }
        }}
      >
        {children}
      </div>
    </div>
  );
}
