# -*- coding: utf-8 -*-
"""密语 (plat_id=474, channel=miyu) 抓流模块。

room/detail 返回 encrypt_type=agora，encrypt_ext 直接给出：
  {playType: "agora", channelId, token, uid, ...}
其中 token 为 006 前缀 Agora 标准 token（appid 内嵌在 token 前段）。
appid = token[:32]（Agora 006/007 格式均内嵌 appid）。
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import client as CL

STREAM_METHOD = "agora_direct (encrypt_ext 直给 channel/token)"


def get_credentials(room_id, plat_id, slot=0):
    CL.ensure_token()
    try:
        rd = CL.room_detail_raw(room_id, plat_id)
    except Exception as e:
        return {"ok": False, "msg": "room/detail 失败:%s" % e}
    if rd.get("code") not in (200, 20000):
        return {"ok": False, "msg": (rd or {}).get("msg") or ("room/detail code=%s" % (rd or {}).get("code"))}
    d = rd.get("data") or {}
    ext = d.get("encrypt_ext") or {}
    if not ext or ext.get("playType") != "agora":
        return {"ok": False, "msg": "encrypt_ext 非 agora 直给(可能离线/VIP)", "vip_wall": True}
    token = ext.get("token") or ""
    channel = ext.get("channelId") or d.get("current_room_id") or ""
    uid = ext.get("uid")
    if not token or not channel:
        return {"ok": False, "msg": "encrypt_ext 缺 token/channelId"}
    # Agora 006/007 token 内嵌 appid: "006" + 32位hex appid + 载荷
    appid = token[3:35] if token.startswith(("006", "007")) else \
        (ext.get("app_id") or ext.get("appid") or "")
    return {
        "ok": True,
        "room_id": str(room_id),
        "name": d.get("name"),
        "appid": appid,
        "channel": str(channel),
        "uid": str(uid if uid is not None else 0),
        "token": token,
        "play_url": "",
        "cover": d.get("cover") or d.get("bg_img"),
    }