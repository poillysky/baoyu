# -*- coding: utf-8 -*-
"""小妲己 (plat_id=383, channel=XNM) 抓流模块。

真实拉流链路 (逆向 xnmHttp.dart GetPlayUrl 确认):
1) room/detail.encrypt_ext = {butter, reqUrl, token(JWT)}
2) GET reqUrl (getPrivateLimit), Authorization: Bearer <token>, UA okhttp/4.8.0
   + X-Live-Butter2/pretty/puzzle 头
3) 响应 = base64(AES-256-CBC 密文), key = md5(token.trim()) 前16字节, iv = 后16
4) 解出 JSON: data.stream.flv_pull_url / pull_url(rtmp) / lll_pull_url(webrtc)

播放/录制统一走 flv_pull_url 直链 (mode=direct, ffmpeg/播放器直连)。
"""
import os, sys, json, hashlib, base64

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import client as CL

import requests

STREAM_METHOD = "xnm_private_limit (getPrivateLimit -> FLV 直链)"

_PRIV_HEADERS = {
    "Authorization": "",          # 运行时填 Bearer token
    "User-Agent": "okhttp/4.8.0",
    "X-Live-Butter2": "knockknock",
    "X-Live-Pretty": "spring",
    "x-accept-puzzle": "cola,tiger,tiger2,panda",
}


def _decrypt_private_limit(req_url, token):
    tok = (token or "").strip()
    r = requests.get(req_url, headers=dict(_PRIV_HEADERS, Authorization="Bearer " + tok),
                     timeout=15)
    if r.status_code != 200:
        raise RuntimeError("getPrivateLimit HTTP %s" % r.status_code)
    raw = base64.b64decode(r.text.strip())
    md5t = hashlib.md5(tok.encode()).hexdigest()
    key = md5t[:16].encode()
    iv = md5t[16:].encode()
    from Crypto.Cipher import AES
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw)
    p = dec[-1]
    if 1 <= p <= 16:
        dec = dec[:-p]
    return json.loads(dec.decode("utf-8"))


def _pick_stream_url(data):
    st = (data.get("stream") or {}) if isinstance(data, dict) else {}
    # FLV 优先 (ffmpeg/网页都好处理), 其次 rtmp, 最后 webrtc
    url = st.get("flv_pull_url") or ""
    if isinstance(url, dict):  # 有些端点返回多清晰度 dict
        url = url.get("high") or url.get("middle") or (list(url.values()) or [""])[0]
    if url:
        return url
    return st.get("pull_url") or ""


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
    req_url = ext.get("reqUrl") or ""
    tok = ext.get("token") or ""
    if req_url and tok:
        try:
            j = _decrypt_private_limit(req_url, tok)
            if j.get("code") != 0:
                return {"ok": False, "msg": "getPrivateLimit code=%s" % j.get("code")}
            url = _pick_stream_url(j.get("data") or {})
            if not url:
                return {"ok": False, "msg": "getPrivateLimit 无流地址", "vip_wall": True}
            return {
                "ok": True,
                "mode": "direct",
                "room_id": str(room_id),
                "name": d.get("name"),
                "play_url": url,
                "encrypt_type": "",
                "current_room_id": d.get("current_room_id") or "",
                "channel": d.get("current_room_id") or str(room_id),
                "uid": "0",
                "token": "",
                "appid": "",
            }
        except Exception as e:
            # 拿不到直链则回退原 HLS 通道
            pass
    # 回退: play_url 直出 (HLS AES-128, 浏览器端 hls.js 无法解 key, 仅作兜底)
    url = d.get("play_url") or ""
    if not url:
        return {"ok": False, "msg": "play_url 为空(可能离线/加密房)", "vip_wall": True}
    return {
        "ok": True,
        "mode": "direct" if url.lower().endswith(".flv") else "web",
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
