#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监守自动录制看门狗 (watchman.py)
核心: 监控清单持久化 + 后台定时轮询 -> 开播自动录 / 下播自动停。

开播判定(最省额度): 拉「平台房间列表 /api/live/plat/detail」(不消耗观看次数, 带 online 字段),
一次覆盖整平台所有在线房; 对清单内每房比对 online:
  - online=1 且未在录 -> 拉流凭据 + 启动录制
  - 下播(不在列表/online=0) 且 在录 -> 停止录制
遇换号中(switching)本轮跳过, 等 token 恢复。
"""
import os, sys, json, time, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C
import client as CL
import gateway as GW
import recorder as REC
import status_db as SDB
try:
    import native_jobs as NJ
    import agora_client as AC
except Exception:
    NJ = None
    AC = None

# 原生录制优先的平台(密语): 原生直录零转码; 失败自动回退 web
_NATIVE_PLAT_IDS = {"474"}
# 原生录制时长(秒/条), 到点服务端自动停并落盘, 之后 watchman 增长监控发现无新文件再起下一条
_NATIVE_CHUNK_SEC = int(os.environ.get("NATIVE_CHUNK_SEC", "1800"))
# 原生录制失败过的房: 本轮及后续改走 web(避免反复原生失败风暴); 值=失败时间戳
_NATIVE_FAILED = {}


def _all_status():
    """合并 web(recorder) 与 原生(native_jobs) 任务状态, 供 tick 统一处理。"""
    out = list(REC.api_status())
    if NJ is not None:
        try:
            out += NJ.status()
        except Exception:
            pass
    return out


_NATIVE_FAIL_COOLDOWN = int(os.environ.get("NATIVE_FAIL_COOLDOWN", "1800"))


def _native_recently_failed(rid):
    """该房原生录制最近是否失败(冷却期内)-> 是则改走 web。"""
    ts = _NATIVE_FAILED.get(str(rid))
    if not ts:
        return False
    if time.time() - ts > _NATIVE_FAIL_COOLDOWN:
        _NATIVE_FAILED.pop(str(rid), None)
        return False
    return True


def _note_native_fail(rid):
    _NATIVE_FAILED[str(rid)] = time.time()

WL_PATH = os.path.join(HERE, "watchlist.json")
_WL_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()       # 后台轮询与手动 /watch/check 串行，防重复开录
_WATCH = {}          # 源: room_id(str) -> {room_id, plat_id, name, cover, added_at}
_STATE = {}          # room_id -> 最近一次判定 {online, rec_id, state, checked_at, offline_streak}
_OFFLINE_STREAK_STOP = 2   # 连续几轮判离线才停止录制（防网络抖动误停）
_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "60"))   # 轮询秒
_STOP = threading.Event()
_THREAD = None


def get_interval():
    return _INTERVAL


def set_interval(sec):
    global _INTERVAL
    sec = int(sec)
    if sec < 5:
        sec = 5
    _INTERVAL = sec
    log("轮询间隔设为 %ds" % _INTERVAL)
    return _INTERVAL


def log(*a):
    print("[WATCH]", *a, flush=True)


# ---------- 监控清单持久化 ----------
def _load():
    global _WATCH
    try:
        with open(WL_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
        if not isinstance(arr, list):
            raise ValueError("watchlist 根节点不是数组")
        _WATCH = {}
        for x in arr:
            rid = str(x["room_id"])
            x.setdefault("enabled", True)
            _WATCH[rid] = x
    except FileNotFoundError:
        _WATCH = {}
    except Exception as e:
        # 配置损坏时保留原文件，避免静默覆盖；原子写入可防止运行中截断。
        _WATCH = {}
        log("load watchlist err", repr(e))


def _save():
    tmp = WL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(_WATCH.values()), f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, WL_PATH)
    except OSError:
        # bind-mount 单文件(容器内) os.replace 到挂载 inode 会 EBUSY;
        # 退化为原地直写挂载文件本身, 保证 watchlist 持久化可用
        with open(WL_PATH, "w", encoding="utf-8") as f:
            json.dump(list(_WATCH.values()), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _watch_snapshot():
    """一致性快照，避免 HTTP 增删清单时看门狗遍历字典发生冲突。"""
    with _WL_LOCK:
        return {rid: dict(it) for rid, it in _WATCH.items()}


def _is_watched(room_id):
    with _WL_LOCK:
        return str(room_id) in _WATCH


def list_watch():
    """读监控状态表(sqlite) 直接返回；清单里还没进表的房本轮回落现算。"""
    rows = SDB.read_list()
    watch = _watch_snapshot()
    have = {r["room_id"] for r in rows}
    # 清单中存在但表里还没有（刚加入、尚未过一轮）：回落实时计算
    missing = [rid for rid in watch if rid not in have]
    if missing:
        recs = {str(r.get("room_id")): r for r in _all_status()}
        now = time.time()
        for rid in missing:
            it = watch[rid]
            st = _STATE.get(rid, {})
            rec = recs.get(rid)
            rec_state = (rec or {}).get("state")
            started = (rec or {}).get("started_at")
            recording = rec_state in ("recording", "starting", "restarting", "stopping")
            disk_dur, disk_bytes = _folder_stats(rid, it, rec)
            live_dur = int(disk_dur) + (int(max(0, now - started)) if (recording and started) else 0)
            rows.append({
                "room_id": rid, "plat_id": it.get("plat_id"), "name": it.get("name"),
                "cover": it.get("cover"), "added_at": it.get("added_at"),
                "online": st.get("online"), "rec_state": rec_state,
                "rec_id": (rec or {}).get("rec_id"), "rec_started_at": started,
                "rec_duration": live_dur, "rec_bytes": disk_bytes,
                "checked_at": st.get("checked_at"), "last_msg": st.get("last_msg"),
            })
    # 过滤掉已不在清单的（prune 兜底）
    rows = [r for r in rows if r["room_id"] in watch]
    # enabled 以清单为准(权威源=watchlist.json, 即时开关生效; 覆盖表内可能滞后的值)
    for r in rows:
        r["enabled"] = watch.get(r["room_id"], {}).get("enabled", True) is not False
    rows.sort(key=lambda x: x.get("added_at") or 0, reverse=True)
    return rows


def _folder_stats(rid, it, rec):
    """统计该主播在磁盘上的真实产物：所有 mp4 总时长 + mp4/webm 总大小(字节)。
    录制中时当前正在写的 mp4 也计入其文件大小（时长以 elapsed 补）。
    兼容新旧布局: recordings/<主播_room>/ 旧二级 与 recordings/<平台>/<主播_room>/ 三级。"""
    import glob as _glob
    folder = (rec or {}).get("folder") if rec else None
    # 没有录制任务时，按 主播名_roomid 推算文件夹
    cand_dirs = []
    if folder:
        cand_dirs.append(os.path.join(C.REC_DIR, folder))
        # 三级布局: 平台目录下的同名字目录
        cand_dirs += _glob.glob(os.path.join(C.REC_DIR, "*", folder))
        cand_dirs += _glob.glob(os.path.join(C.REC_DIR, "*", os.path.basename(folder)))
    nm = str(it.get("name") or "").strip()
    if nm:
        anchor = "%s_%s" % (nm, rid)
        cand_dirs.append(os.path.join(C.REC_DIR, anchor))
        cand_dirs.append(os.path.join(C.REC_DIR, nm))
        cand_dirs += _glob.glob(os.path.join(C.REC_DIR, "*", anchor))
        cand_dirs += _glob.glob(os.path.join(C.REC_DIR, "*", nm))
    seen = set()
    total_dur = 0.0
    total_bytes = 0
    for d in cand_dirs:
        if not d or d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        mp4s = _glob.glob(os.path.join(d, "*.mp4"))
        webms = _glob.glob(os.path.join(d, "*.webm"))
        for f in mp4s:
            try:
                total_bytes += os.path.getsize(f)
            except OSError:
                pass
            total_dur += _probe_dur(f)
        for f in webms:
            try:
                total_bytes += os.path.getsize(f)
            except OSError:
                pass
    return total_dur, total_bytes


_DUR_CACHE2 = {}
def _probe_dur(path):
    """ffprobe 取单文件时长(秒)，按 (path,mtime,size) 缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    key = (int(st.st_mtime), st.st_size)
    cached = _DUR_CACHE2.get(path)
    if cached and cached[0] == key:
        return cached[1]
    import subprocess, shutil, json as _json
    ff = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    dur = 0.0
    try:
        r = subprocess.run([ff, "-v", "quiet", "-print_format", "json", "-show_format", path],
                           timeout=15, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        info = _json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
        dur = float((info.get("format") or {}).get("duration", 0) or 0)
    except Exception:
        dur = 0.0
    _DUR_CACHE2[path] = (key, dur)
    return dur


def add(room_id, plat_id, name=None, cover=None):
    room_id = str(room_id); plat_id = str(plat_id)
    with _WL_LOCK:
        previous = dict(_WATCH[room_id]) if room_id in _WATCH else None
        try:
            if previous is not None:
                _WATCH[room_id].update({"name": name or _WATCH[room_id].get("name"),
                                        "cover": cover or _WATCH[room_id].get("cover")})
            else:
                _WATCH[room_id] = {"room_id": room_id, "plat_id": plat_id,
                                   "name": name, "cover": cover,
                                   "enabled": True, "added_at": time.time()}
            _save()
        except Exception:
            if previous is None:
                _WATCH.pop(room_id, None)
            else:
                _WATCH[room_id] = previous
            raise
    log("加入监控", room_id, name)
    return {"ok": True, "room_id": room_id}


def set_enabled(room_id, enabled):
    """启用/关闭监控: 关闭时若该房正在录一并停掉; 启用后下轮自动恢复监守。"""
    room_id = str(room_id); enabled = bool(enabled)
    with _WL_LOCK:
        it = _WATCH.get(room_id)
        if it is None:
            return {"ok": False, "msg": "不在监控清单"}
        it["enabled"] = enabled
        _save()
    if not enabled:
        rec = next((r for r in _all_status() if str(r.get("room_id")) == room_id), None)
        if rec:
            _stop_rec_background(rec.get("rec_id"))
            log("关闭监控, 后台停止录制", room_id)
        _STATE.pop(room_id, None)
    try:
        SDB.set_enabled(room_id, enabled)   # 立即写库, 前端重读秒级生效
    except Exception:
        pass
    log("监控", room_id, "启用" if enabled else "关闭")
    return {"ok": True, "room_id": room_id, "enabled": enabled}


def _stop_any(rec_id):
    """统一停止: native-*/ 走 native_jobs, 其余走 recorder。"""
    if not rec_id:
        return
    if str(rec_id).startswith("native-") and NJ is not None:
        try:
            NJ.stop(rec_id)
        except Exception:
            pass
    else:
        REC.api_stop(rec_id)


def _stop_rec_background(rec_id):
    """后台停录: REC.api_stop 会等录制收尾(可能几十秒), 放进线程不阻塞 HTTP。"""
    def run():
        try:
            _stop_any(rec_id)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="stop-rec-%s" % rec_id).start()


def remove(room_id):
    room_id = str(room_id)
    with _WL_LOCK:
        it = _WATCH.pop(room_id, None)
        try:
            _save()
        except Exception:
            if it is not None:
                _WATCH[room_id] = it
            raise
    # 若该房正在录, 一并后台停止(不阻塞删除响应)
    rec = next((r for r in _all_status() if str(r.get("room_id")) == room_id), None)
    if rec:
        _stop_rec_background(rec.get("rec_id"))
        log("删除监控, 后台停止录制", room_id)
    _STATE.pop(room_id, None)
    try:
        SDB.delete(room_id)
    except Exception:
        pass
    log("移出监控", room_id)
    return {"ok": True, "removed": bool(it)}


# ---------- 轮询判定 ----------

# 直录/web 起播后, 前几秒 ffmpeg 握手/首帧未落盘, 不判“无增长”
_F_START_WARMUP = 15          # 录制定为 recording 后 15s 内不做文件增长判定
_RESTART_COOLDOWN_DEF = 120   # 流异常重启后的冷却秒数, 防一帧都不出时无限重启风暴
_MONITOR_INTERVAL = 60        # 每轮询间隔(秒): 用户要求每 1 分钟拉一次平台列表


def _start_for(rid, plat_id, it):
    """拉流凭据并启动录制(带移除串行保护)。返回 (ok, msg, rec_id)。
    原生平台(密语474): 优先原生直录, 失败自动回退 web。"""
    log("【开播】自动录制", rid, it.get("name"))
    try:
        res = GW.get_stream_credentials(rid, plat_id, 0)
        if not res.get("ok"):
            if res.get("switching"):
                log("  换号中, 下轮再试", rid)
                _STATE.setdefault(rid, {})["last_msg"] = "观看次数不足, 正在换号重试…(游客受限, 建议登录会员)"
            else:
                log("  拉流失败", rid, res.get("msg"))
                _STATE.setdefault(rid, {})["last_msg"] = "拉流失败: %s" % (res.get("msg") or "")
            return False, res.get("msg") or "拉流失败", None
        cred = {
            "appid": res["appid"], "channel": res["channel"],
            "token": res["token"], "uid": res["uid"],
            "real_room_id": res.get("real_room_id"),
            "mode": res.get("mode"), "play_url": res.get("play_url") or "",
            "name": res.get("name") or it.get("name"),
        }
        # 与 remove 串行: 注册任务后再放行移除, remove 必能查到并停止它,
        # 防止“移除监控后慢速拉流返回, 又把录制开起来”的孤儿任务。
        with _WL_LOCK:
            if rid not in _WATCH:
                return False, "已移出监控", None
            # 原生优先
            if (NJ is not None and AC is not None and str(plat_id) in _NATIVE_PLAT_IDS and AC.enabled()
                    and not _native_recently_failed(rid)):
                try:
                    task = NJ.start(rid, plat_id, cred.get("name"), cred, _NATIVE_CHUNK_SEC)
                    log("  原生录制已启动", rid, task.get("rec_id"), task.get("state"))
                    _STATE.setdefault(rid, {})["last_msg"] = "原生录制已启动"
                    return True, "原生录制已启动", task.get("rec_id")
                except Exception as e:
                    log("  原生启动异常, 回退 web", rid, repr(e))
            out = REC.api_start(rid, plat_id, cred,
                                name=res.get("name") or it.get("name"))
        if not out.get("ok"):
            _STATE.setdefault(rid, {})["last_msg"] = out.get("msg") or "录制启动失败"
            log("  录制未启动", rid, _STATE.get(rid,{}).get("last_msg"))
            return False, out.get("msg") or "录制启动失败", None
        log("  录制已启动", rid, out.get("rec_id"), out.get("state"))
        _STATE.setdefault(rid, {})["last_msg"] = "录制已启动 %s" % (out.get("state") or "")
        return True, "录制已启动", out.get("rec_id")
    except Exception as e:
        log("  start err", rid, repr(e))
        _STATE.setdefault(rid, {})["last_msg"] = "启动异常: %r" % (e,)
        return False, repr(e), None


def _monitor_interval():
    return _MONITOR_INTERVAL


_GROWTH = {}          # room_id -> [probe1, probe2, probe3] 每元素 (ts, bytes, dur)
_GROWTH_MAX = 6       # 记录最近几轮的快照, 判断文件是否在增长
# 连续几轮无增长才判异常重启。拉长容忍: 主播短暂卡顿/换人/连麦抖动本来会自愈,
# 不该几秒不长就重启把连续文件切成碎片(降画质问题, 只是判异常变耐心)。
# 真下播由平台在线状态(_OFFLINE_STREAK_STOP)判, 不受影响; 页面真崩由 ws 关闭快速触发。
_GROWTH_STALL = int(os.environ.get("GROWTH_STALL_ROUNDS", "5"))   # ≈5*WATCH_INTERVAL 秒


def _probe_growth(rid, it, rec):
    """记录 (ts, 文件总字节, 时长) 快照, 返回 (growing, stalled_rounds)。
    直录: bytes 来自磁盘文件(ffmpeg 写盘)。web 录: 磁盘实时 mp4 也会写。"""
    now = time.time()
    dur, nbytes = _folder_stats(rid, it, rec)
    arr = _GROWTH.setdefault(rid, [])
    arr.append((now, nbytes, dur))
    if len(arr) > _GROWTH_MAX:
        del arr[:len(arr) - _GROWTH_MAX]
    if len(arr) < 2:
        return False, 0            # 样本不足, 判无可判
    stalled = 0
    for (t0, b0, _d0), (t1, b1, _d1) in zip(arr, arr[1:]):
        if b1 <= b0 and (t1 - t0) >= _INTERVAL * 0.5:
            stalled += 1
    return (b1 > b0), stalled


def _growth_reset(rid):
    _GROWTH.pop(rid, None)

def _online_map(plat_id):
    """拉该平台在线房 -> {room_id(str): online_bool}。不消耗观看次数。"""
    m = {}
    page_types = [0]
    for rt in page_types:
        try:
            r = CL.rooms(int(plat_id), rt)
        except Exception as e:
            log("rooms err plat", plat_id, repr(e))
            return None
        if not r.get("ok"):
            return None
        for room in r.get("data", []):
            rid = str(room.get("room_id") or room.get("id"))
            m[rid] = bool(room.get("online"))
    return m


def _tick():
    """后台轮询与手动检查共用互斥锁，避免并发重入导致重复开录。"""
    with _TICK_LOCK:
        _tick_inner()


def _tick_inner():
    """执行一轮: 检查清单每房 -> 开播自动录 / 下播自动停。"""
    watch = _watch_snapshot()
    # 孤儿任务清理: 活跃录制任务(recording/starting/restarting)其房已不在 watchlist,
    # 或该监控房 enabled=False -> 停掉, 防止"移出监控后录制还在跑"(移除与后台停录竞态残留)。
    try:
        for r in _all_status():
            rrid = str(r.get("room_id"))
            rstate = r.get("state")
            if rstate not in ("recording", "starting", "restarting"):
                continue
            wit = watch.get(rrid)
            orphan = (wit is None) or (wit.get("enabled") is False)
            if orphan:
                try:
                    _stop_any(r.get("rec_id"))
                    log("孤儿任务清理: 房已不在监控, 停止录制", rrid, r.get("rec_id"))
                except Exception:
                    pass
                _STATE.pop(rrid, None)
    except Exception as e:
        log("孤儿清理异常", repr(e))
    if not watch:
        return
    if CL.switching():
        log("换号中, 本轮跳过")
        return
    # 按平台分组拉在线映射
    by_plat = {}
    for rid, it in watch.items():
        by_plat.setdefault(str(it.get("plat_id")), []).append(rid)
    recs_by_room = {}
    for r in _all_status():
        recs_by_room[str(r.get("room_id"))] = r
    for plat_id, rids in by_plat.items():
        omap = _online_map(plat_id)
        if omap is None:
            log("平台", plat_id, "列表不可用, 跳过本轮")
            continue
        for rid in rids:
            if not _is_watched(rid):
                continue
            it = watch.get(rid) or {}
            # 关闭中的监控房: 不参与自动开录(含拉流), 等待手动启用
            if it.get("enabled") is False:
                rec_off = recs_by_room.get(rid)
                # 轮询中发现关停前遗留的录制任务(如手动托管), 仍按状态机停掉
                if rec_off and rec_off.get("state") in ("recording", "starting", "restarting"):
                    try:
                        _stop_any(rec_off.get("rec_id"))
                        log("监控已关闭, 停止遗留录制", rid)
                    except Exception:
                        pass
                    _STATE.pop(rid, None)
                continue
            online = bool(omap.get(rid))
            rec = recs_by_room.get(rid)
            rec_state = rec.get("state") if rec else None
            now = time.time()
            prev = _STATE.get(rid, {})
            # 离线连击计数（防抖）
            offline_streak = 0 if online else int(prev.get("offline_streak", 0)) + 1
            rec_id_cur = (rec or {}).get("rec_id")
            started = (rec or {}).get("started_at")
            # 每轮对录制中的房记录磁盘文件快照, 判文件是否在增长
            growing, stalled = _probe_growth(rid, it, rec)
            _STATE[rid] = {"online": online, "checked_at": now,
                           "rec_id": rec_id_cur, "state": rec_state,
                           "offline_streak": offline_streak,
                           "restart_cooldown": prev.get("restart_cooldown"),
                           "last_msg": prev.get("last_msg")}
            cur_active = rec_state in ("recording", "starting", "restarting", "stopping")
            # 原生录制失败(fallback_web) -> 登记失败冷却, 本轮即回退 web 重开
            if rec and rec_state == "fail" and rec.get("fallback_web") and rec.get("engine") == "agora_native":
                _note_native_fail(rid)
                log("【原生失败】回退 web 录制", rid, it.get("name"), rec.get("msg"))
                cur_active = False  # 强制本轮走 _start_for(此时原生已进冷却, 会选 web)
            if online and not cur_active:
                # 在线 && 上状态=停止录制 -> 开始录制
                _start_for(rid, plat_id, it)
            elif online and cur_active:
                # 在线 && 上状态=录制中 -> 用本地文件判断是否还在正常录制
                age = (now - started) if started else 0
                samples = len(_GROWTH.get(rid, []))
                if age < _F_START_WARMUP or samples < 2:
                    _STATE[rid]["last_msg"] = "录制中, 文件样本采集中(%ds)" % int(age)
                    continue
                rc = prev.get("restart_cooldown")
                if rc and now - rc < _RESTART_COOLDOWN_DEF:
                    _STATE[rid]["last_msg"] = "流异常, 重启冷却中(%ds)" % int(_RESTART_COOLDOWN_DEF - (now - rc))
                    continue
                if growing:
                    _STATE[rid]["last_msg"] = "录制中, 文件正常增长"
                elif stalled >= _GROWTH_STALL:
                    # 文件连续多轮不变 -> 录制异常: 任务内重启(不建新任务, 保留段)
                    log("【流异常】文件无增长, 任务内重启录制", rid, it.get("name"),
                        "stalled", stalled)
                    try:
                        if rec_id_cur and not str(rec_id_cur).startswith("native-"):
                            r = REC.api_restart(rec_id_cur)
                            log("  任务内重启", rec_id_cur, r.get("ok"), r.get("state"))
                        elif rec_id_cur:
                            # 原生任务无增长: 停掉当前分片, 下轮会重开新分片
                            _stop_any(rec_id_cur)
                        _growth_reset(rid)
                        _STATE[rid]["restart_cooldown"] = time.time()
                        _STATE[rid]["last_msg"] = "流异常, 任务内重启录制"
                    except Exception as e:
                        log("  重启异常 err", rid, repr(e))
                        _STATE[rid]["last_msg"] = "流异常重启失败: %r" % (e,)
                else:
                    _STATE[rid]["last_msg"] = "录制中, 文件未增长(%d/%d)" % (stalled, _GROWTH_STALL)
            elif (not online) and cur_active:
                # 离线 && 上状态=录制中 -> 下播, 关闭录制进程
                if offline_streak >= _OFFLINE_STREAK_STOP:
                    log("【下播】停止录制", rid, it.get("name"), "离线连击", offline_streak)
                    try:
                        if rec_id_cur:
                            _stop_any(rec_id_cur)
                        _STATE[rid]["last_msg"] = "已下播, 关闭录制"
                    except Exception as e:
                        log("  stop err", rid, repr(e))
                    _growth_reset(rid)
                else:
                    _STATE[rid]["last_msg"] = "疑似下播, 等待确认(%d/%d)…" % (offline_streak, _OFFLINE_STREAK_STOP)
            else:
                # 离线 && 上状态=停止录制 -> 跳过
                _STATE[rid]["last_msg"] = "离线, 等待开播"

    # 本轮把每房最新状态写入 sqlite 表（页面直接读表秒回，不现算）
    try:
        _persist_all()
    except Exception as e:
        log("persist err", repr(e))
    # 推状态给前端
    try:
        import hub as HUB
        HUB.broadcast("watch_status", {"ok": True, "data": list_watch()})
        HUB.broadcast("rec_status", _all_status())
    except Exception:
        pass


def _persist_all():
    """把当前清单每房的判定+真实产物统计单事务写入 status.db。"""
    watch = _watch_snapshot()
    recs = {str(r.get("room_id")): r for r in _all_status()}
    now = time.time()
    rows = []
    for rid, it in watch.items():
        st = _STATE.get(rid, {})
        rec = recs.get(rid)
        rec_state = (rec or {}).get("state")
        started = (rec or {}).get("started_at")
        recording = rec_state in ("recording", "starting", "restarting", "stopping")
        disk_dur, disk_bytes = _folder_stats(rid, it, rec)
        live_dur = int(disk_dur)
        if recording and started:
            live_dur = int(disk_dur + max(0, now - started))
        rows.append({
            "room_id": rid, "plat_id": it.get("plat_id"), "name": it.get("name"),
            "cover": it.get("cover"), "added_at": it.get("added_at"),
            "online": st.get("online"), "rec_state": rec_state,
            "rec_id": (rec or {}).get("rec_id"), "rec_started_at": started,
            "rec_duration": live_dur, "rec_bytes": disk_bytes,
            "checked_at": st.get("checked_at"), "last_msg": st.get("last_msg"),
            "enabled": it.get("enabled", True),
        })
    SDB.upsert_many(rows)
    SDB.prune(watch.keys())


def _loop():
    log("监控看门狗启动, 轮询间隔 %ds" % _INTERVAL)
    while not _STOP.is_set():
        try:
            _tick()
        except Exception as e:
            log("tick err", repr(e))
        _STOP.wait(_INTERVAL)


def start():
    global _THREAD
    _load()
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="watchman")
    _THREAD.start()


def stop():
    _STOP.set()
