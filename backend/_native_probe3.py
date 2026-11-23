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

import agora.rtc._video_encoded_frame_observer as veis

# 找 encoded image receiver 接口类
try:
    from agora.rtc._video_encoded_frame_observer import IVideoEncodedFrameObserver as EncRecv
except Exception:
    EncRecv = None
print("EncRecv=", EncRecv, flush=True)
print("veis members:", [x for x in dir(veis) if not x.startswith('__')], flush=True)

cred = stream.get_token(CHANNEL)
print("TOKEN ok appid=%s uid=%s chan=%s len=%d" % (cred["appid"], cred["uid"], cred["channel"], len(cred["token"])), flush=True)

svc = AgoraService(); cfg = AgoraServiceConfig()
cfg.appid = cred["appid"]; cfg.area_code = 0xFFFFFFFF
cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
cfg.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
cfg.enable_audio_processor = False; cfg.enable_audio_device = False
cfg.enable_video = True; cfg.log_path = "/tmp/baoyu_native312.log"
print("init ret=", svc.initialize(cfg), flush=True)

stats = {"audio":0, "enc":0, "raw":0, "users":[], "connected":False, "tracks":0, "err":""}
sub_q = queue.Queue()
conn_holder = {}

class ConnObs(IRTCConnectionObserver):
    def on_connected(self, conn, info, reason):
        stats["connected"]=True; print("CONNECTED", flush=True)
    def on_error(self, conn, stat, err):
        stats["err"]=str(err); print("CONN_ERROR", err, flush=True)
    def on_user_joined(self, conn, uid):
        stats["users"].append(uid); print("USER_JOINED", uid, flush=True)
        sub_q.put(uid)   # 不在此回调里调SDK, 交给主线程处理

# encoded receiver: 拿 H264 帧
class EncRecvImpl:
    def on_encoded_video_frame(self, *a):
        stats["enc"]+=1
        if stats["enc"]<=3 or stats["enc"]%30==0:
            print("ENC_FRAME args=%d" % len(a), [str(x)[:40] for x in a], flush=True)
        return 1

class LocalObs(IRTCLocalUserObserver):
    def on_user_video_track_subscribed(self, lu, uid, info, track):
        stats["tracks"]+=1
        print("TRACK_SUBSCRIBED uid=%s type=%s encoded_only=%s" % (uid, getattr(track,'__class__',None), getattr(info,'encoded_frame_only',None)), flush=True)
        # 在该 track 上直接挂 encoded receiver
        try:
            recv = EncRecvImpl()
            r = track.register_video_encoded_image_receiver(recv)
            print("  track.register_video_encoded_image_receiver ret=", r, flush=True)
        except Exception as e:
            print("  register receiver ERR:", e, flush=True)
    def on_first_remote_video_frame(self, lu, uid, w, h, el):
        print("FIRST_REMOTE_VIDEO_FRAME uid=%s %dx%d" % (uid, w, h), flush=True)

conn_cfg = RTCConnConfig()
conn_cfg.auto_subscribe_audio = 0
conn_cfg.auto_subscribe_video = 0     # 手动订阅
conn_cfg.client_role_type = ClientRoleType.CLIENT_ROLE_BROADCASTER  # 官方服务端用 broadcaster
conn_cfg.channel_profile = ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
conn_cfg.video_recv_media_packet = 0

conn = svc.create_rtc_connection(conn_cfg)
lu = conn.get_local_user(); conn_holder["lu"]=lu
conn.register_observer(ConnObs())
lu.register_local_user_observer(LocalObs())
print("connect ret=", conn.connect(cred["token"], str(cred["channel"]), str(cred["uid"])), flush=True)

# 主线程: 收到 join 后订阅 encoded
def do_subs():
    while True:
        try: uid = sub_q.get(timeout=0.2)
        except queue.Empty: continue
        try:
            r = lu.subscribe_video(uid, VideoSubscriptionOptions(0, True))  # encodedOnly=True
            print("subscribe_video(enc) uid=%s ret=%s" % (uid, r), flush=True)
            lu.subscribe_audio(uid)
        except Exception as e:
            print("subscribe ERR", e, flush=True)

threading.Thread(target=do_subs, daemon=True).start()

t0=time.time()
while time.time()-t0 < RUN_SEC: time.sleep(0.5)

print("=== RESULT chan=%s conn=%s users=%s tracks=%d audio=%d enc=%d raw=%d err=%s ===" % (
    CHANNEL, stats["connected"], stats["users"], stats["tracks"], stats["audio"], stats["enc"], stats["raw"], stats["err"]), flush=True)
try: conn.disconnect()
except Exception: pass
print("DONE_NO_CRASH", flush=True)
os._exit(0)
