'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { Play, Radar, Search } from 'lucide-react';
import { fetchRooms, addWatch, fetchWatchList, imgUrl, type Room, type WatchItem } from '@/lib/api';
import { ROOM_TYPE, fmtCount, showToast, Avatar } from '../ui';
import { AppPush } from '@/components/ui/AppPush';

const PAGE = 40;

/**
 * 房间列表 — 点进某个平台后的主播列表（一行一个，监控 + 播放双按钮）。
 * 用 sehua 通用子页组件 AppPush（返回头 + 独立滚动 body + 边缘滑返）。
 */
export function RoomsScreen({
  pid,
  name,
  onBack,
  onPlay,
}: {
  pid: number;
  name: string;
  onBack: () => void;
  onPlay: (r: Room) => void;
}) {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [all, setAll] = useState<Room[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [watch, setWatch] = useState<WatchItem[]>([]);
  const [page, setPage] = useState(1);
  const [kw, setKw] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetchRooms(pid);
      const list = r.data;
      setAll(list);
      setTotal(r.total || list.length);
      setRooms(list.slice(0, PAGE));
      setPage(1);
    } catch {
      showToast('房间加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [pid]);

  useEffect(() => {
    void loadAll();
    fetchWatchList().then(setWatch).catch(() => {});
  }, [loadAll]);

  const loadMore = useCallback(() => {
    setPage((prev) => {
      const next = prev + 1;
      setRooms(all.slice(0, next * PAGE));
      return next;
    });
  }, [all]);

  const hasMore = rooms.length < all.length;

  // 底部哨兵 + IntersectionObserver：进入视口即加载下一页。
  // 不依赖具体滚动容器（AppPush 的 .app-push__body 才是滚动父级，list 自身不滚）。
  const sentinelRef = useRef<HTMLDivElement>(null);
  const loadingMoreRef = useRef(false);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore) return;
    const root = (el.closest('.app-push__body') as Element) || null;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !loadingMoreRef.current) {
          loadingMoreRef.current = true;
          loadMore();
          // 本地切片加载瞬时完成，无需时间锁；直接解锁，下一轮相交可继续
          loadingMoreRef.current = false;
        }
      },
      { root, rootMargin: '200px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, loadMore, rooms.length]);

  const isWatched = (rid: string) => watch.some((w) => w.room_id === rid);

  // 本地搜索过滤（不重新请求后端，过滤 name/title/room_id）
  const kwTrim = kw.trim().toLowerCase();
  const filtered = kwTrim
    ? all.filter((r) => {
        const rid = String(r.room_id);
        const nm = (r.name || r.title || '').toLowerCase();
        return nm.includes(kwTrim) || rid.includes(kwTrim);
      })
    : all;
  // 搜索时展示过滤结果的前 PAGE 条；否则用增量分页的 rooms
  const visible = kwTrim ? filtered.slice(0, PAGE) : rooms;
  const showMore = !kwTrim && hasMore;

  const onWatch = async (r: Room) => {
    const rid = String(r.room_id);
    try {
      await addWatch(rid, pid, r.name || r.title || rid);
      showToast('已加入监控', 'success');
      setWatch((w) => [...w, { room_id: rid, name: r.name || '', pid } as WatchItem]);
    } catch {
      showToast('添加监控失败', 'error');
    }
  };

  return (
    <AppPush title={`${name} · ${total} 房`} onBack={onBack} scrollKey={`rooms-${pid}`} scrollMode="top">
      {loading ? (
        <div className="baoyu-list">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="baoyu-row skeleton" />
          ))}
        </div>
      ) : (
        <div className="baoyu-list" ref={scrollRef}>
          <div className="baoyu-search">
            <Search size={16} />
            <input
              value={kw}
              onChange={(e) => setKw(e.target.value)}
              placeholder="搜索主播名字 / 房间号"
            />
            {kw && (
              <button type="button" className="baoyu-search-clear" onClick={() => setKw('')}>
                清除
              </button>
            )}
          </div>
          {visible.map((r) => {
            const rid = String(r.room_id);
            const watched = isWatched(rid);
            return (
              <div key={rid} className="baoyu-row">
                <Avatar name={r.name || r.title || rid} src={imgUrl(r.cover)} size={40} />
                <div className="baoyu-row-main">
                  <span className="baoyu-row-name">{r.name || r.title || rid}</span>
                  <span className="baoyu-row-meta">
                    {r.online === 1 ? <i className="dot-on" /> : <i className="dot-off" />}
                    {fmtCount(r.view_count)} · {ROOM_TYPE[r.type ?? 0] ?? '免费'}
                  </span>
                </div>
                <div className="baoyu-row-acts">
                  <button
                    type="button"
                    className={`baoyu-act ${watched ? 'on' : ''}`}
                    onClick={() => onWatch(r)}
                    aria-label={watched ? '已监控' : '监控'}
                  >
                    <Radar size={15} />
                  </button>
                  <button
                    type="button"
                    className="baoyu-act play"
                    onClick={() => onPlay(r)}
                    aria-label="播放"
                  >
                    <Play size={15} />
                  </button>
                </div>
              </div>
            );
          })}
          {showMore && (
            <div className="baoyu-more" ref={sentinelRef}>
              已显示 {rooms.length} / {total}，上滑加载更多
            </div>
          )}
          {kwTrim && visible.length === 0 && (
            <div className="baoyu-empty">没有匹配的主播</div>
          )}
        </div>
      )}
    </AppPush>
  );
}
