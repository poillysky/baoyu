# -*- coding: utf-8 -*-
"""swag (plat_id=475, channel=swag) 抓流模块（待调研）。

room/detail 当前返回 code=1000「获取数据失败:code != 200」。
TODO: 逆向 App 抓包确认 swag 的 detail/凭据接口后实现。
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import client as CL

STREAM_METHOD = "TODO (detail code=1000, 待调研)"


def get_credentials(room_id, plat_id, slot=0):
    CL.ensure_token()
    try:
        rd = CL.room_detail_raw(room_id, plat_id)
    except Exception as e:
        return {"ok": False, "msg": "room/detail 失败:%s" % e}
    if rd.get("code") not in (200, 20000):
        return {"ok": False, "msg": "swag 未接入: detail code=%s (%s)" %
                (rd.get("code"), (rd or {}).get("msg") or "")}
    d = rd.get("data") or {}
    url = d.get("play_url") or ""
    if not url:
        return {"ok": False, "msg": "play_url 为空(可能离线/加密房)", "vip_wall": True}
    return {
        "ok": True,
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