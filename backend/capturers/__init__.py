# -*- coding: utf-8 -*-
"""平台抓流统一入口：按 plat_id 分发到各平台专属抓流模块。

统一接口：get_credentials(room_id, plat_id) -> dict
  {ok: bool, msg?: str, switching?: bool, vip_wall?: bool,
   appid?, channel?, uid?, token?, play_url?, encrypt_type?, ...}

每个平台一个文件（capturers/<plat>.py），互不干扰。
未接入的平台返回 ok=False + 明确提示，不会静默失败。
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from . import half_sugar, xiao_daji, fanqie, xingren, caomei, miyu
from . import hot51, swag

# plat_id -> 抓流模块。模块必须暴露 get_credentials(room_id, plat_id, slot=0)
_REGISTRY = {
    449: half_sugar,   # 半糖直播 (hxzb)   —— Agora 007e 签名
    383: xiao_daji,    # 小妲己 (XNM)      —— xnm HLS (play_url)
    440: fanqie,       # 番茄社区 (fanqie) —— inter_encrypt HLS (play_url)
    455: xingren,      # 杏仁直播          —— wsSecret FLV (play_url)
    472: xingren,      # 青瑶直播          —— 与杏仁同源, 共用抓流
    460: caomei,       # 草莓直播          —— wsSecret FLV (play_url)
    474: miyu,         # 密语              —— agora 直给 token (encrypt_ext)
    473: hot51,        # Hot51直播         —— 待调研 (detail code=1000)
    475: swag,         # swag              —— 待调研 (detail code=1000)
}


def get_credentials(room_id, plat_id, slot=0):
    """统一入口：按 plat_id 选抓流模块。未接入 → 明确失败提示。"""
    mod = _REGISTRY.get(int(plat_id))
    if mod is None:
        return {"ok": False, "msg": "平台 %s 未接入抓流模块" % plat_id}
    try:
        return mod.get_credentials(room_id, plat_id, slot)
    except Exception as e:
        return {"ok": False, "msg": "[%s] 抓流异常: %s" % (mod.__name__, e)}


# 平台展示名(前端平台列表/录制目录用)
_PLAT_NAMES = {
    449: "半糖", 383: "小妲己", 440: "番茄", 455: "杏仁", 472: "青瑶",
    460: "草莓", 474: "密语", 473: "Hot51", 475: "Swag",
}


def plat_name(pid):
    return _PLAT_NAMES.get(int(pid))


def registry_info():
    out = []
    for pid, mod in sorted(_REGISTRY.items()):
        out.append({"plat_id": pid, "module": mod.__name__,
                    "method": getattr(mod, "STREAM_METHOD", "?"),
                    "name": plat_name(pid)})
    return out