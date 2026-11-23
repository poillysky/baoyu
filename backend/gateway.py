#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉流凭据统一入口（薄分发层）。

真正的抓流逻辑已按平台拆分到 capturers/ 目录：
  capturers/half_sugar.py  平台449 半糖直播  Agora 007e 签名 (9875)
  capturers/xiao_daji.py   平台383 小妲己    xnm HLS (play_url)
  capturers/fanqie.py      平台440 番茄社区   inter_encrypt HLS (play_url)
  capturers/xingren.py     平台455/472 杏仁/青瑶 wsSecret FLV (play_url)
  capturers/caomei.py      平台460 草莓直播  wsSecret FLV (play_url)
  capturers/miyu.py        平台474 密语      agora 直给 token (encrypt_ext)
  capturers/hot51.py       平台473 Hot51    待调研 (detail code=1000)
  capturers/swag.py        平台475 swag     待调研 (detail code=1000)

本文件只做两件事：
  1. 按 plat_id 分发到 capturers
  2. 保留旧 gateway_status() 兼容（server /api/gateway/status）
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from capturers import get_credentials, registry_info

# ---- 兼容：CapturersRegistry 元信息 ----
def gateway_status():
    identity = os.path.join(HERE, "kiwi_identity.json")
    return {"ok": os.path.exists(identity), "mode": "capturers", "server": "162.14.199.148:9875"}


def get_stream_credentials(room_id, plat_id, slot=0):
    """统一入口：同 capturers.get_credentials。"""
    return get_credentials(room_id, plat_id, slot)


if __name__ == "__main__":
    import json
    print(json.dumps(registry_info(), ensure_ascii=False, indent=1))