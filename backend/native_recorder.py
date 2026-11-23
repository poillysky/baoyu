#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生 Agora RTC 录制 — 完全按官方 SDK 标准结构重写。

基于官方示例:
  - example_video_encoded_receive.py  (encoded H264 直通)
  - example_pcm_yuv_receive.py        (PCM 音频 + YUV 视频)
  - example_base.py                   (连接生命周期)

架构:
  AgoraService (全局单例)
    └─ RTCConnection (每房间一个)
         ├─ IRTCConnectionObserver    → 连接状态
         ├─ IRTCLocalUserObserver     → 远端轨订阅/关键帧请求
         ├─ IAudioFrameObserver       → PCM 音频 → ffmpeg AAC
         └─ IVideoEncodedFrameObserver → H264 码流 → ffmpeg -c:v copy

视频: encoded H264 直通 MP4 (不解码, CPU 最低)
音频: 16k mono s16 PCM → AAC
封装: fragmented MP4 (frag_keyframe+empty_moov)
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import config as C

# ---- SDK 路径 ----
HERE = Path(__file__).resolve().parent
VENV_LIB = HERE / ".venv-native2410" / "lib" / "python3.12" / "site-packages"
if VENV_LIB.is_dir() and str(VENV_LIB) not in sys.path:
    sys.path.insert(0, str(VENV_LIB))

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RtcConnectionPublishConfig
from agora.rtc.rtc_connection import RTCConnConfig
from agora.rtc.agora_base import (
    VideoSubscriptionOptions, AudioSubscriptionOptions, ClientRoleType, ChannelProfileType,
    AreaCode, AudioScenarioType, AudioProfileType, AudioPublishType, VideoPublishType,
    VideoStreamType, VideoCodecType, TCcMode, SenderOptions,
)
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc.audio_frame_observer import IAudioFrameObserver
from agora.rtc.video_frame_observer import IVideoFrameObserver
from agora.rtc.video_encoded_frame_observer import IVideoEncodedFrameObserver


def log(*args):
    print("[NATIVE-REC]", *args, flush=True)


def _safe_name(value):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(value or "")).strip("_")


# ═══════════════════════════════════════════════════════════════════
# Observers — 严格按官方接口签名
# ═══════════════════════════════════════════════════════════════════

class _ConnObserver(IRTCConnectionObserver):
    """对应官方 observer/connection_observer.py"""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def on_connected(self, agora_rtc_conn, conn_info, reason):
        log(self.task.rec_id, "on_connected, reason=", reason)
        if self.task.state != "stopping":
            self.task.state = "connected"
        self.task.connected_event.set()

    def on_disconnected(self, agora_rtc_conn, conn_info, reason):
        log(self.task.rec_id, "on_disconnected, reason=", reason)
        if not self.task.stop_event.is_set():
            self.task.error = "RTC 断连 reason=%s" % reason
            self.task.state = "interrupted"

    def on_reconnecting(self, agora_rtc_conn, conn_info, reason):
        log(self.task.rec_id, "on_reconnecting, reason=", reason)

    def on_reconnected(self, agora_rtc_conn, conn_info, reason):
        log(self.task.rec_id, "on_reconnected, reason=", reason)

    def on_error(self, agora_rtc_conn, error, msg):
        self.task.error = "RTC error=%s %s" % (error, (msg or b"").decode("utf-8", "replace"))
        log(self.task.rec_id, self.task.error)

    def on_user_joined(self, agora_rtc_conn, user_id):
        log(self.task.rec_id, "on_user_joined, uid=", user_id)
        self.task.remote_uid = user_id

    def on_user_left(self, agora_rtc_conn, user_id, reason):
        log(self.task.rec_id, "on_user_left, uid=", user_id, "reason=", reason)

    def on_aiqos_capability_missing(self, agora_rtc_conn, recommend_audio_scenario):
        log(self.task.rec_id, "on_aiqos_capability_missing, recommend=", recommend_audio_scenario)
        return recommend_audio_scenario


class _LocalUserObserver(IRTCLocalUserObserver):
    """对应官方 observer/local_user_observer.py + 视频订阅回调"""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def on_user_video_track_subscribed(self, agora_local_user, user_id, info, agora_remote_video_track):
        """远端视频轨订阅成功 → 保存 track 引用 + 主动请求关键帧"""
        log(self.task.rec_id, "on_user_video_track_subscribed, uid=", user_id,
            "codec=", info.codec_type if info else "?",
            "encoded_frame_only=", info.encoded_frame_only if info else "?")
        self.task.remote_video_track = agora_remote_video_track
        # 主动请求关键帧 — 官方 README 2.4.2: send_intra_request
        # 从独立线程调用，避免在回调线程中阻塞 SDK
        self.task._request_keyframe(user_id)

    def on_first_remote_video_frame(self, agora_local_user, user_id, width, height, elapsed):
        log(self.task.rec_id, "on_first_remote_video_frame, uid=", user_id,
            width, "x", height, "elapsed=", elapsed)

    def on_first_remote_video_decoded(self, agora_local_user, user_id, width, height, elapsed):
        log(self.task.rec_id, "on_first_remote_video_decoded, uid=", user_id,
            width, "x", height, "elapsed=", elapsed)

    def on_user_video_track_state_changed(self, agora_local_user, user_id,
                                          agora_remote_video_track, state, reason, elapsed):
        log(self.task.rec_id, "video_track_state: uid=", user_id,
            "state=", state, "reason=", reason, "elapsed=", elapsed)
        # state=3 reason=1 → 视频轨已就绪，再请求一次关键帧
        if state == 3 and reason == 1:
            self.task._request_keyframe(user_id)

    def on_user_audio_track_state_changed(self, agora_local_user, user_id,
                                          agora_remote_audio_track, state, reason, elapsed):
        pass

    def on_user_info_updated(self, agora_local_user, user_id, msg, val):
        pass


class _AudioObserver(IAudioFrameObserver):
    """对应官方 observer/audio_frame_observer.py"""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def on_playback_audio_frame_before_mixing(self, agora_local_user, channelId, uid,
                                              frame, vad_result_state=-1, vad_result_bytearray=None):
        try:
            size = frame.samples_per_channel * frame.bytes_per_sample * frame.channels
            if size <= 0 or not frame.buffer:
                return 1
            data = bytes(frame.buffer[:size])
            self.task.audio_frames += 1
            self.task.bytes += len(data)
            self.task._put(self.task.audio_q, (data, frame.samples_per_sec, frame.channels))
        except Exception as exc:
            self.task.error = "音频回调异常: %r" % exc
        return 1

    def on_get_playback_audio_frame_param(self, agora_local_user):
        from agora.rtc.agora_base import AudioParams
        p = AudioParams()
        p.sample_rate = 16000
        p.channels = 1
        p.mode = 0
        p.samples_per_call = 640
        return p

    def on_get_audio_frame_position(self, agora_local_user):
        return 0


class _EncodedVideoObserver(IVideoEncodedFrameObserver):
    """对应官方 observer/video_encoded_frame_observer.py — H264 直通"""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def on_encoded_video_frame(self, uid, image_buffer, length, video_encoded_frame_info):
        try:
            if length <= 0:
                return 1
            info = video_encoded_frame_info
            codec = info.codec_type
            if codec != 2:
                self.task.codec_seen = codec
            data = bytes(image_buffer[:length])
            self.task.width = info.width or self.task.width
            self.task.height = info.height or self.task.height
            self.task.video_frames += 1
            self.task.bytes += len(data)
            if info.frame_type == 3:
                self.task.keyframes += 1
                # 首个关键帧到达后启动 ffmpeg（确保 SPS/PPS）
                self.task.first_keyframe_event.set()
            self.task._put(self.task.video_q, data)
            if self.task.codec == "":
                self.task.codec = "h264"
        except Exception as exc:
            self.task.error = "编码视频回调异常: %r" % exc
        return 1


class _RawVideoObserver(IVideoFrameObserver):
    """对应官方 observer/video_frame_observer.py — raw YUV（备用 B 路线）"""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def on_frame(self, agora_rtc_conn, remote_uid, frame):
        # ★ v4.4.32 实测签名: (conn, uid, frame)  ── 旧版写成 (channel_id, remote_uid, frame) 会收不到
        try:
            width, height = frame.width, frame.height
            if width <= 0 or height <= 0:
                return 1
            ys, us, vs = frame.y_stride, frame.u_stride, frame.v_stride
            cw, ch = (width + 1) // 2, (height + 1) // 2
            # 快路径: stride 对齐时零拷贝直取(720x1280 绝大多数如此)
            if ys == width and us == cw and vs == cw:
                data = bytes(frame.y_buffer[:ys * height]) + \
                       bytes(frame.u_buffer[:us * ch]) + \
                       bytes(frame.v_buffer[:vs * ch])
            else:
                y = b"".join(bytes(frame.y_buffer[r*ys:r*ys+width]) for r in range(height))
                u = b"".join(bytes(frame.u_buffer[r*us:r*us+cw]) for r in range(ch))
                v = b"".join(bytes(frame.v_buffer[r*vs:r*vs+cw]) for r in range(ch))
                data = y + u + v
            self.task.width, self.task.height = width, height
            self.task.fps = self.task.fps or 25
            self.task.codec = "i420"
            self.task.video_frames += 1
            self.task.bytes += len(data)
            self.task._put(self.task.video_q, data)
        except Exception as exc:
            self.task.error = "视频回调异常: %r" % exc
        return 1

    def on_user_video_track_subscribed(self, agora_local_user, user_id, info, agora_remote_video_track):
        log(self.task.rec_id, "raw_video: on_user_video_track_subscribed uid=", user_id)
        return 0


# ═══════════════════════════════════════════════════════════════════
# NativeRecordTask — 单个房间录制
# ═══════════════════════════════════════════════════════════════════

class NativeRecordTask:
    def __init__(self, rec_id, room_id, plat_id, cred, name=None):
        self.rec_id, self.room_id, self.plat_id = rec_id, room_id, plat_id
        self.cred, self.name = cred, name
        self.started_at = time.time()
        self.state = "starting"
        self.error = ""
        self.bytes = 0
        self.video_frames = 0
        self.audio_frames = 0
        self.keyframes = 0
        self.dropped = 0
        self.width = self.height = self.fps = 0
        self.codec = ""
        self.codec_seen = 0
        self.remote_uid = ""
        self.remote_video_track = None
        self.final_mp4 = None
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self.first_keyframe_event = threading.Event()
        self.video_q = queue.Queue(maxsize=120)
        self.audio_q = queue.Queue(maxsize=300)
        self.ffmpeg = None
        self.audio_fd = None
        self.conn = None
        self.local_user = None
        self.writer_thread = None
        self._callback_refs = []

        anchor = _safe_name(name) or str(room_id)
        anchor = "%s_%s" % (anchor, room_id) if name else anchor
        out_dir = Path(C.REC_DIR) / anchor
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = "%s_%s.mp4" % (_safe_name(name) or ("ROOM_%s" % room_id), stamp)
        self.mp4_path = str(out_dir / filename)

    def _put(self, target, item):
        try:
            target.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            try:
                target.get_nowait()
                target.put_nowait(item)
            except Exception:
                pass

    def _resubscribe_watch(self):
        """raw YUV 订阅存在竞态(track 就绪慢), 若 6 秒内无视频帧则重订阅, 最多 6 次"""
        deadline = time.time() + 6.0
        tries = 0
        while not self.stop_event.is_set() and tries < 6:
            if self.video_frames > 0:
                log(self.rec_id, "resubscribe_watch: 已收帧, 退出")
                return
            time.sleep(0.5)
            if time.time() < deadline:
                continue
            tries += 1
            try:
                if self.local_user:
                    r = self.local_user.subscribe_all_video(VideoSubscriptionOptions(
                        type=VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=0))
                    log(self.rec_id, "resubscribe #%d ret=%s vf=%d" % (tries, r, self.video_frames))
            except Exception as exc:
                log(self.rec_id, "resubscribe err", repr(exc))
            deadline = time.time() + 6.0

    def _bootstrap_keyframe(self):
        """订阅后延迟请求关键帧, 确保拿到首帧(keyframe)"""
        time.sleep(1.0)
        uid = self.remote_uid
        if uid:
            for _ in range(3):
                self._request_keyframe(uid)
                time.sleep(1.5)

    def _request_keyframe(self, uid):
        """在独立线程中调用 send_intra_request，避免阻塞 SDK 回调线程"""
        def _do():
            try:
                if self.conn:
                    ret = self.conn.send_intra_request(str(uid))
                    log(self.rec_id, "send_intra_request uid=", uid, "ret=", ret)
            except Exception as exc:
                log(self.rec_id, "send_intra_request err:", repr(exc))
        t = threading.Thread(target=_do, daemon=True)
        t.start()

    # ── ffmpeg ───────────────────────────────────────────────────

    def _start_ffmpeg(self):
        """H264 Annex-B 直通(-c:v copy) + 音频 s16le 16k mono → AAC"""
        if self.ffmpeg:
            return
        audio_read, audio_write = os.pipe()
        cmd = [C.FFMPEG, "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts+igndts", "-flags", "low_delay",
               "-f", "h264", "-i", "pipe:0",
               "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:%d" % audio_read,
               "-map", "0:v:0", "-map", "1:a:0?",
               "-c:v", "copy",
               "-c:a", "aac", "-b:a", "96k",
               "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
               "-y", self.mp4_path]
        try:
            self.ffmpeg = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, pass_fds=(audio_read,), bufsize=0)
            self.audio_fd = audio_write
            os.close(audio_read)
            log(self.rec_id, "ffmpeg[h264 copy] ->", self.mp4_path)
        except Exception:
            os.close(audio_read)
            os.close(audio_write)
            raise

    def _writer(self):
        """独立线程：从队列取数据写入 ffmpeg"""
        while not self.stop_event.is_set() or not self.video_q.empty() or not self.audio_q.empty():
            try:
                video = self.video_q.get(timeout=0.05)
            except queue.Empty:
                video = None
            if video is not None:
                try:
                    if not self.ffmpeg:
                        self._start_ffmpeg()
                    if self.ffmpeg and self.ffmpeg.stdin:
                        self.ffmpeg.stdin.write(video)
                        self.ffmpeg.stdin.flush()
                        if self.state in ("starting", "connected"):
                            self.state = "recording"
                except Exception as exc:
                    self.error = "ffmpeg 视频写入失败: %r" % exc
                    break
            # 音频批量写入
            for _ in range(120):
                try:
                    audio = self.audio_q.get_nowait()
                except queue.Empty:
                    break
                if self.ffmpeg and self.audio_fd is not None:
                    data, sample_rate, channels = audio
                    if sample_rate == 16000 and channels == 1:
                        try:
                            os.write(self.audio_fd, data)
                        except Exception as exc:
                            self.error = "ffmpeg 音频写入失败: %r" % exc
                            return

    # ── 生命周期（严格按官方 example_base.py 流程）────────────────

    def start(self):
        """
        官方流程:
          1. create_rtc_connection(con_config, publish_config)
          2. register_observer(conn_observer)
          3. connect(token, channel, uid)
          4. get_local_user()
          5. register_local_user_observer(local_user_observer)
          6. setup_in_connection()  ← 在这里注册收流回调
          7. await exit
          8. disconnect() + release()
        """
        # ── Step 1: 配置 ──
        # ★ 极简 con_config: 实测带 audio_subs_options/enable_audio_recording_or_playout
        #    会干扰 raw video 通道导致 on_frame 不触发。音频 PCM 走 set_playback_audio_frame_before_mixing
        con_config = RTCConnConfig(
            auto_subscribe_audio=1,
            auto_subscribe_video=1,
            client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        publish_config = RtcConnectionPublishConfig(
            audio_profile=AudioProfileType.AUDIO_PROFILE_DEFAULT,
            audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER,
            is_publish_audio=False,       # ★ 观众只收不发
            is_publish_video=False,
        )

        # ── Step 2: create_rtc_connection ──
        self.conn = _get_service().create_rtc_connection(con_config, publish_config)
        if self.conn is None:
            raise RuntimeError("create_rtc_connection 失败")

        # ── Step 3: register_observer + connect ──
        self._conn_obs = _ConnObserver(self)
        self.conn.register_observer(self._conn_obs)

        self.writer_thread = threading.Thread(target=self._writer, daemon=True)
        self.writer_thread.start()

        ret = self.conn.connect(self.cred["token"], str(self.cred["channel"]), str(self.cred["uid"]))
        if ret != 0:
            raise RuntimeError("RTC Connect 返回 %s" % ret)
        log(self.rec_id, "connect ret=0, channel=", self.cred["channel"])

        if not self.connected_event.wait(timeout=15):
            raise RuntimeError("RTC 等待 on_connected 超时")

        # ★ 已验证稳定的关键顺序（agora_stable.py 三连测 104/165/168 帧）:
        #   on_connected 之后 → 注册 video observer → subscribe_all_video(raw) → 再挂音频
        self.local_user = self.conn.get_local_user()
        time.sleep(0.5)

        # ── Step 4: 编码视频通道(H264) observer + 订阅  ← 实测稳定(232帧/10s, 5-9KB/帧) ──
        self._video_obs = _EncodedVideoObserver(self)
        self._local_obs = _LocalUserObserver(self)
        self._audio_obs = _AudioObserver(self)
        r_video = self.conn.register_video_encoded_frame_observer(self._video_obs)
        log(self.rec_id, "register_video_encoded_frame_observer ret=", r_video)
        r_lu = self.local_user._register_local_user_observer(self._local_obs)
        log(self.rec_id, "register_local_user_observer ret=", r_lu)

        # ── Step 5: 订阅 encoded 视频流 ──
        r_sub = self.local_user.subscribe_all_video(VideoSubscriptionOptions(
            type=VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=1))
        log(self.rec_id, "subscribe_all_video(encoded) ret=", r_sub)

        # ── Step 6: 音频订阅 ──
        self.local_user.set_playback_audio_frame_before_mixing_parameters(1, 16000)
        r_audio = self.conn.register_audio_frame_observer(self._audio_obs, 0, None)
        log(self.rec_id, "register_audio_frame_observer ret=", r_audio)

        # ── Step 7: 兜底请求关键帧 ──
        threading.Thread(target=self._bootstrap_keyframe, daemon=True).start()

        self._callback_refs = [self._conn_obs, self._video_obs, self._local_obs, self._audio_obs]
        return ret

    def stop(self):
        """官方流程: disconnect() → release()"""
        if self.stop_event.is_set():
            return
        self.state = "stopping"
        self.stop_event.set()

        # disconnect
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception as exc:
            log(self.rec_id, "disconnect err:", repr(exc))

        # 等 writer 收尾
        if self.writer_thread:
            self.writer_thread.join(timeout=3)

        # 关 ffmpeg stdin
        try:
            if self.ffmpeg and self.ffmpeg.stdin:
                self.ffmpeg.stdin.close()
                self.ffmpeg.stdin = None
        except Exception:
            pass
        try:
            if self.audio_fd is not None:
                os.close(self.audio_fd)
                self.audio_fd = None
        except Exception:
            pass

        # 等 ffmpeg 退出
        if self.ffmpeg:
            try:
                _, err = self.ffmpeg.communicate(timeout=8)
                if self.ffmpeg.returncode != 0 and err:
                    self.error = err[-400:].decode("utf-8", "replace")
            except Exception:
                self.ffmpeg.kill()
                self.ffmpeg.communicate()

        # release connection
        try:
            if self.conn:
                self.conn.release()
                self.conn = None
        except Exception:
            pass

        # 清空引用
        self._conn_obs = None
        self._local_obs = None
        self._audio_obs = None
        self._video_obs = None
        self._callback_refs = []
        self.remote_video_track = None
        self.local_user = None

        # 判定结果
        if os.path.exists(self.mp4_path) and os.path.getsize(self.mp4_path) > 1024:
            self.final_mp4 = self.mp4_path
            self.state = "done"
        else:
            self.state = "failed"
        log(self.rec_id, "stopped", self.state, self.mp4_path,
            "video_frames=", self.video_frames, "keyframes=", self.keyframes,
            "audio_frames=", self.audio_frames, self.error[:120])

    def status(self):
        return {
            "rec_id": self.rec_id, "room_id": self.room_id, "plat_id": self.plat_id,
            "name": self.name, "state": self.state, "bytes": self.bytes,
            "mp4": self.final_mp4 if self.state == "done" else None,
            "segments": [self.final_mp4] if self.final_mp4 else [],
            "started_at": self.started_at, "retry": 0, "engine": "native_v4_h264",
            "width": self.width, "height": self.height, "fps": self.fps,
            "codec": self.codec, "codec_seen": self.codec_seen,
            "video_frames": self.video_frames, "audio_frames": self.audio_frames,
            "keyframes": self.keyframes, "remote_uid": self.remote_uid,
            "dropped": self.dropped, "error": self.error,
        }


# ═══════════════════════════════════════════════════════════════════
# AgoraService 单例（官方: 一个进程只能有一个）
# ═══════════════════════════════════════════════════════════════════

_TASKS = {}
_TASKS_LOCK = threading.Lock()
_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def _get_service():
    """官方: A process can only have one instance"""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE:
            return _SERVICE
        service = AgoraService()
        cfg = AgoraServiceConfig(
            log_path=str(HERE / "native_agora.log"),
            log_file_size_kb=1024,
            enable_audio_processor=0,
            enable_audio_device=0,
            enable_video=1,
            appid="84437b2ceb75422ab1f7df9c2689241d",
            area_code=AreaCode.AREA_CODE_GLOB.value,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
            audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER,
            use_string_uid=0,
        )
        ret = service.initialize(cfg)
        if ret != 0 or not service.inited:
            raise RuntimeError("AgoraService 初始化失败 ret=%s" % ret)
        log("AgoraService initialized (native v4.4.32)")
        _SERVICE = service
        return service


# ═══════════════════════════════════════════════════════════════════
# 公开 API（与 recorder.py 接口一致）
# ═══════════════════════════════════════════════════════════════════

def start_record(room_id, plat_id, cred, name=None):
    _get_service()
    rec_id = uuid.uuid4().hex[:12]
    task = NativeRecordTask(rec_id, room_id, plat_id, cred, name)
    with _TASKS_LOCK:
        _TASKS[rec_id] = task
    try:
        task.start()
    except Exception as exc:
        task.error = repr(exc)
        task.state = "failed"
        task.stop_event.set()
        log("start failed", rec_id, repr(exc))
    return {"ok": task.state != "failed", "rec_id": rec_id, "state": task.state,
            "mp4": task.mp4_path, "name": name, "msg": task.error}


def stop_record(rec_id):
    task = _TASKS.get(rec_id)
    if not task:
        return {"ok": False, "msg": "无此录制任务"}
    task.stop()
    return {"ok": task.state == "done", "rec_id": rec_id, "mp4": task.final_mp4,
            "state": task.state, "segments": [task.final_mp4] if task.final_mp4 else [],
            "msg": task.error}


def status():
    with _TASKS_LOCK:
        tasks = list(_TASKS.values())
    return [t.status() for t in tasks]


# ═══════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="原生 Agora RTC 录制器")
    parser.add_argument("channel", help="Agora 频道 ID")
    parser.add_argument("--seconds", type=int, default=15, help="录制秒数")
    parser.add_argument("--room-id", default="50188", help="房间 ID")
    parser.add_argument("--name", default="native_test", help="录制名称")
    args = parser.parse_args()

    import stream
    cred = stream.get_token(args.channel)
    result = start_record(args.room_id, 449, cred, args.name)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result["ok"]:
        return 2
    rec_id = result["rec_id"]
    try:
        time.sleep(args.seconds)
        cur = [x for x in status() if x["rec_id"] == rec_id]
        print(json.dumps(cur[0] if cur else {}, ensure_ascii=False), flush=True)
    finally:
        print(json.dumps(stop_record(rec_id), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
