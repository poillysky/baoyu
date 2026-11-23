'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import {
  fetchWatchList,
  removeWatch,
  fetchPlatforms,
  addWatch,
  type WatchItem,
  type Platform,
} from '@/lib/api';
import { showToast } from '../ui';
import { useTabNavigation } from '@/shell/TabContext';

/**
 * 监控台 — 从列表添加 / 手动配置名字添加，显示在线·录制状态，可删除。
 */
export function WatchScreen() {
  const [items, setItems] = useState<WatchItem[]>([]);
  const [plats, setPlats] = useState<Platform[]>([]);
  const [name, setName] = useState('');
  const [pid, setPid] = useState(0);
  const [loading, setLoading] = useState(true);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    try {
      const [w, p] = await Promise.all([fetchWatchList(), fetchPlatforms()]);
      // 只允许最新请求提交状态，防慢响应覆盖删除/新增后的最新列表
      if (seq !== seqRef.current) return;
      setItems(w);
      setPlats(p);
      if (p.length && !pid) setPid(p[0].id);
    } catch {
      if (seq !== seqRef.current) return;
      showToast('监控清单加载失败', 'error');
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [pid]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切到「监控」tab 时刷新列表，保证加入/删除后状态及时
  const tab = useTabNavigation();
  useEffect(() => {
    if (tab?.activeTab === '/watch') void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab?.activeTab]);

  // 在「监控」tab 时每 4 秒轮询刷新状态（在线/录制中/已录时长）
  useEffect(() => {
    if (tab?.activeTab !== '/watch') return;
    let alive = true;
    let timer: number;
    const tick = () => {
      fetchWatchList()
        .then((w) => {
          if (alive) setItems(w);
        })
        .catch(() => {})
        .finally(() => {
          // 上一次完成后等待 4 秒再下一次，避免慢请求期间并发堆积
          if (alive) timer = window.setTimeout(tick, 4000);
        });
    };
    timer = window.setTimeout(tick, 4000);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [tab?.activeTab]);

  const onAddManual = async () => {
    const n = name.trim();
    if (!n) return showToast('请输入名字', 'info');
    const manualId = `manual_${Date.now().toString(36)}`;
    try {
      await addWatch(manualId, pid || plats[0]?.id || 449, n);
      showToast('已添加监控', 'success');
      setName('');
      await load();
    } catch {
      showToast('添加失败', 'error');
    }
  };

  const onRemove = async (rid: string) => {
    seqRef.current++; // 使在途旧轮询/load 响应作废，防删除项被旧快照复活
    try {
      await removeWatch(rid);
      showToast('已移除', 'success');
      setItems((arr) => arr.filter((i) => i.room_id !== rid));
    } catch {
      showToast('移除失败', 'error');
    }
  };

  return (
    <div className="app-hub">
      <div className="app-hub__top">
        <h1 className="app-hub__title">监控</h1>
      </div>
      <div className="app-hub__scroll baoyu-watch">
        <div className="baoyu-watch-add">
        <input
          className="baoyu-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="主播名字"
        />
        <select
          className="baoyu-input baoyu-select"
          value={pid}
          onChange={(e) => setPid(Number(e.target.value))}
        >
          {plats.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button type="button" className="baoyu-btn" onClick={onAddManual} aria-label="添加监控">
          <Plus size={16} />
        </button>
      </div>

      {loading ? (
        <div className="baoyu-list">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="baoyu-row skeleton" />
          ))}
        </div>
      ) : (
        <div className="baoyu-list">
          {items.length === 0 && <div className="baoyu-empty">暂无监控，添加一个吧</div>}
          {items.map((it) => {
            const recording = it.rec_state === 'recording' || it.rec_state === 'starting' || it.rec_state === 'restarting' || it.rec_state === 'stopping';
            const online = it.online === true;
            return (
              <div key={it.room_id} className="watch-card">
                <div className="watch-card-row1">
                  <span className="watch-name">{it.name || it.room_id}</span>
                  {recording ? (
                    <span className="watch-badge watch-badge--rec">
                      <i className="watch-rec-dot" />
                      {it.rec_state === 'recording' ? '录制中' : it.rec_state === 'starting' ? '启动中' : it.rec_state === 'restarting' ? '重连中' : '收尾中'}
                    </span>
                  ) : online ? (
                    <span className="watch-badge watch-badge--on">
                      <i className="watch-on-dot" />
                      在线
                    </span>
                  ) : (
                    <span className="watch-badge watch-badge--off">
                      <i className="watch-off-dot" />
                      离线
                    </span>
                  )}
                  <button
                    type="button"
                    className="watch-del"
                    onClick={() => onRemove(it.room_id)}
                    aria-label="移除监控"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
                <div className="watch-card-row2">
                  <span className="watch-stat-v--big">{fmtDur(it.rec_duration)}</span>
                  <span className="watch-stat-sep">·</span>
                  <span className="watch-stat-v--big">{fmtBytesText(it.rec_bytes)}</span>
                  <span className="watch-foot-item">判定 {fmtAgo(it.checked_at)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
      </div>
    </div>
  );
}

/** 已录时长：<1h → mm:ss，≥1h → HhMMm；0 → '—' */
function fmtDur(sec?: number): string {
  if (!sec || sec <= 0) return '—';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`;
  return `${m}:${String(ss).padStart(2, '0')}`;
}

/** 字节人性化：0 → '—' */
function fmtBytesText(b?: number): string {
  if (!b || b <= 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let n = b; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

/** 距今天数/时间：checked_at 秒级时间戳 */
function fmtAgo(ts?: number | null): string {
  if (!ts) return '—';
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${diff}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}时前`;
  return `${Math.floor(diff / 86400)}天前`;
}
