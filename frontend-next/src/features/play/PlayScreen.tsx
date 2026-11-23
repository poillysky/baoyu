'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { apiGet, type Room } from '@/lib/api';
import { AppPush } from '@/components/ui/AppPush';
import {
  ChevronLeft,
  Volume2,
  VolumeX,
  Maximize2,
  RotateCcw,
} from 'lucide-react';

declare global {
  interface Window {
    AgoraRTC?: any;
  }
}

/** ---------- HLS/FLV 直链播放(mpegts.js/hls.js) ----------
 * 半糖/密语走 Agora; 番茄/小妲己(HLS)/杏仁/青瑶/草莓(FLV) 走直链 <video>。
 * m3u8/flv URL 经后端 /api/stream 代理转发, 绕开浏览器 CORS(hls.js 用 xhr 拉)。 */

let mpegtsPromise: Promise<any> | null = null;
function loadMpegts(): Promise<any> {
  const W = window as any;
  if (W.mpegts) return Promise.resolve(W.mpegts);
  if (mpegtsPromise) return mpegtsPromise;
  mpegtsPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/mpegts.js';
    s.async = true;
    s.onload = () => (W.mpegts ? resolve(W.mpegts) : reject(new Error('mpegts.js 加载失败')));
    s.onerror = () => { mpegtsPromise = null; reject(new Error('mpegts.js 加载失败')); };
    document.head.appendChild(s);
  });
  return mpegtsPromise;
}

let hlsPromise: Promise<any> | null = null;
function loadHls(): Promise<any> {
  const W = window as any;
  if (W.Hls) return Promise.resolve(W.Hls);
  if (hlsPromise) return hlsPromise;
  hlsPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/hls.light.min.js';
    s.async = true;
    s.onload = () => (W.Hls ? resolve(W.Hls) : reject(new Error('hls.js 加载失败')));
    s.onerror = () => { hlsPromise = null; reject(new Error('hls.js 加载失败')); };
    document.head.appendChild(s);
  });
  return hlsPromise;
}

/** 判断直链类型: .m3u8 → hls; .flv → mpegts-flv; 其他按 mpegts 兜底 */
function streamKind(url: string): 'hls' | 'flv' | null {
  if (!url) return null;
  const u = url.split('?')[0].toLowerCase();
  if (u.endsWith('.m3u8')) return 'hls';
  if (u.endsWith('.flv')) return 'flv';
  return null;
}

/** 全局运行序号，跨组件实例 */
let runTokenSeq = 0;

/** 模块级：保证同一时刻只有一个 Agora client 在频道里，避免 UID_CONFLICT / 双连接卡 CONNECTING */
let activeClient: any = null;
/** 串行锁：保证 dispose→create→join 严格排队，React StrictMode 双挂载也不会并存两个 client */
let opChain: Promise<void> = Promise.resolve();

async function disposeActiveClient() {
  const c = activeClient;
  activeClient = null;
  if (!c) return;
  try {
    if (c.__playTimer) clearTimeout(c.__playTimer);
  } catch {}
  // 停止已订阅的音视频轨道，避免离开频道后音轨继续播放
  try {
    const users = c.remoteUsers || [];
    for (const u of users) {
      try { u.audioTrack?.stop?.(); } catch {}
      try { u.videoTrack?.stop?.(); } catch {}
    }
  } catch {}
  try {
    await c.leave();
  } catch {}
}

/** 观众端不需要摄像头/麦克风。iOS Safari 下 SDK init 会调 navigator.mediaDevices.enumerateDevices，
 *  非 HTTPS 或无权限时抛 NotAllowedError，污染控制台。这里在加载 SDK 前把它兜成安全版本：
 *  失败时 resolve([]) 而不是 reject，SDK 拿空列表照常以 audience 身份入会拉流。 */
let devicesPatched = false;
function patchEnumerateDevices() {
  if (devicesPatched || typeof navigator === 'undefined') return;
  const md: any = navigator.mediaDevices;
  if (!md || typeof md.enumerateDevices !== 'function') return;
  const orig = md.enumerateDevices.bind(md);
  md.enumerateDevices = () => orig().catch(() => [] as MediaDeviceInfo[]);
  devicesPatched = true;
}

/** Agora SDK 会用浏览器 fetch 探测 cds-*.agora.io / webrtc*.agora.io 的 HTTP 边缘节点，
 *  被浏览器 CORS 拦后控制台报红（不影响 wss 实际播放）。这里在 SDK 加载前拦截这些探测，
 *  直接返回 404 Response，让 SDK 立即走 fallback，从源头消灭跨域红字。 */
/** 动态加载本地 AgoraRTC SDK（只加载一次） */
let agoraPromise: Promise<any> | null = null;
function loadAgora(): Promise<any> {
  if (window.AgoraRTC) return Promise.resolve(window.AgoraRTC);
  if (agoraPromise) return agoraPromise;
  patchEnumerateDevices();
  agoraPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/AgoraRTC_N-4.20.0.js';
    s.async = true;
    s.onload = () => {
      try {
        window.AgoraRTC?.setLogLevel?.(0); // 0=NONE，压掉 SDK 控制台噪声
      } catch {}
      // 校验 SDK 确实挂上了全局，否则按失败处理
      if (window.AgoraRTC) resolve(window.AgoraRTC);
      else {
        agoraPromise = null;
        reject(new Error('Agora SDK 加载失败'));
      }
    };
    s.onerror = () => {
      // 失败后清空缓存的 Promise，允许下次重试重新加载脚本
      agoraPromise = null;
      if (s.parentNode) s.parentNode.removeChild(s);
      reject(new Error('Agora SDK 加载失败'));
    };
    document.head.appendChild(s);
  });
  return agoraPromise;
}

type PlayStatus = 'loading' | 'playing' | 'error';

/**
 * 播放页 — Agora 观众端拉流，成熟播放器 UI：
 * 固定 16:9 舞台 + 毛玻璃控制层（返回/标题/直播徽标/静音/全屏）+ 加载骨架 + 错误重试。
 */
export function PlayScreen({ room, onBack }: { room: Room; onBack: () => void }) {
  const vidRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<any>(null);
  const clientRef = useRef<any>(null);
  const disposedRef = useRef(false);
  const runTokenRef = useRef(0);
  /** 真实播放状态 ref，避免定时器闭包读到陈旧 status */
  const playingRef = useRef(false);
  /** 直链播放(HLS/FLV): <video> 元素 + mpegts/hls 播放器实例引用 */
  const videoElRef = useRef<HTMLVideoElement | null>(null);
  const mediaPlayerRef = useRef<any>(null);

  const [status, setStatus] = useState<PlayStatus>('loading');
  const [msg, setMsg] = useState('连接中…');
  const [muted, setMuted] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [isFull, setIsFull] = useState(false);
  const [aspect, setAspect] = useState<'16 / 9' | '9 / 16'>('9 / 16');

  /** 视频起播时按真实分辨率决定舞台横竖 */
  const applyVideoSize = useCallback((w: number, h: number) => {
    if (w && h) setAspect(w >= h ? '16 / 9' : '9 / 16');
  }, []);

  const roomName = room.name || room.title || String(room.room_id);

  const startPlay = useCallback(async () => {
    disposedRef.current = false;
    playingRef.current = false;
    setStatus('loading');
    setMsg('连接中…');
    let client: any = null;
    let playTimer: ReturnType<typeof setTimeout> | null = null;
    /** 清超时定时器 + 标记播放态，供事件回调统一调用（起播前就建好） */
    const clearTimer = () => {
      if (playTimer) {
        clearTimeout(playTimer);
        playTimer = null;
      }
    };

    try {
      // 串行：先等上一轮 dispose + 本轮全部入队，React StrictMode 双挂载/快速重试都不会并存两个 client
      await disposeActiveClient();
      // 记录本次运行的序号，若中途被新的 effect 覆盖则作废
      const myToken = ++runTokenSeq;
      runTokenRef.current = myToken;

      // 提前建立超时定时器：15s 内未收到画面则判失败。
      // 用 playingRef 判断，避免闭包捕获陈旧 status（即使 UI 已 playing，ref 才是真实值）。
      playTimer = setTimeout(() => {
        if (!disposedRef.current && runTokenRef.current === myToken && !playingRef.current) {
          setStatus('error');
          setMsg('未收到画面，可能主播未开播或需要门票/密码');
        }
      }, 15000);

      const cred = await apiGet(
        `/api/play?room_id=${encodeURIComponent(room.room_id)}&pid=${room.pid ?? 0}`,
      );
      const d = cred?.data || cred;
      if (!d || (!d.ok && !d.token)) throw new Error(d?.msg || '凭证获取失败');
      if (disposedRef.current || runTokenRef.current !== myToken) return;

      // ===== 直链分支: HLS(.m3u8)/FLV(.flv) 无 appid/token, 走 <video> 播放 =====
      const playUrl: string = d.play_url || '';
      const kind = d.mode === 'direct' || (!d.appid && playUrl) ? streamKind(playUrl) : null;
      if (kind) {
        const proxied = `/api/stream?url=${encodeURIComponent(playUrl)}`;
        let video = videoElRef.current;
        if (!video) {
          video = document.createElement('video');
          video.className = 'bp-media-el';
          video.playsInline = true;
          video.autoplay = true;
          (video as any).webkitPlaysinline = true;
          video.muted = false;
          vidRef.current?.appendChild(video);
          videoElRef.current = video;
        }
        const v = video!;
        v.onloadedmetadata = () => {
          if (v.videoWidth && v.videoHeight) applyVideoSize(v.videoWidth, v.videoHeight);
        };
        v.onplaying = () => {
          playingRef.current = true;
          clearTimer();
          setStatus('playing');
          setMsg('播放中');
        };
        v.onerror = () => {
          if (!disposedRef.current && runTokenRef.current === myToken && !playingRef.current) {
            setStatus('error');
            setMsg('直播流连接失败');
          }
        };
        if (kind === 'hls') {
          // Safari 原生支持 HLS 直接 video.src; 其他浏览器用 hls.js
          const canNative = v.canPlayType('application/vnd.apple.mpegurl');
          if (canNative) {
            v.src = proxied;
            v.play().catch(() => {});
          } else {
            const Hls = await loadHls();
            if (disposedRef.current || runTokenRef.current !== myToken) return;
            if (Hls.isSupported()) {
              const hls = new Hls({ maxBufferLength: 6, liveSyncDurationCount: 3 });
              mediaPlayerRef.current = hls;
              hls.loadSource(proxied);
              hls.attachMedia(v);
              hls.on(Hls.Events.ERROR, (_ev: any, data: any) => {
                if (data?.fatal && !playingRef.current) {
                  setStatus('error');
                  setMsg('HLS 流错误: ' + (data.details || ''));
                }
              });
            } else {
              throw new Error('浏览器不支持 HLS 播放');
            }
          }
        } else {
          // FLV → mpegts.js
          const mpegts = await loadMpegts();
          if (disposedRef.current || runTokenRef.current !== myToken) return;
          if (!mpegts.getFeatureList?.().mseLivePlayback) {
            throw new Error('浏览器不支持 FLV 播放');
          }
          // HEVC in FLV (enhanced-FLV) 依赖浏览器 MSE hvc1 硬解能力;
          // 不支持时只会出声音没画面, 提前给出明确错误而不是让用户干等 15s 超时。
          const urlLower = playUrl.split('?')[0].toLowerCase();
          const flvHevc = urlLower.endsWith('.flv');
          if (
            flvHevc &&
            window.MediaSource &&
            !MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.B0"') &&
            !MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.1.L93.B0"')
          ) {
            throw new Error('当前浏览器不支持 HEVC(H.265) 解码，请使用较新版 Chrome/Safari 桌面浏览器');
          }
          const player = mpegts.createPlayer(
            { type: 'flv', isLive: true, url: proxied },
            { enableStashBuffer: false, liveBufferLatencyChasing: true },
          );
          mediaPlayerRef.current = player;
          player.attachMediaElement(v);
          player.load();
          player.play().catch(() => {});
        }
        return; // 直链分支到此为止, 不进 Agora
      }

      // ===== Agora 分支(半糖/密语等) =====
      const AgoraRTC = await loadAgora();
      if (disposedRef.current || runTokenRef.current !== myToken) return;

      // token 是按后端返回的 uid 签发的，入会 uid 必须与之一致，否则 invalid token。
      // （之前 UID_CONFLICT 是双 client 并存所致，已由串行链解决，不是 uid 本身问题）
      const uid = d.uid != null ? Number(d.uid) >>> 0 : 0;

      // create→setClientRole→join 严格排进全局串行链，保证任何时刻频道里只有一个 client
      const joinTask = opChain.then(async () => {
        if (disposedRef.current || runTokenRef.current !== myToken) return;
        await disposeActiveClient();
        if (disposedRef.current || runTokenRef.current !== myToken) return;

        client = AgoraRTC.createClient({ mode: 'live', codec: 'h264' });
        activeClient = client;
        clientRef.current = client;

        client.on('connection-state-change', (cur: string) => {
          if (disposedRef.current || runTokenRef.current !== myToken) return;
          if (cur === 'CONNECTED') setMsg('已连接，等待画面…');
          else setMsg(`连接中…（${cur}）`);
        });

        client.on('user-published', async (user: any, mediaType: string) => {
          try {
            if (disposedRef.current || runTokenRef.current !== myToken) return;
            await client.subscribe(user, mediaType);
            if (disposedRef.current || runTokenRef.current !== myToken) return;
            if (mediaType === 'video') {
              if (vidRef.current) user.videoTrack.play(vidRef.current);
              playingRef.current = true;
              clearTimer();
              setStatus('playing');
              setMsg('播放中');
              const vt = user.videoTrack;
              if (vt?.getMediaStreamTrack) {
                const track = vt.getMediaStreamTrack?.();
                const st = track?.getSettings?.();
                if (st?.width && st?.height) applyVideoSize(st.width, st.height);
              }
            }
            if (mediaType === 'audio') {
              audioRef.current = user.audioTrack;
              user.audioTrack?.play();
            }
          } catch (e) {
            // 订阅失败不致命，交给超时/重试兜底
          }
        });

        await client.setClientRole('audience');
        await client.join(d.appid, d.channel, d.token || null, uid);
      });
      opChain = joinTask.catch(() => {});
      await joinTask;

      if (disposedRef.current || runTokenRef.current !== myToken) return;

      // 兜底：入会后已有远端视频立即订阅
      for (const u of client.remoteUsers || []) {
        if (disposedRef.current || runTokenRef.current !== myToken) break;
        if (u.hasVideo) {
          await client.subscribe(u, 'video');
          if (vidRef.current) u.videoTrack.play(vidRef.current);
          playingRef.current = true;
          clearTimer();
          setStatus('playing');
          setMsg('播放中');
          const st = u.videoTrack?.getMediaStreamTrack?.()?.getSettings?.();
          if (st?.width && st?.height) applyVideoSize(st.width, st.height);
        }
        if (u.hasAudio) {
          await client.subscribe(u, 'audio');
          audioRef.current = u.audioTrack;
          u.audioTrack?.play();
        }
      }
    } catch (e: any) {
      if (!disposedRef.current) {
        setStatus('error');
        setMsg(e?.message || '播放失败');
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.room_id, room.pid]);

  useEffect(() => {
    startPlay();
    return () => {
      disposedRef.current = true;
      // 停掉本地音频引用，避免卸载后继续出声
      try { audioRef.current?.stop?.(); } catch {}
      audioRef.current = null;
      // 销毁直链播放器(mpegts/hls) + <video> 元素
      try {
        const mp = mediaPlayerRef.current;
        mediaPlayerRef.current = null;
        if (mp) {
          try { mp.pause?.(); } catch {}
          try { mp.unload?.(); } catch {}
          try { mp.detachMediaElement?.(); } catch {}
          try { mp.destroy?.(); } catch {}
        }
        const v = videoElRef.current;
        videoElRef.current = null;
        if (v) {
          try { v.pause(); } catch {}
          try { v.removeAttribute('src'); v.load?.(); } catch {}
          v.parentNode?.removeChild(v);
        }
      } catch {}
      disposeActiveClient();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.room_id, room.pid, retryKey]);

  const toggleMute = () => {
    // 直链分支: 直接操作 <video>.muted; Agora 分支: 操作 audioTrack
    const v = videoElRef.current;
    if (v) {
      const next = !muted;
      setMuted(next);
      try { v.muted = next; } catch {}
      return;
    }
    const track = audioRef.current;
    const next = !muted;
    setMuted(next);
    try {
      next ? track?.setMuted(true) : track?.setMuted(false);
    } catch {}
  };

  const toggleFull = () => {
    const el = stageRef.current;
    if (!el) return;
    try {
      if (!document.fullscreenElement) {
        (el.requestFullscreen?.() ||
          (el as any).webkitRequestFullscreen?.())?.catch?.(() => {});
        setIsFull(true);
      } else {
        (document.exitFullscreen?.() ||
          (document as any).webkitExitFullscreen?.())?.catch?.(() => {});
        setIsFull(false);
      }
    } catch {}
  };

  const retry = () => setRetryKey((k) => k + 1);

  return (
    <AppPush title={roomName} onBack={onBack} scrollMode="top">
      <div className="bp-player">
        {/* 舞台：16:9 固定比例，视频 contain 居中 */}
        <div className={`bp-stage ${status}`} ref={stageRef} style={{ aspectRatio: aspect }}>
          <div className="bp-video" ref={vidRef} />
          {/* 直链播放时 <video> 由 JS 动态插入 bp-video 容器; CSS 见 .bp-media-el */}

          {/* 加载/错误遮罩 */}
          {status === 'loading' && (
            <div className="bp-overlay">
              <div className="bp-spinner" />
              <div className="bp-overlay-text">{msg}</div>
            </div>
          )}
          {status === 'error' && (
            <div className="bp-overlay">
              <div className="bp-err-icon">!</div>
              <div className="bp-overlay-text">{msg}</div>
              <button type="button" className="bp-retry" onClick={retry}>
                <RotateCcw size={15} /> 重试
              </button>
            </div>
          )}

          {/* 顶部控制条：返回 + 标题 + 直播徽标 */}
          <div className="bp-top">
            <button type="button" className="bp-icon-btn" onClick={onBack} aria-label="返回">
              <ChevronLeft size={22} />
            </button>
            <div className="bp-top-title">{roomName}</div>
            <span className={`bp-live ${status === 'playing' ? 'on' : ''}`}>
              <span className="bp-live-dot" />
              {status === 'playing' ? 'LIVE' : '连接'}
            </span>
          </div>

          {/* 底部控制条：直播状态 + 静音 + 全屏 */}
          <div className="bp-bottom">
            <span className={`bp-state ${status}`}>{msg}</span>
            <div className="bp-actions">
              <button
                type="button"
                className="bp-icon-btn"
                onClick={toggleMute}
                aria-label={muted ? '取消静音' : '静音'}
              >
                {muted ? <VolumeX size={20} /> : <Volume2 size={20} />}
              </button>
              <button
                type="button"
                className="bp-icon-btn"
                onClick={toggleFull}
                aria-label="全屏"
              >
                <Maximize2 size={19} />
              </button>
            </div>
          </div>
        </div>

        {/* 舞台下方信息条 */}
        <div className="bp-info">
          <div className="bp-info-meta">
            {room.view_count != null && (
              <span className="bp-chip">🔥 {fmtCount(room.view_count)}</span>
            )}
            <span className={`bp-chip ${status === 'playing' ? 'live' : ''}`}>
              {status === 'playing' ? '● 直播中' : status === 'error' ? '● 连接失败' : '● 连接中'}
            </span>
            {room.type != null && (
              <span className="bp-chip">
                {{ 0: '免费', 1: '门票', 2: '计时', 3: '密码', 4: '等级' }[room.type] || '—'}
              </span>
            )}
          </div>
        </div>
      </div>
    </AppPush>
  );
}

function fmtCount(n: number | undefined): string {
  if (n == null) return '0';
  if (n >= 1e8) return (n / 1e8).toFixed(1) + '亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
  return String(n);
}
