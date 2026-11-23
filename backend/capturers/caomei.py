# -*- coding: utf-8 -*-
"""草莓直播 (plat_id=460, channel=caomeizb) 抓流模块。

room/detail 直出 play_url（FLV + wsSecret），与杏仁/青瑶同型。
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import client as CL

STREAM_METHOD = "flv_wssecret (play_url 直出)"


def get_credentials(room_id, plat_id, slot=0):
    CL.ensure_token()
    try:
        rd = CL.room_detail_raw(room_id, plat_id)
    except Exception as e:
        return {"ok": False, "msg": "room/detail 失败:%s" % e}
    if rd.get("code") not in (200, 20000):
        return {"ok": False, "msg": (rd or {}).get("msg") or ("room/detail code=%s" % (rd or {}).get("code"))}
    d = rd.get("data") or {}
    url = d.get("play_url") or ""
    if not url:
        return {"ok": False, "msg": "play_url 为空(可能离线/加密房)", "vip_wall": True}
    return {
        "ok": True,
        "mode": "direct",
        "room_id": str(room_id),
        "name": d.get("name"),
        "play_url": url,
        "encrypt_type": d.get("encrypt_type") or "",
        "current_room_id": d.get("current_room_id") or "",
        "channel": d.get("current_room_id") or str(room_id),
        "uid": "0",
        "token": "",
        "appid": "",
    }