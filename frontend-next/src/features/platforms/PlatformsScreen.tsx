'use client';

import { useEffect, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { fetchPlatforms, imgUrl, type Platform } from '@/lib/api';
import { fmtCount, showToast, Avatar } from '../ui';
import { useTabNavigation } from '@/shell/TabContext';

/**
 * 平台列表 — 第一屏：一行一个直播平台（纯文字）。
 * 结构对齐 sehua：app-hub(flex-col,100%) > app-hub__scroll(flex:1 独立滚动)。
 */
export function PlatformsScreen({ onOpen }: { onOpen: (p: Platform) => void }) {
  const [plats, setPlats] = useState<Platform[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    fetchPlatforms()
      .then((d) => setPlats(d))
      .catch(() => showToast('平台加载失败', 'error'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    let alive = true;
    fetchPlatforms()
      .then((d) => {
        if (!alive) return;
        setPlats(d);
      })
      .catch(() => alive && showToast('平台加载失败', 'error'))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切回「平台」tab 时刷新房间数，保证增量及时
  const tab = useTabNavigation();
  useEffect(() => {
    if (tab?.activeTab === '/') refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab?.activeTab]);

  return (
    <div className="app-hub">
      <div className="app-hub__top">
        <h1 className="app-hub__title">平台</h1>
      </div>
      <div className="app-hub__scroll">
        {loading ? (
          <div className="baoyu-list">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="baoyu-row skeleton" />
            ))}
          </div>
        ) : (
          <div className="baoyu-list">
            {plats.map((p) => (
              <button type="button" key={p.id} className="baoyu-row" onClick={() => onOpen(p)}>
                <Avatar name={p.name} src={imgUrl(p.cover)} size={40} />
                <span className="baoyu-row-name">{p.name}</span>
                <span className="baoyu-row-trail">
                  <span className="baoyu-row-count">{fmtCount(p.room_count)} 房</span>
                  <ChevronRight className="baoyu-row-chev" size={18} />
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
