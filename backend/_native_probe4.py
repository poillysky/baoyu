import sys, os, time, threading, queue
sys.path.insert(0, '.')
import stream

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "453539121"
RUN_SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 20

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RTCConnection
from agora.rtc.rtc_connection import RTCConnConfig
from agora.rtc.agora_base import AudioScenarioType, ChannelProfileType, ClientRoleType, VideoSubscriptionOptions
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc._video_encoded_frame_observer import VideoEncodedFrameObserverInner, IVideoEncodedFrameObserver
from agora.rtc.local_user import agora_video_encoded_frame_observer_create
from agora.rtc._video_frame_observer import IVideoFrameObserver, VideoFrameObserverInner

cred = stream.get_token(CHANNEL)
print("TOKEN ok appid=%s uid=%s chan=%s len=%d" % (cred["appid"], cred["uid"], cred["channel"], len(cred["token"])), flush=True)

svc = AgoraService(); cfg = AgoraServiceConfig()
cfg.appid = cred["appid"]; cfg.area_code = 0xFFFFFFFF
cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
cfg.enable_audio_processor = False; cfg.enable_audio_device = False
cfg.enable_video = True; cfg.log_path = "/tmp/baoyu_native312.log"
print("init ret=", svc.initialize(cfg), flush=True)

stats = {"enc":0, "raw":0, "tracks":0, "frames_sample":0, "err":""}
sub_q = queue.Queue()
KEEP = []  # 保持 inner/handle 引用防GC
TRACKS = []

class EncImpl(IVideoEncodedFrameObserver):
    def on_encoded_video_frame(self, obs, uid, buf, length, info):
        stats["enc"]+=1
        if stats["enc"]<=3 or stats["enc"]%30==0:
            print("ENC uid=%s len=%s codec=%s %dx%d" % (uid, length, info.codec_type, info.width, info.height), flush=True)
        return 1
class RawImpl(IVideoFrameObserver):
    def on_frame(self, lu, ch, uid, frame):
        stats["raw"]+=1
        if stats["raw"]<=3:
            print("RAW uid=%s %dx%d" % (uid, frame.width, frame.height), flush=True)
        return 1

# 预先造 inner + handle (join前注册, 官方要求)
enc_impl = EncImpl()
enc_inner = VideoEncodedFrameObserverInner(enc_impl)
enc_handle = agora_video_encoded_frame_observer_create(enc_inner)
KEEP += [enc_impl, enc_inner]
print("enc_handle=", enc_handle, flush=True)

class ConnObs(IRTCConnectionObserver):
    def on_connected(self, conn, info, reason): print("CONNECTED", flush=True)
    def on_error(self, conn, stat, err):
        stats["err"]=str(err); print("ERR", err, flush=True)
    def on_user_joined(self, conn, uid):
        print("USER_JOINED", uid, flush=True); sub_q.put(uid)

class LocalObs(IRTCLocalUserObserver):
    def on_user_video_track_subscribed(self, lu, uid, info, track):
        stats["tracks"]+=1
        enc_only = getattr(info, "encoded_frame_only", None)
        try:
            ti = track.get_track_info()
            if ti: print("TRACK_SUB uid=%s enc_only=%s stream_type=%s codec=%s encoded_frame_only=%s source=%s pos=%s" % (uid, enc_only, ti.stream_type, ti.codec_type, ti.encoded_frame_only, ti.source_type, ti.observation_position), flush=True)
            else: print("TRACK_SUB uid=%s enc_only=%s (no info)" % (uid, enc_only), flush=True)
        except Exception as e:
            print("TRACK_SUB uid=%s enc_only=%s info_err=%s" % (uid, enc_only, e), flush=True)
        try:
            r = track.register_video_encoded_image_receiver(enc_handle)
            print("  track.register_encoded_receiver ret=", r, flush=True)
            TRACKS.append(track)
        except Exception as e:
            print("  track register ERR:", e, flush=True)
    def on_first_remote_video_frame(self, lu, uid, w, h, el):
        print("FIRST_FRAME uid=%s %dx%d" % (uid, w, h), flush=True)

# audience + auto_subscribe(1) 才收流; 观察器 join 前注册
conn_cfg = RTCConnConfig()
conn_cfg.auto_subscribe_audio = 1
conn_cfg.auto_subscribe_video = 1
conn_cfg.client_role_type = ClientRoleType.CLIENT_ROLE_AUDIENCE
conn_cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
conn_cfg.video_recv_media_packet = 0
conn = svc.create_rtc_connection(conn_cfg)
lu = conn.get_local_user()
conn.register_observer(ConnObs())
lu.register_local_user_observer(LocalObs())
# 也注册全局 encoded 观察器(双保险)
try:
    print("global register_encoded_frames ret=", lu.register_video_encoded_frame_observer(enc_impl), flush=True)
except Exception as e:
    print("global reg enc err", e, flush=True)
# 对齐 App 原生设置: 关硬件解码器(第App用的是 enable_hw_decoder:false)
try:
    ap = conn.get_agora_parameter()
    if ap:
        r1 = ap.set_parameters('{"engine.video.enable_hw_decoder":false}')
        r2 = ap.set_bool("che.video.enable_hw_decoder", False)
        print("set_parameters hw_decode_off ret=", r1, r2, flush=True)
except Exception as e:
    print("set_parameters ERR", e, flush=True)
print("connect ret=", conn.connect(cred["token"], str(cred["channel"]), str(cred["uid"])), flush=True)

def do_subs():
    while True:
        try: uid = sub_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            r = lu.subscribe_video(uid, VideoSubscriptionOptions(0, True))
            print("subscribe_video(enc) uid=%s ret=%s" % (uid, r), flush=True)
            lu.subscribe_audio(uid)
            # 关键: 请求远端发关键帧(IDR), 否则解码器不启动
            ir = lu.send_intra_request(uid)
            print("send_intra_request uid=%s ret=%s" % (uid, ir), flush=True)
            # 周期性再请求(防关键帧丢)
            for _ in range(6):
                time.sleep(2)
                try: lu.send_intra_request(uid)
                except Exception: pass
        except Exception as e:
            print("subscribe ERR", e, flush=True)
threading.Thread(target=do_subs, daemon=True).start()

t0=time.time()
while time.time()-t0 < RUN_SEC: time.sleep(0.5)
print("=== RESULT chan=%s tracks=%d enc=%d raw=%d err=%s ===" % (CHANNEL, stats["tracks"], stats["enc"], stats["raw"], stats["err"]), flush=True)
print("=== RESULT chan=%s tracks=%d enc=%d raw=%d err=%s ===" % (CHANNEL, stats["tracks"], stats["enc"], stats["raw"], stats["err"]), flush=True)
try: conn.disconnect()
except Exception: pass
print("DONE_NO_CRASH", flush=True); os._exit(0)
