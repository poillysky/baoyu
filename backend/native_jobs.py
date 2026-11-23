#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native_jobs.py — 密语原生录制任务管理(嵌入 watchman 调度)。

把 NAS agora 服务的"一次性录制"包装成与 recorder.api_status() 兼容的任务,
让 watchman 的 tick 状态机统一处理(开播开录/下播停录/增长监控)。

一个原生任务的生命周期(后台线程):
  1) 调 agora_client.start_by_room(duration 秒)  -> 服务端到点自动停并落盘
  2) 轮询 AGORA_REC_DIR 出现新 mp4 且大小稳定(>100KB)
  3) 移到 REC_DIR(暂存, 按项目三级目录命名) + 复制归档 FINAL_REC_DIR
  4) 标记 done(带 mp4 路径)

失败(服务不通/开录失败/超时无文件/文件过小) -> 标记 fail, 并置 fallback_web=True,
由调用方(watchman)在同一轮或下一轮回退 Web 录制。
"""
import os
import re
import time
import shutil
import subprocess
import threading

import config as C
import agora_client as AC

_LOCK = threading.Lock()
_JOBS = {}          # rec_id -> task dict (与 REC.api_status 项兼容 + 原生扩展字段)
_SEQ = 0

# ---- NAS 侧拉取配置(agora 服务与 baoyu 不是同主机时, 用 SSH/scp 拉回) ----
# 若两者共享同一挂载目录, 可设 NATIVE_SHARED_DIR 指向该目录, 此时直接读本地, 不走 SSH。
_NAS_SSH = os.environ.get("NAS_SSH", "poilly@192.168.2.38")
_NAS_PASS = os.environ.get("NAS_PASS", "2885748zhu")
_NAS_FLAT_DIR = os.environ.get("NAS_AGORA_DIR", "/vol1/1000/Docker/agora-recorder/recordings")
_SHARED_DIR = os.environ.get("NATIVE_SHARED_DIR", C.AGORA_REC_DIR)   # 共享挂载目录: 容器平铺输出已映射到本机此目录, 默认即 AGORA_REC_DIR; 非空则纯本地读写, 不走 SSH


def _nas_ssh(cmd, timeout=60):
    env = dict(os.environ, SSHPASS=_NAS_PASS)
    wrapped = "echo '%s' | sudo -S sh -c \"%s\"" % (_NAS_PASS, cmd)
    return subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-p", "22", _NAS_SSH, wrapped],
        input=_NAS_PASS + "\n", capture_output=True, text=True, timeout=timeout, env=env)


def _nas_scp(remote, local, timeout=180):
    env = dict(os.environ, SSHPASS=_NAS_PASS)
    # 远端文件 root 属主, 先 chmod 可读再 scp
    return subprocess.run(
        ["sshpass", "-e", "scp", "-o", "StrictHostKeyChecking=no", "-P", "22",
         "%s:%s" % (_NAS_SSH, remote), local],
        capture_output=True, text=True, timeout=timeout, env=env)


def _list_remote_mp4():
    """列 NAS 平铺目录下的 mp4 文件名集合。"""
    if _SHARED_DIR:
        try:
            return set(f for f in os.listdir(_SHARED_DIR) if f.lower().endswith(".mp4"))
        except FileNotFoundError:
            return set()
    r = _nas_ssh("ls -1 %s 2>/dev/null" % _NAS_FLAT_DIR)
    return set(x.strip() for x in (r.stdout or "").splitlines() if x.strip().lower().endswith(".mp4"))


def _remote_size(fn):
    if _SHARED_DIR:
        try:
            return os.path.getsize(os.path.join(_SHARED_DIR, fn))
        except OSError:
            return 0
    r = _nas_ssh("stat -c %%s %s/%s 2>/dev/null" % (_NAS_FLAT_DIR, fn))
    try:
        return int((r.stdout or "0").strip().splitlines()[-1])
    except Exception:
        return 0


def _fetch_to_local(fn, local_path):
    """把 NAS 上的文件拉到本地(拷贝到 local_path)。"""
    if _SHARED_DIR:
        shutil.copy2(os.path.join(_SHARED_DIR, fn), local_path)
        return
    _nas_ssh("chmod 0644 %s/%s 2>/dev/null" % (_NAS_FLAT_DIR, fn))
    res = _nas_scp("%s/%s" % (_NAS_FLAT_DIR, fn), local_path)
    if not os.path.exists(local_path) or os.path.getsize(local_path) < 1024:
        raise RuntimeError("scp 拉取失败: %s" % (res.stderr or "")[:200])


def _sanitize(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s or "")).strip("_")


def _plat_name(plat_id):
    try:
        from capturers import plat_name
        return plat_name(plat_id) or ("plat%s" % plat_id)
    except Exception:
        return {"474": "密语", "449": "半糖"}.get(str(plat_id), "plat%s" % plat_id)


def _place(src_local, plat_name, anchor, name_tag):
    """把已拉到本地的 src_local 放进 REC_DIR 暂存(三级), 并复制归档 FINAL_REC_DIR。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = "%s_%s.mp4" % (name_tag, stamp)
    mid_dir = os.path.join(C.REC_DIR, _sanitize(plat_name), anchor)
    os.makedirs(mid_dir, exist_ok=True)
    mid = os.path.join(mid_dir, fname)
    try:
        shutil.move(src_local, mid)
    except Exception:
        shutil.copy2(src_local, mid)
    final = None
    try:
        fdir = os.path.join(C.FINAL_REC_DIR, _sanitize(plat_name), anchor)
        os.makedirs(fdir, exist_ok=True)
        final = os.path.join(fdir, fname)
        shutil.copy2(mid, final)
    except Exception as e:
        print("[NATIVE] 归档失败(保留暂存):", e)
    return mid, final


def _new_rec_id():
    global _SEQ
    _SEQ += 1
    return "native-%d-%d" % (int(time.time()), _SEQ)


def _run(rec_id, room_id, plat_id, name, cred, duration):
    """后台线程: 原生录制一轮。更新 _JOBS[rec_id] 状态。"""
    task = _JOBS.get(rec_id)
    if not task:
        return
    try:
        appid, channel, token, uid = cred.get("appid"), cred.get("channel"), cred.get("token"), cred.get("uid")
        if not (appid and channel and token):
            task.update(state="fail", fallback_web=True, msg="凭据缺 appid/channel/token")
            return
        os.makedirs(C.AGORA_REC_DIR, exist_ok=True)
        before = _list_remote_mp4()
        task["state"] = "starting"
        st = AC.start_by_room(appid, channel, token, uid, duration)
        if not st.get("ok"):
            task.update(state="fail", fallback_web=True, msg="原生开录失败:%s" % st.get("msg"))
            return
        task["task_id"] = st.get("task_id")
        task["state"] = "recording"
        task["started_at"] = time.time()
        # 等 NAS 上出现新文件 + 大小稳定(轮询远端大小)
        deadline = time.time() + duration + 30
        cand_fn = None
        last_sz = -1
        stable_t = 0
        while time.time() < deadline:
            names = _list_remote_mp4()
            new = [f for f in names if f not in before]
            if new:
                fn = sorted(new)[-1]
                sz = _remote_size(fn)
                task["bytes"] = sz
                if sz != last_sz:
                    last_sz = sz
                    stable_t = time.time()
                    cand_fn = fn
                elif cand_fn and (time.time() - stable_t) >= 5 and sz > 102400:
                    break
            time.sleep(2.0)
        if (not cand_fn) or _remote_size(cand_fn) < 102400:
            task.update(state="fail", fallback_web=True, msg="原生录制未产出有效文件")
            return
        # 拉到本地临时文件, 再归位
        tmp = os.path.join(C.AGORA_REC_DIR, ".pull_%s.mp4" % rec_id)
        _fetch_to_local(cand_fn, tmp)
        name_tag = _sanitize(name or cred.get("name") or ("ROOM_%s" % room_id))
        anchor = "%s_%s" % (name_tag, room_id)
        mid, final = _place(tmp, _plat_name(plat_id), anchor, name_tag)
        task.update(state="done", mp4=final or mid, mid_path=mid, final_path=final,
                    folder=os.path.basename(os.path.dirname(mid)), bytes=os.path.getsize(mid))
    except Exception as e:
        task.update(state="fail", fallback_web=True, msg="原生异常:%r" % (e,))


def start(room_id, plat_id, name, cred, duration):
    """启动一个原生录制任务, 返回 task dict(含 rec_id/state)。"""
    rec_id = _new_rec_id()
    now = time.time()
    task = {
        "rec_id": rec_id, "room_id": str(room_id), "plat_id": int(plat_id),
        "name": name or cred.get("name"), "state": "starting", "engine": "agora_native",
        "bytes": 0, "seg": 0, "seg_bytes": 0, "mp4": None,
        "started_at": now, "msg": "", "fallback_web": False,
        "folder": None, "duration": duration,
    }
    with _LOCK:
        _JOBS[rec_id] = task
    t = threading.Thread(target=_run, args=(rec_id, room_id, plat_id, name, cred, duration),
                         daemon=True, name="native-%s" % rec_id)
    t.start()
    return dict(task)


def status():
    """返回所有原生任务(与 recorder.api_status 项兼容的列表)。"""
    with _LOCK:
        return [dict(t) for t in _JOBS.values()]


def get(rec_id):
    with _LOCK:
        t = _JOBS.get(rec_id)
        return dict(t) if t else None


def stop(rec_id):
    """停止原生任务(尽力: 调服务 stop; 线程到点自停)。"""
    with _LOCK:
        task = _JOBS.get(rec_id)
    if not task:
        return {"ok": False, "msg": "无此原生任务"}
    tid = task.get("task_id")
    if tid:
        AC.stop(tid)
    task["state"] = "done" if task.get("mp4") else "stopped"
    return {"ok": True, "rec_id": rec_id, "state": task["state"]}


def reap_finished(max_age=3600):
    """清理完成超过 max_age 秒的旧任务(内存)。"""
    now = time.time()
    with _LOCK:
        for k in list(_JOBS.keys()):
            t = _JOBS[k]
            if t.get("state") in ("done", "fail", "stopped") and now - t.get("started_at", now) > max_age:
                _JOBS.pop(k, None)
