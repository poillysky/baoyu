#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_record_direct.py — Agora 官方标准「单文件直录」方案。

一条 ffmpeg 进程边收边封装成 MP4：
  - 视频: on_encoded_video_frame 的 H264 码流 → ffmpeg pipe:0
  - 音频: on_playback_audio_frame_before_mixing 的 PCM → ffmpeg pipe:1 (独立 fd)
  - ffmpeg 输出 frag_keyframe+empty_moov 的 mp4（渐进可播，不用等封尾）
  - -c:v copy 视频零重编码；音频 aac

不再落 .h264/.pcm 中间裸文件、不事后 mux。录制全程单进程单文件。

环境: .venv-native2410 (Agora SDK 4.10)。用法:
  python3 native_record_direct.py <cred_json> <name> <secs>
"""
import sys, os, json, time, subprocess, threading, collections

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
    print("[DIRECT]", *a, flush=True)


class DirectRecorder:
    """一条 ffmpeg，直录单文件 mp4。"""

    def __init__(self, cred, name, out_dir=None, audio_sr=16000, audio_ch=1):
        self.cred = cred
        self.name = name or "native"
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_dir = out_dir or C.REC_DIR
        os.makedirs(out_dir, exist_ok=True)
        base = "%s_%s" % (name or ("ROOM_%s" % cred.get("channel")), ts)
        self.mp4_path = os.path.join(out_dir, base + ".mp4")
        self.audio_sr = audio_sr
        self.audio_ch = audio_ch
        self.ff = None
        self.video_frames = 0
        self.keyframes = 0
        self.audio_bytes = 0
        self.width = self.height = 0
        self.error = ""
        self.conn = self.svc = None
        self._started = False
        self._start_ts = time.time()
        self._seen_rtp = collections.deque(maxlen=256)
        self._lock = threading.Lock()
        self._keep = []

    # ---- ffmpeg 单文件直录 ----
    def _start_ffmpeg(self):
        if self.ff:
            return
        # 音频从 fd 3 读（避免与视频 stdin 混用），视频走 stdin(pipe:0)
        a_r, a_w = os.pipe()
        os.set_inheritable(a_r, True)
        cmd = [
            C.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "h264", "-i", "pipe:0",
            "-f", "s16le", "-ar", str(self.audio_sr), "-ac", str(self.audio_ch),
            "-i", "pipe:3",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            self.mp4_path,
        ]
        self.ff = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, pass_fds=(a_r,),
            preexec_fn=lambda: os.dup2(a_r, 3))
        os.close(a_r)          # 子进程已继承 fd3
        self.audio_fd = a_w
        self._started = True
        log("ffmpeg direct -> %s" % self.mp4_path)

    def _write_video(self, data: bytes):
        if not self._started:
            self._start_ffmpeg()
        try:
            self.ff.stdin.write(data)
            self.ff.stdin.flush()
        except Exception as e:
            self.error = "video pipe: %r" % e

    def _write_audio(self, data: bytes):
        if not self._started:
            return               # 视频关键帧先到再开 ffmpeg
        try:
            os.write(self.audio_fd, data)
        except Exception as e:
            self.error = "audio pipe: %r" % e

    # ---- observers ----
    def _make_observers(self):
        rec = self

        class Conn(IRTCConnectionObserver):
            def on_connected(self, c, i, r):
                log("on_connected reason=", r)
            def on_disconnected(self, c, i, r):
                log("on_disconnected reason=", r)
            def on_error(self, c, e, msg):
                rec.error = "conn err=%s" % e

        class VideoObs(IVideoEncodedFrameObserver):
            def on_encoded_video_frame(self, uid, buf, length, info):
                if length <= 0:
                    return 1
                try:
                    if not rec._started:
                        if info.frame_type != 3:      # 等关键帧, 保证 SPS/PPS
                            return 1
                        rec._start_ffmpeg()
                    rec._write_video(bytes(buf[:length]))
                    rec.video_frames += 1
                    if info.frame_type == 3:
                        rec.keyframes += 1
                    rec.width = info.width or rec.width
                    rec.height = info.height or rec.height
                except Exception as e:
                    rec.error = "video cb: %r" % e
                return 1

        class AudioObs(IAudioFrameObserver):
            def on_playback_audio_frame_before_mixing(self, lu, chid, uid, frame, *a):
                try:
                    rtp = frame.rtp_timestamp
                    # 同一 rtp 回调解多次, 去重防止音频重复灌入
                    with rec._lock:
                        if rtp in rec._seen_rtp:
                            return 1
                        rec._seen_rtp.append(rtp)
                    n = frame.samples_per_channel * frame.bytes_per_sample * frame.channels
                    if n <= 0 or not frame.buffer:
                        return 1
                    rec._write_audio(bytes(frame.buffer[:n]))
                    rec.audio_bytes += n
                except Exception as e:
                    rec.error = "audio cb: %r" % e
                return 1
            def on_get_audio_frame_position(self, lu):
                return 0
            def on_get_playback_audio_frame_param(self, lu):
                p = AudioParams()
                p.sample_rate = rec.audio_sr
                p.channels = rec.audio_ch
                p.mode = 0
                p.samples_per_call = 640
                return p

        return Conn(), VideoObs(), AudioObs()

    def start(self):
        cred = self.cred
        self.svc = AgoraService()
        cfg = AgoraServiceConfig(
            log_path=os.path.join(HERE, "native_agora.log"), log_file_size_kb=1024,
            enable_audio_processor=0, enable_audio_device=0, enable_video=1,
            appid=cred["appid"], area_code=AreaCode.AREA_CODE_GLOB.value,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
            audio_scenario=AudioScenarioType.AUDIO_SCENARIO_AI_SERVER, use_string_uid=0)
        if self.svc.initialize(cfg) != 0:
            raise RuntimeError("AgoraService init 失败")
        pub = RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False)
        self.conn = self.svc.create_rtc_connection(
            RTCConnConfig(client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
                          channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
                          auto_subscribe_audio=1, auto_subscribe_video=1), pub)
        if self.conn is None:
            raise RuntimeError("create_rtc_connection 失败")
        conn_obs, video_obs, audio_obs = self._make_observers()
        self._keep = [conn_obs, video_obs, audio_obs]
        self.conn.register_observer(conn_obs)
        r = self.conn.connect(cred["token"], str(cred["channel"]), str(cred.get("uid") or 0))
        if r != 0:
            raise RuntimeError("connect ret=%s" % r)
        log("connect ok channel=", cred["channel"])
        time.sleep(2)
        lu = self.conn.get_local_user()
        self.lu = lu
        self.conn.register_video_encoded_frame_observer(video_obs)
        lu.subscribe_all_video(VideoSubscriptionOptions(
            type=VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=1))
        lu.set_playback_audio_frame_before_mixing_parameters(self.audio_ch, self.audio_sr)
        self.conn.register_audio_frame_observer(audio_obs, 0, None)
        self._start_ts = time.time()
        log("subscribed video(encoded)+audio, 直录开始")

    def stop(self):
        try:
            if self.conn:
                self.conn.disconnect()
        except Exception as e:
            log("disconnect err", repr(e))
        time.sleep(0.5)
        # 关闭 ffmpeg：先关音频写端 → EOF，再关视频 stdin → EOF，ffmpeg 收尾封 mp4
        try:
            if getattr(self, "audio_fd", None) is not None:
                os.close(self.audio_fd)
        except Exception:
            pass
        try:
            if self.ff and self.ff.stdin:
                self.ff.stdin.close()
        except Exception:
            pass
        try:
            if self.ff:
                rc = self.ff.wait(timeout=30)
                log("ffmpeg exit rc=", rc)
                err = self.ff.stderr.read().decode("utf-8", "replace")[-500:] if self.ff.stderr else ""
                if err.strip():
                    log("ffmpeg stderr tail:", err)
        except Exception as e:
            log("ffmpeg wait err", repr(e))
        try:
            if self.conn:
                self.conn.release()
        except Exception:
            pass
        info = self._probe()
        log("stopped vf=%d kf=%d audio=%dKB err=%s -> %s %s"
            % (self.video_frames, self.keyframes, self.audio_bytes // 1024, self.error,
               self.mp4_path, info))
        return {"ok": os.path.exists(self.mp4_path) and os.path.getsize(self.mp4_path) > 1024,
                "mp4": self.mp4_path, "probe": info,
                "video_frames": self.video_frames, "keyframes": self.keyframes}

    def _probe(self):
        try:
            p = subprocess.run(
                ["/opt/homebrew/bin/ffprobe", "-v", "error",
                 "-show_entries",
                 "stream=codec_type,codec_name,width,height,sample_rate,channels,nb_frames,duration",
                 "-show_entries", "format=duration,size",
                 "-of", "json", self.mp4_path],
                capture_output=True, text=True, timeout=15)
            return json.loads(p.stdout or "{}")
        except Exception as e:
            return {"probe_err": repr(e)}


def main():
    if len(sys.argv) < 2:
        print("usage: native_record_direct.py <cred_json> <name> <secs>", flush=True)
        return 2
    cred = json.loads(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else "native"
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    rec = DirectRecorder(cred, name)
    try:
        rec.start()
    except Exception as e:
        log("start failed:", repr(e))
        os._exit(3)
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(5)
        log("t=%2ds vf=%d kf=%d audio=%dKB %dx%d"
            % (int(time.time() - t0), rec.video_frames, rec.keyframes,
               rec.audio_bytes // 1024, rec.width, rec.height))
    res = rec.stop()
    print("=== RESULT ===", json.dumps({"name": name, **res}, ensure_ascii=False), flush=True)
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
