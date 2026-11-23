# -*- coding: utf-8 -*-
"""半糖直播 (plat_id=449, channel=hxzb) 抓流模块。

流程：room/detail -> real_room_id -> 纯 Python 9875 签 Agora 007e。
与旧 gateway.py 逻辑等价，只服务半糖一个平台。
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import config as C
import client as CL

STREAM_METHOD = "agora_007e (real_room_id -> 9875 签名)"

# real_room_id 缓存: 避免同房反复走外网 detail 慢拉
_CRED_CACHE = {}          # key=(room_id,plat_id) -> {ts, data}
_CRED_TTL = int(os.environ.get("CRED_TTL", "45"))   # 缓存秒


def get_credentials(room_id, plat_id, slot=0):
    """room/detail 拿 real_room_id -> stream.get_token 生成 007e。"""
    key = (str(room_id), str(plat_id))
    hit = _CRED_CACHE.get(key)
    if hit and (time.time() - hit["ts"] < _CRED_TTL):
        return hit["data"]
    if CL.switching():
        return {"ok": False, "switching": True, "msg": "正在换号中"}
    CL.ensure_token()
    # 会员(账号)身份: 无次数限制, 永不自动换号
    is_member = CL.is_account_active()
    try:
        rd = CL.room_detail_raw(room_id, plat_id)
    except Exception as e:
        return {"ok": False, "msg": "room/detail 失败:%s" % e}
    msg = (rd.get("msg") or "")
    if (not is_member) and rd.get("code") not in (200, 20000) and CL._QUOTA_HINT in msg:
        CL.switch_guest_async()
        return {"ok": False, "switching": True, "msg": "正在换号中"}
    if rd.get("code") not in (200, 20000):
        return {"ok": False, "msg": (rd or {}).get("msg") or ("room/detail code=%s" % (rd or {}).get("code"))}
    d = rd.get("data") or {}
    real = d.get("real_room_id") or d.get("current_room_id")
    if not real:
        return {"ok": False, "msg": "该房无 real_room_id(可能付费/VIP房)", "vip_wall": True}
    try:
        import stream as ST
        cred = ST.get_token(real)
    except Exception as e:
        return {"ok": False, "msg": "生成 token 失败:%s" % e}
    out = {"ok": True, **cred, "room_id": room_id,
           "name": d.get("name"), "real_room_id": real,
           "cover": d.get("cover") or d.get("bg_img")}
    _CRED_CACHE[key] = {"ts": time.time(), "data": out}
    return out