import sys, os, time, threading
sys.path.insert(0, '.')
import stream

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "453539121"
RUN_SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 15

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RTCConnection
from agora.rtc.rtc_connection import RTCConnConfig
from agora.rtc.agora_base import AudioScenarioType, ChannelProfileType, ClientRoleType, VideoSubscriptionOptions
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc._video_encoded_frame_observer import IVideoEncodedFrameObserver
from agora.rtc._video_frame_observer import IVideoFrameObserver
from agora.rtc.audio_frame_observer import IAudioFrameObserver

cred = stream.get_token(CHANNEL)
print("TOKEN ok appid=%s uid=%s chan=%s len=%d" % (cred["appid"], cred["uid"], cred["channel"], len(cred["token"])), flush=True)

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
print("service.initialize ret=", svc.initialize(cfg), flush=True)

stats = {"audio":0, "enc_video":0, "raw_video":0, "users":[], "connected":False, "err":"", "subscribed":[]}
conn_holder = {}

def do_subscribe(uid):
    """在用户 join 后订阅该远端用户的音视频"""
    lu = conn_holder["lu"]
    try:
        r = lu.subscribe_video(uid, VideoSubscriptionOptions(0, False))  # raw YUV
        stats["subscribed"].append(("video_raw", uid, r))
        print("DO subscribe_video(raw) uid=%s ret=%s" % (uid, r), flush=True)
    except Exception as e:
        print("subscribe_video err", e, flush=True)
    try:
        r2 = lu.subscribe_audio(uid) if hasattr(lu, "subscribe_audio") else "no-method"
        stats["subscribed"].append(("audio", uid, r2))
        print("DO subscribe_audio uid=%s ret=%s" % (uid, r2), flush=True)
    except Exception as e:
        print("subscribe_audio err", e, flush=True)

class ConnObs(IRTCConnectionObserver):
    def on_connected(self, conn, info, reason):
        stats["connected"]=True; print("CONNECTED", flush=True)
    def on_disconnected(self, conn, info):
        print("DISCONNECTED", flush=True)
    def on_error(self, conn, stat, err):
        stats["err"]=str(err); print("CONN_ERROR", err, flush=True)
    def on_user_joined(self, conn, uid):
        stats["users"].append(uid); print("USER_JOINED", uid, flush=True)
        do_subscribe(uid)

class LocalObs(IRTCLocalUserObserver):
    def on_user_video_track_subscribed(self, lu, uid, info, track):
        print("USER_VIDEO_TRACK_SUBSCRIBED uid=%s" % uid, flush=True)
    def on_first_remote_video_frame(self, lu, uid, w, h, elapsed):
        print("FIRST_REMOTE_VIDEO_FRAME uid=%s %dx%d" % (uid, w, h), flush=True)
    def on_video_subscribe_state_changed(self, lu, ch, uid, old, new, el):
        print("VIDEO_SUB_STATE uid=%s %s->%s" % (uid, old, new), flush=True)
    def on_user_state_changed(self, lu, uid, state):
        print("USER_STATE uid=%s state=%s" % (uid, state), flush=True)

class EncObs(IVideoEncodedFrameObserver):
    def on_encoded_video_frame(self, obs, uid, buf, length, info):
        stats["enc_video"]+=1
        if stats["enc_video"]<=3 or stats["enc_video"]%30==0:
            print("ENC_VIDEO uid=%s len=%s codec=%s %dx%d" % (uid, length, info.codec_type, info.width, info.height), flush=True)
        return 1
class RawObs(IVideoFrameObserver):
    def on_frame(self, lu, ch, uid, frame):
        stats["raw_video"]+=1
        if stats["raw_video"]<=3 or stats["raw_video"]%30==0:
            print("RAW_VIDEO uid=%s %dx%d" % (uid, frame.width, frame.height), flush=True)
        return 1
class AudObs(IAudioFrameObserver):
    def on_playback_audio_frame_before_mixing(self, lu, ch, uid, frame):
        stats["audio"]+=1
        return 1
    def on_get_playback_audio_frame_param(self, lu, ch):
        return None

conn_cfg = RTCConnConfig()
conn_cfg.auto_subscribe_audio = 1
conn_cfg.auto_subscribe_video = 1
conn_cfg.client_role_type = ClientRoleType.CLIENT_ROLE_AUDIENCE
conn_cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
conn_cfg.video_recv_media_packet = 0

conn = svc.create_rtc_connection(conn_cfg)
lu = conn.get_local_user()
conn_holder["lu"] = lu
conn.register_observer(ConnObs())
lu.register_local_user_observer(LocalObs())
lu.register_video_encoded_frame_observer(EncObs())
lu.register_video_frame_observer(RawObs())
lu.register_audio_frame_observer(AudObs())
print("observers registered", flush=True)

ret = conn.connect(cred["token"], str(cred["channel"]), str(cred["uid"]))
print("connect ret=", ret, flush=True)

t0=time.time()
while time.time()-t0 < RUN_SEC:
    time.sleep(0.5)

print("=== RESULT chan=%s connected=%s users=%s subscribed=%s audio=%d enc_video=%d raw_video=%d err=%s ===" % (
    CHANNEL, stats["connected"], stats["users"], stats["subscribed"], stats["audio"], stats["enc_video"], stats["raw_video"], stats["err"]), flush=True)
try: conn.disconnect()
except Exception: pass
print("DONE_NO_CRASH", flush=True)
os._exit(0)
