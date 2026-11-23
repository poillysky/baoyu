#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""full_test_miyu_native.py — 密语原生优先 + Web兜底 完整测试。

本机(Mac)测试, NAS 上真实跑 agora 服务。为模拟"同机目录映射", 脚本:
  - 远端 NAS: agora 服务把 mp4 落到 /vol1/1000/Docker/agora-recorder/recordings
  - 本机: 轮询 NAS 新文件 -> scp 到 本地 C.AGORA_REC_DIR
  - 再走 native_orchestrator 的 place/归档逻辑(暂存 REC_DIR + 归档 FINAL_REC_DIR)

测三条路径:
  A) 原生正常: 密语在线房 -> 原生录制 -> 拉回 -> 归档
  B) Web兜底: 强制 force_web=True -> 走 web 录制(需要真实起 Chromium, 这里验证调度分支即可)
  C) 原生失败兜底: 给一个不存在的房/错 channel -> 原生失败 -> 触发回退分支
"""
import os, sys, time, subprocess, json

HERE = "/Volumes/vol1T/project/cams/baoyu/baoyu-py/backend"
sys.path.insert(0, HERE)
os.chdir(HERE)

import config as C
import settings_store as SS
import agora_client as AC
import native_orchestrator as NO

NAS = "poilly@192.168.2.38"
PASS = "2885748zhu"
NAS_FLAT = "/vol1/1000/Docker/agora-recorder/recordings"
ENV = dict(os.environ, SSHPASS=PASS)


def nas(cmd, t=30):
    return subprocess.run(["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-p", "22", NAS,
                           f"echo '{PASS}'|sudo -S sh -c \"{cmd}\""], capture_output=True, text=True, timeout=t, env=ENV, input=PASS+"\n")


def nas_ls():
    r = nas(f"ls -1 {NAS_FLAT}")
    return set(x.strip() for x in r.stdout.splitlines() if x.strip().endswith(".mp4"))


def scp_to(src_remote, dst_local, t=120):
    return subprocess.run(["sshpass", "-e", "scp", "-o", "StrictHostKeyChecking=no", "-P", "22",
                           f"{NAS}:{src_remote}", dst_local], capture_output=True, text=True, timeout=t, env=ENV)


def probe(path):
    p = subprocess.run([C.FFMPEG.replace("ffmpeg", "ffprobe") if "ffmpeg" in C.FFMPEG else "/opt/homebrew/bin/ffprobe",
                        "-v", "error", "-show_entries", "format=duration:stream=codec_name,codec_type,width,height",
                        "-of", "default=nw=1", path], capture_output=True, text=True)
    return p.stdout.strip()


def test_A_native():
    print("\n========== 测试A: 密语原生正常路径 ==========")
    import client as CL, gateway
    CL.ensure_token(force=True)
    # 找密语在线房
    r = CL.api("/api/live/plat/detail", {"id": 474, "room_type": 0, "page": 1, "page_size": 20})
    arr = (r.get("data") or {}).get("room_list") or []
    cred = None; rid = None
    # 优先小蓉宝 6516530(稳定房), 再按列表
    prefer = ["6516530"]
    order = prefer + [str(x.get("room_id")) for x in arr if str(x.get("room_id")) not in prefer]
    for rid in order[:15]:
        cred = gateway.get_stream_credentials(rid, 474)
        if cred.get("ok"):
            break
    if not cred or not cred.get("ok"):
        print("!! 取不到密语凭据"); return False
    print("  房间:", rid, cred.get("name"), "channel=", cred.get("channel"), "uid=", cred.get("uid"))

    # 原生开录(duration=20)
    dur = 20
    before = nas_ls()
    st = AC.start_by_room(cred["appid"], cred["channel"], cred["token"], cred["uid"], dur)
    print("  原生开录:", st)
    if not st.get("ok"):
        print("!! 原生开录失败"); return False

    # 轮询 NAS 新文件(等 dur+12), 判大小稳定(连续两次采样大小不变且 > 100KB)
    print("  等录制完成...")
    t0 = time.time(); got = None; last_sz = -1; stable_t = 0
    while time.time() - t0 < dur + 18:
        new = nas_ls() - before
        if new:
            f = sorted(new)[-1]
            sz = int(nas(f"stat -c%s {NAS_FLAT}/{f}").stdout.strip().replace(PASS, "").strip() or 0)
            if sz != last_sz:
                last_sz = sz; stable_t = time.time(); got = f
            elif got and (time.time() - stable_t) >= 4 and sz > 100000:
                break
        time.sleep(2)
    if not got:
        print("!! NAS 未产出文件"); return False
    sz = int(nas(f"stat -c%s {NAS_FLAT}/{got}").stdout.strip().replace(PASS, "").strip() or 0)
    print("  NAS 新文件:", got, "size=", sz)
    if sz < 100000:
        print("!! 文件过小(%d), 疑似坏房/未真正开录" % sz); return False

    # scp 拉到本地 AGORA_REC_DIR(模拟目录映射)
    os.makedirs(C.AGORA_REC_DIR, exist_ok=True)
    local_src = os.path.join(C.AGORA_REC_DIR, got)
    r = scp_to(f"{NAS_FLAT}/{got}", local_src)
    if r.returncode != 0:
        print("!! scp 失败:", r.stderr[:200]); return False
    print("  拉到本地:", local_src, os.path.getsize(local_src), "bytes")

    # 走 orchestrator 的 place/归档
    name_tag = NO._sanitize(cred.get("name") or f"ROOM_{rid}")
    anchor = f"{name_tag}_{rid}"
    mid, fin = NO._place(local_src, NO._plat_name(474), anchor, name_tag)
    print("  暂存:", mid)
    print("  归档:", fin)
    print("  ffprobe(归档文件):")
    print("   ", probe(fin).replace("\n", "\n    "))
    okv = os.path.exists(fin) and os.path.getsize(fin) > 10000
    print("  测试A:", "✅ PASS" if okv else "❌ FAIL")
    return okv


def test_C_native_fail_fallback():
    print("\n========== 测试C: 原生失败->Web兜底分支 ==========")
    # 构造错误凭据(不存在channel), 原生应失败; 用假 web_start 验证回退被调用
    bad = {"ok": True, "appid": "6123b8d23d8b4f9190e1f759351a6236",
           "channel": "00000000-dead-beef-0000-000000000000", "token": "007ebad", "uid": "1", "name": "FAKE"}
    called = {"web": False}
    def fake_web_start(c):
        called["web"] = True
        return "WEBSESSION123"
    res = NO.record_room("999999", 474, 5, name="FAKE", cred=bad, web_start=fake_web_start)
    print("  结果:", res)
    okv = called["web"] and res.get("method") == "web"
    print("  测试C:", "✅ PASS(原生失败已回退Web)" if okv else "❌ FAIL")
    return okv


def test_D_health():
    print("\n========== 测试D: 服务健康+配置 ==========")
    info = SS.agora_info()
    print("  agora 配置:", info)
    h = AC.health()
    print("  health:", h)
    print("  测试D:", "✅ PASS" if h else "❌ FAIL")
    return h


def test_B_web_real():
    print("\n========== 测试B: 真实 Web 录制兜底 ==========")
    import client as CL, gateway
    import recorder as REC
    CL.ensure_token(force=True)
    rid = "6516530"
    cred = gateway.get_stream_credentials(rid, 474)
    if not cred.get("ok"):
        print("!! 密语凭据失败"); return False
    print("  房间:", rid, cred.get("name"), "mode=", cred.get("mode"))
    port = REC.ws_port()
    print("  recorder ws_port:", port)

    def web_start(c):
        out = REC.api_start(rid, 474, {
            "appid": c["appid"], "channel": c["channel"], "token": c["token"],
            "uid": c["uid"], "mode": c.get("mode"), "play_url": c.get("play_url") or ""},
            name=c.get("name"))
        print("  web api_start:", out)
        return out.get("rec_id") if out.get("ok") else None

    # 用 orchestrator 强制 web 路径
    res = NO.record_room(rid, 474, 12, name=cred.get("name"), cred=cred,
                         force_web=True, web_start=web_start)
    print("  orchestrator结果:", res)
    rec_id = res.get("rec_id")
    if not rec_id:
        print("!! web 未启动"); return False
    # 轮询状态到 done
    t0 = time.time(); final=None
    while time.time() - t0 < 60:
        sts = REC.api_status()
        t = next((x for x in sts if x.get("rec_id") == rec_id), None)
        if t:
            print("    state=%s bytes=%s seg=%s" % (t.get("state"), t.get("bytes"), t.get("seg")))
            if t.get("state") in ("done",) and t.get("mp4"):
                final = t.get("mp4"); break
            if t.get("state") in ("fail", "interrupted"):
                break
        time.sleep(3)
    # 停止
    try: REC.api_stop(rec_id)
    except Exception as e: print("  stop err", e)
    if final and os.path.exists(final) and os.path.getsize(final) > 10000:
        print("  web 成片:", final, os.path.getsize(final), "bytes")
        print("  ffprobe:", probe(final).replace("\n", " | "))
        print("  测试B: ✅ PASS")
        return True
    print("  !! web 未产出成片(final=%r)" % final)
    print("  测试B: ❌ FAIL")
    return False



if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}
    if which in ("all", "D"): results["D_health"] = test_D_health()
    if which in ("all", "C"): results["C_fallback"] = test_C_native_fail_fallback()
    if which in ("all", "A"): results["A_native"] = test_A_native()
    if which in ("all", "B"): results["B_web"] = test_B_web_real()
    print("\n========== 汇总 ==========")
    for k, v in results.items():
        print("  %-14s %s" % (k, "✅" if v else "❌"))
    print("ALL PASS" if all(results.values()) else "有失败项")
