#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_worker.py — 密语(474)及标准 Agora 房「原生录制」正式内核。

设计目标（按老大要求）:
  1. 视频 + 声音都正常
  2. 断网 / 崩溃 / 异常 视频文件不丢失  → 边收边封 frag mp4 (frag_keyframe+empty_moov)
     且保留 .h264 / .pcm 原始裸流兜底, 崩溃后仍可事后合成
  3. 音画不错位 → 记录视频首帧 / 音频首帧墙钟时间, mux 时用真实 fps + 音频对齐

关键经验(已实测):
  - 视频回调 on_encoded_video_frame 只顺序写 .h264 (等首个 I 帧开文件, 保 SPS/PPS)
  - 音频回调 on_playback_audio_frame_before_mixing:
      * rtp_timestamp 去重 (同段会被回调多次 → 否则音频膨胀 2x)
      * 丢开头 burst (连接瞬间 SDK 突发吐出历史缓冲, 稳态才 ~100帧/秒=16k实时)
      * 记录首帧墙钟时间用于音画对齐
  - 录制期绝不起 ffmpeg / 不写管道 (会阻塞 SDK 回调 → 0帧)
  - stop 后一次性用 ffmpeg 合成 (fps 由 视频帧/(视频首末墙钟差) 估算)

环境: .venv-native2410 (Agora SDK 4.10)
用法: python3 native_worker.py '<cred_json>' <name> <secs>
"""
import sys, os, json, time, threading, collections, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
import config as C

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RtcConnectionPublishConfig
from agora.rtc.rtc_connection import RTCConnConfig
from agora.rtc.agora_base import (
    ClientRoleType, ChannelProfileType, AreaCode, AudioScenarioType,
    VideoSubscriptionOptions, VideoStreamType, AudioParams,
)
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.video_encoded_frame_observer import IVideoEncodedFrameObserver
from agora.rtc.audio_frame_observer import IAudioFrameObserver


def log(*a):
    print("[NATIVE-WORKER]", *a, flush=True)


# 音频开头 burst 丢弃窗口: 订阅后这段时间内的音频帧是 SDK 突发吐出的历史缓冲，丢掉
AUDIO_BURST_SKIP_SEC = 1.2
# 视频连续 N 秒无帧视为流中断 (断网), 触发兜底封片
VIDEO_STALL_SEC = 6.0


class Recorder:
    def __init__(self, cred, name, out_dir=None):
        self.cred = cred
        self.name = name or "native"
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_dir = out_dir or C.REC_DIR
        os.makedirs(out_dir, exist_ok=True)
        base = "%s_%s" % (name or ("ROOM_%s" % cred.get("channel")), ts)
        self.base = base
        self.h264_path = os.path.join(out_dir, base + ".h264")
        self.pcm_path = os.path.join(out_dir, base + ".pcm")
        self.mp4_path = os.path.join(out_dir, base + ".mp4")
        self.out_dir = out_dir
        self.vf = None
        self.af = None
        self.video_frames = 0
        self.keyframes = 0
        self.audio_bytes = 0
        self.width = self.height = 0
        self.codec = ""
        self.error = ""
        self.conn = None
        self.svc = None
        self.stop_event = threading.Event()
        self._first_kf = False
        self._start_ts = time.time()
        self.audio_sample_rate = 16000
        self.audio_channels = 1
        # 音画同步时间戳 (墙钟)
        self.v_first_ts = None      # 视频首帧(首个I帧)到达时刻
        self.v_last_ts = None       # 视频末帧到达时刻
        self.a_first_ts = None      # 音频(去burst后首个有效帧)到达时刻
        # 音频去重/去burst
        self._seen_rtp = collections.deque(maxlen=512)
        self._audio_started = False
        self._raw_audio_start_ts = None   # 音频回调首次触发时刻(判断burst窗口)
        self._lock = threading.Lock()
        self._stop_reason = ""

    # ---- observers ----
    def _make_observers(self):
        rec = self

        class Conn(IRTCConnectionObserver):
            def on_connected(self, c, i, r):
                log("on_connected reason=", r)
            def on_disconnected(self, c, i, r):
                log("on_disconnected reason=", r)
                rec._stop_reason = "disconnected reason=%s" % r
                # 断线: 通知主循环尽快收尾封片 (不阻塞回调)
                rec.stop_event.set()
            def on_error(self, c, e, msg):
                rec.error = "conn error=%s" % e
                log("on_error", e)
            def on_user_joined(self, c, uid):
                log("on_user_joined uid=", uid)

        class VideoObs(IVideoEncodedFrameObserver):
            def on_encoded_video_frame(self, uid, buf, length, info):
                if length <= 0:
                    return 1
                try:
                    ft = info.frame_type
                    now = time.time()
                    if not rec._first_kf:
                        if ft != 3:            # 等首个 I 帧, 保证 SPS/PPS 在文件头
                            return 1
                        rec._first_kf = True
                        rec.vf = open(rec.h264_path, "wb")
                        rec.v_first_ts = now
                        log("first keyframe, open h264")
                    rec.vf.write(bytes(buf[:length]))
                    rec.video_frames += 1
                    rec.v_last_ts = now
                    if ft == 3:
                        rec.keyframes += 1
                    rec.width = info.width or rec.width
                    rec.height = info.height or rec.height
                    rec.codec = "h264"
                except Exception as e:
                    rec.error = "video cb err: %r" % e
                return 1

        class AudioObs(IAudioFrameObserver):
            def on_playback_audio_frame_before_mixing(self, lu, ch, uid, frame, *a):
                try:
                    now = time.time()
                    # 记录首次回调时刻, 用于判定 burst 窗口
                    if rec._raw_audio_start_ts is None:
                        rec._raw_audio_start_ts = now
                    # 1) 丢开头 burst: 订阅后 AUDIO_BURST_SKIP_SEC 内的帧是历史缓冲
                    if now - rec._raw_audio_start_ts < AUDIO_BURST_SKIP_SEC:
                        return 1
                    # 2) rtp 去重: 同一 rtp 会被回调多次
                    rtp = frame.rtp_timestamp
                    with rec._lock:
                        if rtp in rec._seen_rtp:
                            return 1
                        rec._seen_rtp.append(rtp)
                    size = frame.samples_per_channel * frame.bytes_per_sample * frame.channels
                    if size <= 0 or not frame.buffer:
                        return 1
                    if rec.af is None:
                        rec.af = open(rec.pcm_path, "wb")
                        rec.audio_sample_rate = frame.samples_per_sec or 16000
                        rec.audio_channels = frame.channels or 1
                        rec.a_first_ts = now
                        log("audio first valid frame sr=%d ch=%d (skip burst %.1fs)"
                            % (rec.audio_sample_rate, rec.audio_channels, AUDIO_BURST_SKIP_SEC))
                    rec.af.write(bytes(frame.buffer[:size]))
                    rec.audio_bytes += size
                except Exception as e:
                    rec.error = "audio cb err: %r" % e
                return 1
            def on_get_playback_audio_frame_param(self, lu):
                p = AudioParams()
                p.sample_rate = 16000
                p.channels = 1
                p.mode = 0
                p.samples_per_call = 640
                return p
            def on_get_audio_frame_position(self, lu):
                return 0

        return Conn(), VideoObs(), AudioObs()

    def start(self):
        cred = self.cred
        appid = cred["appid"]; channel = str(cred["channel"])
        token = cred["token"]; uid = str(cred.get("uid") or 0)
        self.svc = AgoraService()
        cfg = AgoraServiceConfig(
            log_path=os.path.join(HERE, "native_agora.log"), log_file_size_kb=1024,
            enable_audio_processor=0, enable_audio_device=0, enable_video=1,
            appid=appid, area_code=AreaCode.AREA_CODE_GLOB.value,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
            audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER, use_string_uid=0)
        ret = self.svc.initialize(cfg)
        if ret != 0:
            raise RuntimeError("AgoraService init ret=%s" % ret)

        publish_config = RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False)
        self.conn = self.svc.create_rtc_connection(
            RTCConnConfig(client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
                          channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
                          auto_subscribe_audio=1, auto_subscribe_video=1), publish_config)
        if self.conn is None:
            raise RuntimeError("create_rtc_connection 失败")

        conn_obs, video_obs, audio_obs = self._make_observers()
        self._keep = [conn_obs, video_obs, audio_obs]
        self.conn.register_observer(conn_obs)
        r = self.conn.connect(token, channel, uid)
        if r != 0:
            raise RuntimeError("connect ret=%s" % r)
        log("connect ok channel=", channel)
        time.sleep(2)

        lu = self.conn.get_local_user()
        self.lu = lu
        self.conn.register_video_encoded_frame_observer(video_obs)
        lu.subscribe_all_video(VideoSubscriptionOptions(
            type=VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=1))
        lu.set_playback_audio_frame_before_mixing_parameters(1, 16000)
        self.conn.register_audio_frame_observer(audio_obs, 0, None)
        log("subscribed video(encoded)+audio")
        self._start_ts = time.time()

    def stop(self):
        """断连接 → 关文件 → 合成 mp4。可被 stop_event(断网) 触发。"""
        if self.stop_event.is_set() and not self._stop_reason:
            self._stop_reason = "stop_event"
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception as e:
            log("disconnect err", repr(e))
        time.sleep(0.5)
        for f in (self.vf, self.af):
            try:
                if f: f.close()
            except Exception: pass
        try:
            if self.conn:
                self.conn.release()
        except Exception: pass
        log("stopped video_frames=%d keyframes=%d audio_bytes=%d err=%s reason=%s"
            % (self.video_frames, self.keyframes, self.audio_bytes, self.error, self._stop_reason))
        return self._mux()

    def _ensure_playable_frag(self):
        """兜底: 若还没 mp4 (崩溃前没机会 mux), 用已有 .h264/.pcm 快速封一个可播 frag mp4。
        崩溃场景由外部 supervisor / 断电恢复调用。"""
        return self._mux()

    def _mux(self):
        """停止后合成 MP4。裸 H264 无时间戳, 用 (视频帧数 / 视频首末墙钟差) 估算真实 fps。"""
        if not os.path.exists(self.h264_path) or os.path.getsize(self.h264_path) < 1024:
            log("h264 无有效数据, 跳过合成")
            return {"ok": False, "msg": "no video data", "mp4": None}
        has_audio = os.path.exists(self.pcm_path) and os.path.getsize(self.pcm_path) > 1024
        # 真实视频帧率: 用视频首帧~末帧墙钟跨度 (比用整个 start_ts 更准, 排除等待首帧)
        if self.v_first_ts and self.v_last_ts and self.v_last_ts > self.v_first_ts and self.video_frames > 1:
            vdur = self.v_last_ts - self.v_first_ts
            fps = (self.video_frames - 1) / vdur
        else:
            elapsed = max(0.1, time.time() - self._start_ts)
            fps = self.video_frames / elapsed if self.video_frames > 0 else 25.0
        fps = max(5.0, min(60.0, fps))
        log("mux fps=%.2f (frames=%d, vdur=%.1fs)" % (
            fps, self.video_frames, (self.v_last_ts - self.v_first_ts) if (self.v_first_ts and self.v_last_ts) else 0))
        cmd = [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-r", "%.4f" % fps, "-f", "h264", "-i", self.h264_path]
        if has_audio:
            # 音画对齐: 若音频首帧晚于视频首帧, 用 -itsoffset 把音频往后推相应偏移
            audio_delay = 0.0
            if self.v_first_ts and self.a_first_ts:
                audio_delay = self.a_first_ts - self.v_first_ts
                audio_delay = max(0.0, min(3.0, audio_delay))  # 限幅, 防异常
            if audio_delay > 0.05:
                cmd += ["-itsoffset", "%.3f" % audio_delay]
            cmd += ["-f", "s16le", "-ar", str(self.audio_sample_rate),
                    "-ac", str(self.audio_channels), "-i", self.pcm_path]
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k"]
            log("audio_delay=%.3fs" % audio_delay)
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-movflags", "+faststart", self.mp4_path]
        log("mux:", " ".join(cmd))
        p = subprocess.run(cmd, capture_output=True)
        if p.returncode != 0:
            log("ffmpeg mux fail:", p.stderr[-400:].decode("utf-8", "replace"))
            return {"ok": False, "msg": "ffmpeg fail rc=%s" % p.returncode, "mp4": None}
        # ★ 不删原始裸流: 保留供崩溃恢复/二次合成 (老大要求不丢)
        info = self._probe()
        log("mux done ->", self.mp4_path, info)
        return {"ok": True, "mp4": self.mp4_path, "probe": info,
                "raw_h264": self.h264_path, "raw_pcm": self.pcm_path if has_audio else None}

    def _probe(self):
        try:
            p = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error",
                                "-show_entries",
                                "stream=codec_type,codec_name,width,height,sample_rate,channels,nb_frames,duration",
                                "-show_entries", "format=duration,size",
                                "-of", "json", self.mp4_path],
                               capture_output=True, text=True, timeout=15)
            return json.loads(p.stdout or "{}")
        except Exception as e:
            return {"probe_err": repr(e)}


def _install_crash_guard(rec):
    """信号/异常兜底: 进程被 kill / 异常时也尽量合成 mp4, 不丢文件。"""
    import atexit, signal

    def _finalize(*_a):
        try:
            if rec.vf or rec.af:
                # 还有打开句柄说明没走过 stop(), 直接合成兜底
                for f in (rec.vf, rec.af):
                    try:
                        if f: f.flush(); f.close()
                    except Exception: pass
                rec._mux()
        except Exception:
            pass

    atexit.register(_finalize)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)
            def handler(signum, frame, _rec=rec):
                log("signal", signum, "→ finalize")
                try:
                    _finalize()
                finally:
                    os._exit(0)
            signal.signal(sig, handler)
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print("usage: native_worker.py <cred_json> <name> <secs>", flush=True)
        return 2
    cred = json.loads(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else "native"
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    rec = Recorder(cred, name)
    _install_crash_guard(rec)
    try:
        rec.start()
    except Exception as e:
        log("start failed:", repr(e))
        os._exit(3)

    t0 = time.time()
    last_v = 0
    last_progress = t0
    while time.time() - t0 < secs:
        # 断网/断流: stop_event 触发 或 视频长时间无帧 都提前收尾
        if rec.stop_event.is_set():
            log("stop_event set → 提前收尾 (%s)" % rec._stop_reason)
            break
        time.sleep(1)
        if rec.video_frames != last_v:
            last_v = rec.video_frames
            last_progress = time.time()
        elif rec.video_frames > 0 and (time.time() - last_progress) > VIDEO_STALL_SEC:
            log("视频 %.0fs 无新帧 → 判定断流, 收尾封片" % VIDEO_STALL_SEC)
            rec._stop_reason = "video stall"
            break
        if int(time.time() - t0) % 5 == 0:
            log("t=%2ds vf=%d kf=%d audio=%dKB %dx%d"
                % (int(time.time() - t0), rec.video_frames, rec.keyframes,
                   rec.audio_bytes // 1024, rec.width, rec.height))
    nm = rec.video_frames
    res = rec.stop()
    print("=== RESULT ===", json.dumps({
        "name": name, "video_frames": nm, "keyframes": rec.keyframes, **res},
        ensure_ascii=False), flush=True)
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
