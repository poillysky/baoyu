'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import type { ReactNode } from 'react';

/**
 * 轻量 toast：iOS 风格顶部提示，不依赖外部库。
 */
let toastTimer: ReturnType<typeof setTimeout> | null = null;

export function showToast(text: string, type: 'success' | 'error' | 'info' = 'success') {
  if (typeof document === 'undefined') return;
  let el = document.getElementById('baoyu-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'baoyu-toast';
    document.body.appendChild(el);
  }
  el.className = `baoyu-toast baoyu-toast--${type}`;
  el.textContent = text;
  // 强制重排，确保重复调用时过渡动画重播
  void el.offsetWidth;
  el.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el?.classList.remove('show'), 1800);
}

/** 万/亿 格式化热度 */
export function fmtCount(n: number | string | undefined | null): string {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v >= 100000000) return (v / 100000000).toFixed(1).replace(/\.0$/, '') + '亿';
  if (v >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, '') + '万';
  return String(v);
}

/** 字节格式化 */
export function fmtBytes(n: number | string | undefined | null): string {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(v) / Math.log(1024)), units.length - 1);
  return (v / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + units[i];
}

export function fmtTime(ts: number | string | undefined | null): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const ms = n < 1e12 ? n * 1000 : n;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (x: number) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtDate(ts: number | string | undefined | null): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const ms = n < 1e12 ? n * 1000 : n;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (x: number) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 简短日期：今天=时间(HH:MM)，昨天=昨天，今年=MM-DD，跨年=YY-MM-DD */
export function fmtDay(ts: number | string | undefined | null): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  const ms = n < 1e12 ? n * 1000 : n;
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '';
  const p = (x: number) => String(x).padStart(2, '0');
  const now = new Date();
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (sameDay(d, yest)) return '昨天';
  if (d.getFullYear() === now.getFullYear()) return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return `${String(d.getFullYear()).slice(2)}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export const ROOM_TYPE: Record<number, string> = {
  0: '免费',
  1: '门票',
  2: '计时',
  3: '密码',
  4: '等级',
};

/**
 * 头像/封面组件：走 /api/img 代理加载，失败自动回退到「首字占位」色块。
 * name 用于取首字 + 配色（字符 hash 取色，稳定不闪）。
 */
export function Avatar({
  name,
  src,
  size = 40,
  className = '',
}: {
  name: string;
  src?: string;
  size?: number;
  className?: string;
}) {
  const [err, setErr] = useState(false);
  const url = src && !err ? src : '';

  const initial = (() => {
    const s = (name || '').trim();
    if (!s) return '?';
    // 取首个字母/数字/汉字
    const m = s.match(/[A-Za-z0-9]|[\u4e00-\u9fa5]/);
    return m ? m[0].toUpperCase() : '?';
  })();

  const bg = (() => {
    let h = 0;
    for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    const hue = h % 360;
    return `hsl(${hue} 55% 45%)`;
  })();

  if (!url) {
    return (
      <span
        className={`baoyu-avatar baoyu-avatar--fallback ${className}`}
        style={{ width: size, height: size, background: bg }}
      >
        {initial}
      </span>
    );
  }

  return (
    <span
      className={`baoyu-avatar ${className}`}
      style={{ width: size, height: size, background: bg }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={name}
        loading="lazy"
        onError={() => setErr(true)}
        style={{ width: size, height: size }}
      />
    </span>
  );
}

/** 通用 Section 卡片包装 */
export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={`baoyu-card ${className || ''}`}>{children}</div>;
}

/**
 * 简易下拉刷新：作用于滚动容器，顶部已到 0 且下拉超阈值时触发。
 */
export function usePullRefresh(
  scrollRef: React.RefObject<HTMLElement | null>,
  onRefresh: () => Promise<void> | void,
) {
  const startY = useRef<number | null>(null);
  const [pulling, setPulling] = useState(0);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onTouchStart = (e: TouchEvent) => {
      if (el.scrollTop <= 0) startY.current = e.touches[0].clientY;
    };
    const onTouchMove = (e: TouchEvent) => {
      if (startY.current === null) return;
      const dy = e.touches[0].clientY - startY.current;
      if (dy > 0 && el.scrollTop <= 0) {
        setPulling(Math.min(dy * 0.45, 90));
      }
    };
    const onTouchEnd = async () => {
      if (startY.current !== null && pulling > 56) {
        startY.current = null;
        setPulling(0);
        await onRefresh();
      } else {
        startY.current = null;
        setPulling(0);
      }
    };
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchmove', onTouchMove, { passive: true });
    el.addEventListener('touchend', onTouchEnd, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onTouchStart);
      el.removeEventListener('touchmove', onTouchMove);
      el.removeEventListener('touchend', onTouchEnd);
    };
  }, [scrollRef, onRefresh, pulling]);

  return pulling;
}

/** 滚动到底加载更多 */
export function useInfiniteScroll(
  scrollRef: React.RefObject<HTMLElement | null>,
  onLoadMore: () => void,
  hasMore: boolean,
) {
  const cb = useRef(onLoadMore);
  cb.current = onLoadMore;
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 120) {
        cb.current();
      }
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [scrollRef, hasMore]);
}
