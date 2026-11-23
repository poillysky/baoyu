#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_orchestrator.py — 密语原生优先 + Web 兜底 录制闭环。

对单个房间:
  1) 是标准 Agora 平台(密语/474) 且 agora 服务启用且健康
       → 调原生录制服务开录(duration 秒)
       → 轮询 AGORA_REC_DIR 出现新 mp4 且稳定(大小不再增长)
       → 搬到 BAOYU 暂存(REC_DIR)/平台/主播_room_id/ 命名
       → 再搬到 FINAL_REC_DIR 归档(机械盘)
       → 成功返回
  2) 任一步失败(服务不通/开录失败/超时没文件/文件损坏)
       → 回退 web 录制(REC.api_start), 走原流程
       → 录制完成后同样归档到 FINAL_REC_DIR

返回 dict: {ok, method: native|web, path, ...}
"""
import os
import re
import time
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config as C
import agora_client as AC
import settings_store as SS

# 支持原生 Agora 直录的平台(标准 Agora 006/007 token)。半糖449/密语474等标准房可原生;
# 半糖(449)虽 appid 相同但服务端不给视频轨道, 见 NATIVE_RECORDING_RESEARCH 结论 → 走 web。
NATIVE_PLAT_IDS = {"474"}   # 密语: 标准 Agora, 原生可靠。半糖(449)列入但录不到视频, 故不放。


def _plat_name(plat_id):
    try:
        from capturers import get_plat_name
        return get_plat_name(plat_id) or ("plat%s" % plat_id)
    except Exception:
        return {"474": "密语", "449": "半糖"}.get(str(plat_id), "plat%s" % plat_id)


def _sanitize(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s or "")).strip("_")


def _wait_new_file(src_dir, before, timeout, settle=3.0):
    """等 src_dir 出现新 mp4 且大小稳定(不再增长 settle 秒)。返回新文件路径或 None。"""
    t0 = time.time()
    candidate = None
    last_size = -1
    stable_since = 0
    while time.time() - t0 < timeout:
        try:
            names = [f for f in os.listdir(src_dir) if f.lower().endswith(".mp4")]
        except FileNotFoundError:
            names = []
        new = [f for f in names if f not in before]
        if new:
            f = os.path.join(src_dir, sorted(new)[-1])
            sz = os.path.getsize(f)
            if sz != last_size:
                last_size = sz
                stable_since = time.time()
                candidate = f
            elif candidate and (time.time() - stable_since) >= settle and sz > 0:
                return candidate
        time.sleep(1.0)
    # 超时: 若有候选且 >0 也返回(尽力)
    if candidate and os.path.getsize(candidate) > 0:
        return candidate
    return None


def _place(src_file, plat_name, anchor, name_tag):
    """把文件放到 BAOYU 暂存(REC_DIR)按项目规则, 再复制归档到 FINAL_REC_DIR。返回 (mid_path, final_path)。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = "%s_%s.mp4" % (name_tag, stamp)
    mid_dir = os.path.join(C.REC_DIR, _sanitize(plat_name), anchor)
    os.makedirs(mid_dir, exist_ok=True)
    mid_path = os.path.join(mid_dir, fname)
    shutil.move(src_file, mid_path)   # 从固态 agora 目录移到暂存(同为固态, 快)
    # 归档到机械盘(一次写入)
    final_dir = os.path.join(C.FINAL_REC_DIR, _sanitize(plat_name), anchor)
    os.makedirs(final_dir, exist_ok=True)
    final_path = os.path.join(final_dir, fname)
    try:
        shutil.copy2(mid_path, final_path)
    except Exception as e:
        print("[ARCHIVE] 归档失败(保留暂存):", e)
        final_path = None
    return mid_path, final_path


def record_native(room_id, plat_id, cred, duration, name=None):
    """原生录制单房。返回 {ok, method:'native', mid_path, final_path} 或 {ok:False, msg}。"""
    if not AC.enabled():
        return {"ok": False, "msg": "原生录制未启用"}
    if not AC.health():
        return {"ok": False, "msg": "agora 服务健康检查失败"}
    appid, channel, token, uid = cred.get("appid"), cred.get("channel"), cred.get("token"), cred.get("uid")
    if not (appid and channel and token):
        return {"ok": False, "msg": "凭据缺 appid/channel/token"}
    src_dir = C.AGORA_REC_DIR
    os.makedirs(src_dir, exist_ok=True)
    before = set(f for f in os.listdir(src_dir) if f.lower().endswith(".mp4"))
    print("[NATIVE] 开录 room=%s channel=%s uid=%s duration=%ss" % (room_id, channel, uid, duration))
    st = AC.start_by_room(appid, channel, token, uid, duration)
    if not st.get("ok"):
        return {"ok": False, "msg": "原生开录失败: %s" % st.get("msg")}
    # 等文件 + 稳定(录制时长 + 15s 缓冲)
    f = _wait_new_file(src_dir, before, timeout=duration + 15, settle=3.0)
    if not f:
        return {"ok": False, "msg": "原生录制超时未产出文件"}
    # 简单有效性检查(非0字节)
    if os.path.getsize(f) < 1024:
        return {"ok": False, "msg": "原生文件异常(过小 %d)" % os.path.getsize(f)}
    name_tag = _sanitize(name or cred.get("name") or ("ROOM_%s" % room_id))
    anchor = "%s_%s" % (name_tag, room_id)
    mid_path, final_path = _place(f, _plat_name(plat_id), anchor, name_tag)
    print("[NATIVE] 完成 -> 暂存=%s 归档=%s" % (mid_path, final_path))
    return {"ok": True, "method": "native", "mid_path": mid_path, "final_path": final_path}


def record_room(room_id, plat_id, duration, name=None, cred=None, force_web=False,
                web_start=None, web_wait=None):
    """统一入口: 原生优先, 失败 Web 兜底。

    web_start(cred)->rec_id, web_wait(rec_id)->完成 由调用方(如 watchman)注入;
    独立测试时可传 None(仅测原生)。
    """
    room_id = str(room_id)
    if cred is None:
        import gateway
        cred = gateway.get_stream_credentials(room_id, int(plat_id), 0)
    if not cred.get("ok"):
        return {"ok": False, "method": None, "msg": "凭据失败:%s" % cred.get("msg")}

    # 1) 原生优先
    if (not force_web) and str(plat_id) in NATIVE_PLAT_IDS:
        r = record_native(room_id, plat_id, cred, duration, name=name)
        if r.get("ok"):
            return r
        print("[FALLBACK] 原生失败(%s), 回退 Web" % r.get("msg"))

    # 2) Web 兜底(由调用方注入具体实现)
    if web_start is None:
        return {"ok": False, "method": "web", "msg": "原生失败且无 web 兜底实现注入"}
    rec_id = web_start(cred)
    if not rec_id:
        return {"ok": False, "method": "web", "msg": "web 录制启动失败"}
    if web_wait is not None:
        web_wait(rec_id)
    return {"ok": True, "method": "web", "rec_id": rec_id}
