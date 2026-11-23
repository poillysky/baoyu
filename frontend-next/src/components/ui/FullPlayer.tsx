'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { ChevronLeft, Play, Pause } from 'lucide-react';

/**
 * 全面屏沉浸式本地视频播放页。
 * 全屏黑色舞台 + 视频居中 contain，浮动控制条（返回/标题/播放暂停），
 * 点击画面切换控制条显隐，播放结束回显控制条。
 */
export function FullPlayer({
  src,
  title,
  subtitle,
  onClose,
}: {
  src: string;
  title: string;
  subtitle?: string;
  onClose: () => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [showCtrl, setShowCtrl] = useState(true);
  const [progress, setProgress] = useState(0);
  const [dur, setDur] = useState(0);
  const [cur, setCur] = useState(0);
  const [mounted, setMounted] = useState(false);
  const hideTimer = useRef<number | null>(null);

  useEffect(() => {
    setMounted(true);
    // 挂载后尝试自动播放；被浏览器自动播放策略拒绝时保持 paused 状态不误报
    const v = videoRef.current;
    if (v) {
      v.play().catch(() => setPlaying(false));
    }
  }, []);

  const armHide = useCallback(() => {
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setShowCtrl(false), 3200);
  }, []);

  useEffect(() => {
    armHide();
    return () => {
      if (hideTimer.current) window.clearTimeout(hideTimer.current);
      // 卸载时暂停并释放媒体，避免后台继续播放/占用资源
      const v = videoRef.current;
      if (v) {
        try { v.pause(); } catch {}
        v.removeAttribute('src');
        try { v.load(); } catch {}
      }
    };
  }, [armHide]);

  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      v.play().catch(() => setPlaying(false));
    } else {
      v.pause();
      setPlaying(false);
    }
    setShowCtrl(true);
    armHide();
  };

  const onSurfaceTap = () => {
    setShowCtrl((s) => {
      const next = !s;
      if (next) armHide();
      return next;
    });
  };

  const fmt = (s: number) => {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60);
    const ss = Math.floor(s % 60);
    return `${m}:${String(ss).padStart(2, '0')}`;
  };

  const onSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = videoRef.current;
    if (!v) return;
    const t = Number(e.target.value);
    v.currentTime = t;
    setCur(t);
  };

  const enterFullscreen = () => {
    const el = wrapRef.current;
    if (el && el.requestFullscreen) {
      el.requestFullscreen().catch(() => {});
    }
  };

  if (!mounted) return null;

  return createPortal(
    <div className="fp-root" ref={wrapRef}>
      {/* 视频舞台 */}
      <div className="fp-stage" onClick={onSurfaceTap}>
        <video
          ref={videoRef}
          className="fp-video"
          src={src}
          playsInline
          onTimeUpdate={(e) => {
            const v = e.currentTarget;
            setCur(v.currentTime);
            setProgress(v.duration ? v.currentTime / v.duration : 0);
          }}
          onLoadedMetadata={(e) => setDur(e.currentTarget.duration || 0)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => {
            setPlaying(false);
            setShowCtrl(true);
          }}
        />
      </div>

      {/* 顶部控制条 */}
      <div className={`fp-top ${showCtrl ? 'show' : ''}`} onClick={(e) => e.stopPropagation()}>
        <button type="button" className="fp-back" onClick={onClose} aria-label="返回">
          <ChevronLeft size={26} />
        </button>
        <div className="fp-title-wrap">
          <span className="fp-title">{title}</span>
          {subtitle && <span className="fp-subtitle">{subtitle}</span>}
        </div>
      </div>

      {/* 中央播放/暂停提示 */}
      <button
        type="button"
        className={`fp-bigplay ${showCtrl && !playing ? 'show' : 'hide'}`}
        onClick={(e) => {
          e.stopPropagation();
          togglePlay();
        }}
        aria-label={playing ? '暂停' : '播放'}
      >
        {playing ? <Pause size={40} /> : <Play size={40} />}
      </button>

      {/* 底部进度/控制条 */}
      <div className={`fp-bottom ${showCtrl ? 'show' : ''}`} onClick={(e) => e.stopPropagation()}>
        <input
          className="fp-progress"
          type="range"
          min={0}
          max={dur || 0}
          step={0.1}
          value={cur}
          onChange={onSeek}
          style={{ ['--p' as string]: `${(progress * 100).toFixed(2)}%` }}
        />
        <div className="fp-ctrl-row">
          <button type="button" className="fp-icon-btn" onClick={togglePlay} aria-label="播放/暂停">
            {playing ? <Pause size={18} /> : <Play size={18} />}
          </button>
          <span className="fp-time">{fmt(cur)} / {fmt(dur)}</span>
          <span className="fp-spacer" />
          <button type="button" className="fp-icon-btn" onClick={enterFullscreen} aria-label="全屏">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
