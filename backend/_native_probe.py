import sys, os, time, ctypes, queue, threading
sys.path.insert(0, '.')
import stream

# 配置
CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "91199373"
VIDEO_MEDIA_PACKET = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0=走observer, 1=media packet
RUN_SEC = int(sys.argv[3]) if len(sys.argv) > 3 else 12

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RTCConnection
from agora.rtc.rtc_connection import RTCConnConfig
from agora.rtc.agora_base import AudioScenarioType, ChannelProfileType, ClientRoleType, VideoSubscriptionOptions
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc._video_encoded_frame_observer import IVideoEncodedFrameObserver
from agora.rtc._video_frame_observer import IVideoFrameObserver

cred = stream.get_token(CHANNEL)
print("TOKEN ok appid=%s uid=%s len=%d" % (cred["appid"], cred["uid"], len(cred["token"])), flush=True)

svc = AgoraService()
cfg = AgoraServiceConfig()
cfg.appid = cred["appid"]
cfg.area_code = 0xFFFFFFFF
cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
cfg.enable_audio_processor = False
cfg.enable_audio_device = False
cfg.enable_video = True
cfg.log_path = "/tmp/baoyu_native312.log"
ret = svc.initialize(cfg)
print("service.initialize ret=", ret, flush=True)

stats = {"audio":0, "enc_video":0, "raw_video":0, "users":[], "connected":False, "err":""}

class ConnObs(IRTCConnectionObserver):
    def on_connected(self, conn, info, reason):
        stats["connected"]=True
        print("CONNECTED", flush=True)
    def on_disconnected(self, conn, info):
        print("DISCONNECTED", flush=True)
    def on_error(self, conn, stat, err):
        stats["err"]=str(err); print("ERROR", err, flush=True)
    def on_user_joined(self, conn, uid):
        stats["users"].append(uid); print("USER_JOINED", uid, flush=True)

class LocalObs(IRTCLocalUserObserver):
    def on_first_remote_video_frame(self, lu, uid, info):
        print("FIRST_REMOTE_VIDEO_FRAME uid=", uid, flush=True)

class EncObs(IVideoEncodedFrameObserver):
    def on_encoded_video_frame(self, obs, uid, buf, length, info):
        stats["enc_video"]+=1
        if stats["enc_video"]<=3 or stats["enc_video"]%30==0:
            print("ENC_VIDEO uid=%s len=%s codec=%s w=%s h=%s type=%s" % (uid, length, info.codec_type, info.width, info.height, info.frame_type), flush=True)
        return 1

class RawObs(IVideoFrameObserver):
    def on_frame(self, lu, ch, uid, frame):
        stats["raw_video"]+=1
        if stats["raw_video"]<=3 or stats["raw_video"]%30==0:
            print("RAW_VIDEO uid=%s %dx%d y_stride=%s" % (uid, frame.width, frame.height, frame.y_stride), flush=True)
        return 1

conn_cfg = RTCConnConfig()
conn_cfg.auto_subscribe_audio = 1
conn_cfg.auto_subscribe_video = 1
conn_cfg.client_role_type = ClientRoleType.CLIENT_ROLE_AUDIENCE
conn_cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
conn_cfg.video_recv_media_packet = VIDEO_MEDIA_PACKET

conn = svc.create_rtc_connection(conn_cfg)
print("create_rtc_connection ok", flush=True)
lu = conn.get_local_user()

conn.register_observer(ConnObs())
lu.register_local_user_observer(LocalObs())

enc = EncObs(); raw = RawObs()

# 两种都注册，看哪个来帧
r_enc = lu.register_video_encoded_frame_observer(enc)
r_sub_enc = lu.subscribe_all_video(VideoSubscriptionOptions(0, True))
print("register_encoded ret=", r_enc, "subscribe_all(encoded) ret=", r_sub_enc, flush=True)

r_raw = lu.register_video_frame_observer(raw)
r_sub_raw = lu.subscribe_all_video(VideoSubscriptionOptions(0, False))
print("register_raw ret=", r_raw, "subscribe_all(raw) ret=", r_sub_raw, flush=True)

ret = conn.connect(cred["token"], str(cred["channel"]), str(cred["uid"]))
print("connect ret=", ret, flush=True)

t0=time.time()
while time.time()-t0 < RUN_SEC:
    time.sleep(0.5)
    if int((time.time()-t0)*2) % 4 == 0:
        pass

print("=== RESULT vmp=%d connected=%s users=%s audio=%d enc_video=%d raw_video=%d err=%s ===" % (
    VIDEO_MEDIA_PACKET, stats["connected"], stats["users"], stats["audio"], stats["enc_video"], stats["raw_video"], stats["err"]), flush=True)

try:
    conn.disconnect()
except Exception: pass
print("DONE_NO_CRASH", flush=True)
os._exit(0)
