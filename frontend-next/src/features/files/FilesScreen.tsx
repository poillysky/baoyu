'use client';

import { useEffect, useState, useCallback } from 'react';
import { Play, Trash2, ChevronRight, ChevronLeft } from 'lucide-react';
import { SwipeRow } from '@/components/ui/SwipeRow';
import { FullPlayer } from '@/components/ui/FullPlayer';
import { fetchFiles, type FileItem } from '@/lib/api';
import { showToast } from '../ui';
import { apiGet } from '@/lib/api';
import { AppPush } from '@/components/ui/AppPush';
import { useTabNavigation } from '@/shell/TabContext';

/** 主播分组：folder=主播名_roomid，取主播显示名 */
interface StreamerGroup {
  folder: string;
  name: string;
  videos: FileItem[];
  latest: number;
  totalBytes: number;
}

/** 从 folder 提取主播名（去掉尾部 _数字 房间号） */
function streamerName(folder: string): string {
  return folder.replace(/_\d+$/, '') || folder;
}

/**
 * 回放 — 按主播聚合 → 点进看该主播的具体视频列表。
 */
export function FilesScreen() {
  const [files, setFiles] = useState<FileItem[]>([]);
  const [groups, setGroups] = useState<StreamerGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [cur, setCur] = useState<StreamerGroup | null>(null);
  const [playing, setPlaying] = useState<FileItem | null>(null);

  const load = useCallback(async () => {
    try {
      const f = await fetchFiles();
      // 按 folder 聚合
      const map = new Map<string, FileItem[]>();
      for (const it of f) {
        const arr = map.get(it.folder) || [];
        arr.push(it);
        map.set(it.folder, arr);
      }
      const groups: StreamerGroup[] = [];
      map.forEach((arr, folder) => {
        arr.sort((a, b) => b.mtime - a.mtime);
        groups.push({
          folder,
          name: streamerName(folder),
          videos: arr,
          latest: arr[0]?.mtime || 0,
          totalBytes: arr.reduce((s, x) => s + (x.size || 0), 0),
        });
      });
      groups.sort((a, b) => b.latest - a.latest);
      setFiles(f);
      setGroups(groups);
    } catch {
      showToast('成品加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 切到「回放」tab 时刷新列表，保证删除/新增后状态及时
  const tab = useTabNavigation();
  useEffect(() => {
    if (tab?.activeTab === '/files') void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab?.activeTab]);

  const onDelete = async (f: FileItem) => {
    if (!confirm(`删除 ${f.name}？`)) return;
    try {
      // rel 才是 recordings/ 下的相对路径(主播文件夹/文件名)，name 只是文件名
      // 不同主播同名文件时 name 会删错/删不到，必须用 rel
      await apiGet(`/api/files/delete?name=${encodeURIComponent(f.rel)}`);
      showToast('已删除', 'success');
      setFiles((arr) => arr.filter((x) => x.rel !== f.rel));
      setPlaying(null);
      void load();
    } catch {
      showToast('删除失败', 'error');
    }
  };

  // 删除整个主播文件夹（连带里面所有视频）
  const onDeleteFolder = async (g: StreamerGroup) => {
    if (!confirm(`删除「${g.name}」全部 ${g.videos.length} 个视频？此操作不可恢复。`)) return;
    try {
      await apiGet(`/api/files/delete_folder?folder=${encodeURIComponent(g.folder)}`);
      showToast('已删除文件夹', 'success');
      setGroups((arr) => arr.filter((x) => x.folder !== g.folder));
      setFiles((arr) => arr.filter((x) => x.folder !== g.folder));
      void load();
    } catch {
      showToast('删除失败', 'error');
    }
  };

  // 视频详情页（某主播的视频列表）
  if (cur) {
    const videos = files
      .filter((f) => f.folder === cur.folder)
      .sort((a, b) => b.mtime - a.mtime);
    return (
      <AppPush title={cur.name} onBack={() => { setCur(null); setPlaying(null); }} scrollMode="top">
        {playing && (
          <FullPlayer
            src={playing.url}
            title={cur.name}
            subtitle={`${fmtVideoTime(playing.name, playing.mtime)}${playing.duration ? ' · ' + fmtDuration(playing.duration) : ''}`}
            onClose={() => setPlaying(null)}
          />
        )}
        <div className="baoyu-grid">
          {videos.length === 0 && <div className="baoyu-empty">暂无视频</div>}
          {videos.map((f) => (
            <div key={f.name} className="baoyu-cell">
              <button type="button" className="baoyu-cell-thumb" onClick={() => setPlaying(f)}>
                {f.thumb ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={f.thumb} alt="" loading="lazy" />
                ) : (
                  <span className="baoyu-cell-ph"><Play size={26} /></span>
                )}
                <span className="baoyu-cell-play"><Play size={24} /></span>
                <div className="baoyu-cell-meta">
                  <span className="baoyu-cell-line">
                    <span className="baoyu-cell-time">{fmtVideoTime(f.name, f.mtime)}</span>
                    {f.duration ? (
                      <span className="baoyu-cell-sub">{fmtDuration(f.duration)}</span>
                    ) : null}
                  </span>
                </div>
              </button>
              <button
                type="button"
                className="baoyu-cell-del"
                onClick={() => onDelete(f)}
                aria-label="删除"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      </AppPush>
    );
  }

  return (
    <div className="app-hub">
      <div className="app-hub__top">
        <h1 className="app-hub__title">回放</h1>
      </div>
      <div className="app-hub__scroll">
        {loading ? (
          <div className="baoyu-list">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="baoyu-row skeleton" />
            ))}
          </div>
        ) : (
          <div className="baoyu-list">
            {groups.length === 0 && <div className="baoyu-empty">暂无成品</div>}
            {groups.map((g) => (
              <SwipeRow key={g.folder} onDelete={() => onDeleteFolder(g)}>
                <button
                  type="button"
                  className="baoyu-row baoyu-row--flat"
                  onClick={() => setCur(g)}
                >
                  <span className="baoyu-row-name">{g.name}</span>
                  <span className="baoyu-row-trail">
                    <span className="baoyu-row-count">{g.videos.length} 个</span>
                    <ChevronRight className="baoyu-row-chev" size={18} />
                  </span>
                </button>
              </SwipeRow>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** 从文件名解析录制 月-日 时:分（形如 主播_20261122-085927.mp4），退化为 mtime */
function fmtVideoTime(name: string, mtime: number): string {
  const m = name.match(/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})\.(?:mp4|webm)$/);
  if (m) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  const d = new Date(mtime * 1000);
  const p = (x: number) => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 时长格式化：<1h → mm:ss；≥1h → HhMMm；0 → '' */
function fmtDuration(sec?: number): string {
  if (!sec || sec <= 0) return '';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`;
  return `${m}:${String(ss).padStart(2, '0')}`;
}
