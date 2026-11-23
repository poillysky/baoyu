# -*- coding: utf-8 -*-
"""番茄社区 (plat_id=440, channel=fanqie) 抓流模块。

room/detail 返回：encrypt_type=inter_encrypt，直出 play_url（HLS m3u8）。
m3u8 带 AES-128 KEY URI（#EXT-X-KEY），由播放器请求 key 解密。
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import client as CL

STREAM_METHOD = "inter_encrypt_hls (play_url 直出)"


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
        # 直出 HLS m3u8; 播放/录制都走后端 /api/stream 代理, 由代理把 key.key 解密
        # 成明文 AES-128 key 并显式 IV=0x0, hls.js/ffmpeg 才能解开 ts 段。
        "play_url": url,
        "encrypt_type": d.get("encrypt_type") or "",
        "current_room_id": d.get("current_room_id") or "",
        "channel": d.get("current_room_id") or str(room_id),
        "uid": "0",
        "token": "",
        "appid": "",
    }