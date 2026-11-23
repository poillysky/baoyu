#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监守录屏 录制引擎 (recorder.py) — 完善版
吸收 ihmily/DouyinLiveRecorder + fideo-live-record 经验:
- 每房一个 headless Chromium page, recorder.html 用 Agora Web SDK join
- canvas.captureStream + MediaRecorder(webm vp8 + 音轨 aac/opus) 高分片
- 分片经 WebSocket 推回后端落 webm
- 2GB 自动切分 -> 串行转码队列 -> mp4
- 看门狗: 录制页异常/断连 -> 自动重启录制(已录段保住)
- 断连即收尾: 立即把已录 webm 转 mp4, 避免文件丢失
- 状态变化经 hub 实时推前端
"""
import asyncio, os, time, json, threading, sys, uuid, glob, shutil
import config as C

HERE = C.HERE
sys.path.insert(0, HERE)

# ---- 录制任务管理 ----
_TASKS = {}                 # rec_id -> task dict
_TASKS_LOCK = threading.Lock()
_LOOP_INIT_LOCK = threading.Lock()
_BROWSER = None
_BROWSER_LOCK = None
_PW = None
_LOOP = None
_WS_SERVER = None
_WS_PORT = None
_LAST_ACTIVE = time.time()   # 最近一次浏览器活动时间(任务启动/数据到达时刷新)

# 串行转码队列 (避免多段 ffmpeg 同时抢 CPU)
_TRANSCODE_Q = None         # asyncio.Queue
_TRANSCODE_WORKER = None

try:
    import websockets
    HAVE_WS = True
except Exception:
    HAVE_WS = False

import hub as HUB
import status_db as SDB   # 录制任务实时落库(前端秒读数据库)


def log(*a):
    print("[REC]", *a, flush=True)


def _notify_stats():
    """任务状态变化 -> 广播前端 + 全量快照。"""
    try:
        data = _sync_status()
        HUB.broadcast("rec_status", data)
    except Exception:
        pass


# ============ Playwright ============
# 单 Chromium 多 Tab: 全局只 launch 一次, 每房一个独立 BrowserContext + page。
# 内存从"N×200MB"降到"1×200MB + 每房独立渲染进程几十MB"。
_CHROMIUM_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    # 后台不降频: 多 tab 时非前台页也要全速编码
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # 媒体
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    # 容器/无沙箱场景
    "--no-sandbox",
    "--disable-dev-shm-usage",
    # ★性能: 禁 GPU/合成器(无头录制用不到, 省大量进程与 CPU)
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    "--disable-accelerated-2d-canvas",
    # ★削减无关服务与后台进程
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--no-first-run",
    "--no-default-browser-check",
    "--metrics-recording-only",
    "--mute-audio",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints",
    # 单浏览器多渲染进程上限放宽, 但每渲染进程堆限 256M 防爆
    "--renderer-process-limit=24",
    "--js-flags=--max-old-space-size=256",
    "--disk-cache-size=33554432",
]


async def _get_browser():
    global _BROWSER, _PW, _BROWSER_LOCK
    if _BROWSER is not None and _BROWSER.is_connected():
        return _BROWSER
    # 多房同时启动时只允许一个 launch；否则 await 交错会启动多个 Chromium。
    if _BROWSER_LOCK is None:
        _BROWSER_LOCK = asyncio.Lock()
    async with _BROWSER_LOCK:
        if _BROWSER is not None and _BROWSER.is_connected():
            return _BROWSER
        from playwright.async_api import async_playwright
        if _PW is None:
            _PW = await async_playwright().start()
        _BROWSER = await _PW.chromium.launch(
            executable_path=C.CHROMIUM,
            headless=True,
            args=_CHROMIUM_ARGS)
        _LAST_ACTIVE = time.time()
        log("Chromium started (single-browser mode)")
        return _BROWSER


# ============ 实时转封装 (webm 分片边收边转 mp4, 不攒最后一次性转) ============
# 说明: 浏览器 MediaRecorder 出的是 webm(vp8/opus) 分片, mp4 容器不接受 vp8,
# 必须实时转码为 h264/aac。常驻 ffmpeg 进程收 stdin 分片, 实时输出 fragmented mp4
# (frag_keyframe+empty_moov) 边写边成可用文件; 停止时关 stdin 秒级收尾, 无最后长等待。
def _ff_args(mp4, copy_video=False, in_fmt="webm", src_url=None):
    """ffmpeg 常驻实时转封装参数。
    - src_url 给定时: 直接从网络源拉流(FLV/HLS), 不吃 stdin 管道。
    - copy_video=True (MediaRecorder 已出 h264 / 源流已是 h264+aac): -c:v copy 零重编, CPU 最低。
    - 否则 libx264 ultrafast+zerolatency 实时转, 多线程抢速度。"""
    args = [C.FFMPEG, "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+igndts+discardcorrupt"]
    if src_url:
        # 直录源流: FLV/HLS 由 ffmpeg 自动探测格式; 超时/断流由看门狗处理。
        # HLS ts 段走 https 时部分 CDN(ooat88) 证书链不完整 → -tls_verify 0 跳过校验。
        args += ["-rw_timeout", "15000000", "-i", src_url]
    else:
        args += ["-f", in_fmt, "-i", "pipe:0"]
    if copy_video:
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-crf", "23", "-g", "50", "-pix_fmt", "yuv420p",
                 "-threads", "0", "-x264-params", "slice-max-size=1500:aud=1"]
    args += ["-c:a", "aac", "-b:a", "96k",
             "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
             "-y", mp4]
    return args


async def _start_ff(task, mp4, copy_video=None):
    """为当前段启动常驻 ffmpeg 实时转封装进程。
    直录任务(task['mode']=='direct'): 从源 URL 拉流。
    copy_video 未指定时按 task['rec_mime'] 判断是否已出 h264。"""
    await _stop_ff(task, finalize=False)
    is_direct = task.get("mode") == "direct"
    if copy_video is None:
        if is_direct:
            # 直录: 源流本来就是 h264+aac(FLV/HLS), 零重编; 若源是别的编码, mp4 容器
            # 不接受时 ffmpeg 会报错退出, 由看门狗换转码参数重启。
            copy_video = True
        else:
            mime = str(task.get("rec_mime") or "")
            copy_video = ("h264" in mime) and C.TRANSCODE_FAST
    in_fmt = "matroska" if "matroska" in str(task.get("rec_mime") or "") else "webm"
    try:
        # OpenSSL 版 ffmpeg 需要 CA 包环境变量(部分 CDN 证书链含自签交叉根)
        env = dict(os.environ)
        ca = getattr(C, "FFMPEG_CA_FILE", "")
        if ca and os.path.exists(ca):
            env["SSL_CERT_FILE"] = ca
        proc = await asyncio.create_subprocess_exec(
            *_ff_args(mp4, copy_video=copy_video, in_fmt=in_fmt,
                      src_url=task.get("play_url") if is_direct else None),
            stdin=asyncio.subprocess.PIPE if not is_direct else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env)
        task["ff"] = proc
        task["ff_mp4"] = mp4
        task["ff_copy"] = copy_video
        log("ffmpeg realtime start ->", os.path.basename(mp4),
            "(direct src)" if is_direct else ("(copy)" if copy_video else "(x264 ultrafast)"))
        return True
    except Exception as e:
        log("ffmpeg start err", repr(e))
        task["ff"] = None
        return False


async def _ff_write(task, data):
    """把分片写入 ffmpeg stdin，并 drain 施加背压，避免转码慢时内存无限堆积。"""
    proc = task.get("ff")
    if not proc or proc.stdin is None or proc.returncode is not None:
        return
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except Exception as e:
        log("ff write err", repr(e))


async def _stop_ff(task, finalize=True):
    """关闭 ffmpeg stdin, 等其收尾写 moov。返回输出 mp4 是否有效。"""
    proc = task.get("ff"); mp4 = task.get("ff_mp4")
    task["ff"] = None
    if proc:
        try:
            if proc.stdin: proc.stdin.close()
        except Exception: pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            try: proc.kill(); await proc.wait()
            except Exception: pass
    if finalize and mp4 and os.path.exists(mp4) and os.path.getsize(mp4) > 1024:
        return True
    return False


# ============ 转码串行队列 ============
async def _transcode_worker():
    while True:
        webm, mp4, fut = await _TRANSCODE_Q.get()
        try:
            ok = await _transcode(webm, mp4)
        except Exception as e:
            log("transcode task err", e); ok = False
        try:
            if fut and not fut.done():
                fut.set_result(ok)
        except Exception:
            pass
        _TRANSCODE_Q.task_done()


async def _transcode(webm, mp4):
    """ffmpeg webm->mp4 (含音轨), 失败回退纯视频。"""
    if not os.path.exists(webm) or os.path.getsize(webm) < 1024:
        return False
    # 先试带音轨
    cmd = [C.FFMPEG, "-y", "-loglevel", "error", "-i", webm] + C.TRANSCODE_ARGS + [mp4]
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(p.communicate(), timeout=600)
        if p.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            try: os.remove(webm)
            except Exception: pass
            return True
        msg = (err or b"")[-300:].decode("utf-8", "ignore")
        log("ffmpeg(with audio) fail, retry video-only:", msg)
    except Exception as e:
        log("ffmpeg exc", repr(e))
        # 超时/异常: 主动杀子进程, 避免孤儿后台继续写造成状态混乱
        try:
            if p and p.returncode is None:
                p.kill()
                await p.wait()
        except Exception:
            pass
        # 兜底: 若超时但文件已写完且有效, 认成功
        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            try: os.remove(webm)
            except Exception: pass
            return True
    # 回退: 无音频轨 (找不到音频流时不报错)
    cmd2 = [C.FFMPEG, "-y", "-loglevel", "error", "-i", webm, "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-pix_fmt", "yuv420p", "-g", "50", "-movflags", "+faststart", mp4]
    p2 = None
    try:
        p2 = await asyncio.create_subprocess_exec(
            *cmd2, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(p2.communicate(), timeout=600)
        if p2.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            try: os.remove(webm)
            except Exception: pass
            return True
        log("ffmpeg(video-only) fail:", (err or b"")[-300:].decode("utf-8", "ignore"))
    except Exception as e:
        log("ffmpeg exc2", repr(e))
        try:
            if p2 and p2.returncode is None:
                p2.kill()
                await p2.wait()
        except Exception:
            pass
        if os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            try: os.remove(webm)
            except Exception: pass
            return True
    return False


async def _enqueue_transcode(webm, mp4, wait=False):
    """把转码任务放入串行队列。wait=True 时等待结果。"""
    fut = _LOOP.create_future() if wait else None
    await _TRANSCODE_Q.put((webm, mp4, fut))
    if wait and fut:
        return await fut
    return None


# ============ 分片接收 ============
async def _ws_handler(ws):
    # 兼容 websockets 新旧 API 取 path
    path = ""
    try:
        if hasattr(ws, "request") and ws.request is not None:
            path = ws.request.path or ""
        elif hasattr(ws, "path"):
            path = ws.path or ""
    except Exception:
        path = ""
    rec_id = path.rstrip("/").split("/")[-1].split("?")[0]
    task = _TASKS.get(rec_id)
    log("ws incoming path=%r rec_id=%r task_found=%s" % (path, rec_id, bool(task)))
    if not task:
        try: await ws.close()
        except Exception: pass
        return
    task["state"] = "recording"
    task["last_data"] = time.time()
    # 前端在 ws.onopen 后开始录, 会先把真实 mime 通过文本消息告知; 这里先读第一条若为 mime 声明
    task["ws_ready"] = True
    log("ws connected rec", rec_id)
    _notify_stats()
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                if task.get("ff") or task.get("fh"):
                    await _ff_write(task, bytes(msg))     # 背压写入，防止内存涨爆
                    task["bytes"] = task.get("bytes", 0) + len(msg)
                    task["seg_bytes"] = task.get("seg_bytes", 0) + len(msg)
                    task["last_data"] = time.time()
                    if task["seg_bytes"] >= C.MAX_SEG_BYTES:
                        await _roll_segment(task)
            else:
                try:
                    j = json.loads(msg)
                    if j.get("__end__"):
                        break
                    if j.get("__mime__"):
                        # 前端实际使用的 mime: 据此决定后续 ffmpeg 是否 copy 及输入格式
                        m = j["__mime__"]
                        task["rec_mime"] = m
                        log("rec mime =", m, "rec", rec_id)
                        # 首帧到达前重建 ffmpeg, 对齐输入格式/copy 策略(此时还没数据写入)
                        if not task.get("_ff_aligned"):
                            task["_ff_aligned"] = True
                            if task.get("bytes", 0) == 0:
                                mp4 = task.get("ff_mp4") or task.get("mp4_path")
                                await _start_ff(task, mp4)   # 用 rec_mime 重新决定参数
                            else:
                                log("首帧已到, 跳过 ffmpeg 重建(保持现参数) rec", rec_id)
                except Exception:
                    pass
    except Exception as e:
        log("ws recv err", rec_id, e)
    finally:
        try: await ws.close()
        except Exception: pass
        log("ws closed rec", rec_id, "bytes", task.get("bytes"))
        # 断连处理: 用户主动 stop -> 已 finalized, 不动;
        # 否则(直播断流/页面崩溃/浏览器崩) -> 先保存当前段 mp4, 再看门狗重启继续监守。
        if not task.get("finalized") and not task.get("restarting"):
            task["_disc_task"] = asyncio.create_task(_on_disconnect(task))


async def _on_disconnect(task):
    """断连收尾: 当前段落存 mp4 (保命), 然后看门狗重启(若未主动停且重试未耗尽)。"""
    rec_id = task.get("rec_id")
    if task.get("finalized") or task.get("restarting"):
        return
    task["restarting"] = True   # 防重入
    user_stop = bool(task.get("user_stopped"))
    await _save_current_segment(task, reason="disconnect")   # 断连保文件
    if user_stop:
        task["state"] = "done"; task["restarting"] = False
        task["finished_at"] = time.time()
        await _close_ctx(task)   # 下播/主动停: 释放 tab
        _notify_stats(); return
    if task.get("retry", 0) < C.WATCHDOG_MAX_RETRY:
        log("断连保文件完成, 看门狗重启继续监守 rec", rec_id)
        task["restarting"] = False
        asyncio.create_task(_watchdog(task))
    else:
        log("断连且达最大重试, 停止监守 rec", rec_id)
        task["state"] = "interrupted"; task["restarting"] = False
        task["finished_at"] = time.time()
        await _close_ctx(task)   # 重试耗尽: 释放 tab
        _notify_stats()


async def _close_ctx(task):
    """关闭浏览器的 context/page（释放 tab 与 renderer 进程），幂等。
    录制结束/下播/重试耗尽后调用，避免空闲 tab 长期占用浏览器进程。"""
    try:
        if task.get("ctx"):
            await asyncio.wait_for(task["ctx"].close(), timeout=10)
    except Exception:
        pass
    try:
        if not task.get("ctx") and task.get("page"):
            await task["page"].close()
    except Exception:
        pass
    task["ctx"] = None
    task["page"] = None


async def _task_reaper():
    """后台清理：已结束任务(done/fail/interrupted/failed)在 _TASKS 保留
    TASK_KEEP_SEC(默认300s) 供前端查看后移除，释放 task 引用/cred/句柄。"""
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            keep = float(os.environ.get("TASK_KEEP_SEC", "300"))
            with _TASKS_LOCK:
                dead = [k for k, t in _TASKS.items()
                        if (t.get("finalized") or t.get("state") in ("done", "fail", "interrupted", "failed"))
                        and (t.get("finished_at") or t.get("started_at") or 0)
                        and now - (t.get("finished_at") or t.get("started_at") or now) > keep]
                for k in dead:
                    _TASKS.pop(k, None)
            if dead:
                log("task reaper 清理结束任务", len(dead))
        except Exception as e:
            log("task_reaper err", repr(e))


async def _idle_browser_reaper():
    """后台清理：无活动任务且空闲超过 IDLE_BROWSER_SEC(默认60s) 时关闭全局
    Chromium，释放浏览器进程/内存。下次 _get_browser 会自动重新 launch。"""
    global _BROWSER, _PW, _LAST_ACTIVE
    while True:
        await asyncio.sleep(20)
        try:
            if _BROWSER is None:
                continue
            with _TASKS_LOCK:
                active = any(t.get("state") in ("starting", "recording", "restarting", "stopping", "saved")
                             and not t.get("finalized") for t in _TASKS.values())
            idle_sec = float(os.environ.get("IDLE_BROWSER_SEC", "60"))
            if not active and time.time() - _LAST_ACTIVE > idle_sec:
                log("浏览器空闲超过 %.0fs, 关闭全局 Chromium 释放资源" % idle_sec)
                try:
                    await _BROWSER.close()
                except Exception:
                    pass
                try:
                    if _PW is not None:
                        await _PW.stop()
                except Exception:
                    pass
                _BROWSER = None
                _PW = None
        except Exception as e:
            log("idle_browser_reaper err", repr(e))


async def _save_current_segment(task, reason="disconnect"):
    """关闭当前段 ffmpeg(收尾写 moov), mp4 已实时存在。不销毁任务, 供重启用。"""
    mp4 = task.get("ff_mp4") or task.get("mp4_path")
    ok = await _stop_ff(task, finalize=True)
    if ok:
        task.setdefault("segments", []).append(mp4)
        task["final_mp4"] = mp4
    else:
        log("断连段 ffmpeg 收尾无效", mp4)
    task["state"] = "saved"
    task["_seg_saved"] = True   # 标记本段已保存, 防看门狗重复处理


async def _roll_segment(task):
    """段达 2GB: 收尾当前段 ffmpeg -> 起新段 ffmpeg 继续。"""
    seg = task.get("seg", 0)
    old_mp4 = task.get("ff_mp4") or task["mp4_path"]
    await _stop_ff(task, finalize=True)
    if old_mp4 and os.path.exists(old_mp4):
        task.setdefault("segments", []).append(old_mp4)
    task["seg"] = seg + 1
    base_mp4 = task["base_mp4"]
    new_mp4 = base_mp4.replace(".mp4", "_part%d.mp4" % task["seg"])
    task["mp4_path"] = new_mp4
    task["seg_bytes"] = 0
    await _start_ff(task, new_mp4)          # 起新段 ffmpeg
    log("rolled to segment", task["seg"])
    _notify_stats()


async def _start_ws_server():
    global _WS_SERVER, _WS_PORT
    if not HAVE_WS:
        log("!! websockets 未装"); return
    _WS_SERVER = await websockets.serve(_ws_handler, "127.0.0.1", C.REC_PORT)
    _WS_PORT = C.REC_PORT
    log("录制分片 WS on 127.0.0.1:%d" % _WS_PORT)


# ============ 开始/停止/看门狗 ============
async def _open_page(task):
    """打开 recorder.html 并等进 recording。返回是否成功。
    单 Chromium 多 Tab: 每房一个独立 BrowserContext(隔离 storage/cookie), 内建 page。"""
    browser = await _get_browser()
    # 销毁旧 context(重启场景) 再建新的, 防泄漏
    old_ctx = task.get("ctx")
    if old_ctx:
        try: await old_ctx.close()
        except Exception: pass
    ctx = await browser.new_context(
        viewport={"width": 720, "height": 1280},
        bypass_csp=True,
        ignore_https_errors=True,
    )
    task["ctx"] = ctx
    page = await ctx.new_page()
    task["page"] = page
    _LAST_ACTIVE = time.time()
    # 新页面 = 新 WS = 会重发 __mime__, 重置对齐标志让 ffmpeg 重新按真实 mime 参数启动
    task["_ff_aligned"] = False
    task["rec_mime"] = None
    cred = task["cred"]
    rec_cfg = {
        "appid": cred["appid"], "channel": cred["channel"],
        "token": cred["token"], "uid": int(cred.get("uid") or 0),
        "wsUrl": "ws://127.0.0.1:%d/recv/%s" % (_WS_PORT, task["rec_id"]),
        "bitrate": C.VIDEO_BITRATE, "audioBitrate": C.AUDIO_BITRATE,
        "fps": C.CAPTURE_FPS, "mimeCands": C.REC_MIME_CANDS,
    }
    await page.add_init_script("window.__REC__ = %s;" % json.dumps(rec_cfg))
    try:
        await page.goto("http://127.0.0.1:%d/recorder.html" % C.PORT,
                        wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        log("page goto err", e)
    t0 = time.time()
    while time.time() - t0 < 25:
        try:
            stt = await page.evaluate("() => (window.__STATE__||{}).state")
        except Exception:
            stt = None
        if stt:
            task["state"] = stt
        if stt == "recording":
            return True
        if stt and str(stt).startswith("join-fail"):
            log("join fail rec", task["rec_id"], stt)
            return False
        await asyncio.sleep(0.5)
    return False


async def start_record(room_id, plat_id, cred, name=None):
    if not _WS_PORT:
        return {"ok": False, "msg": "录制分片WS未就绪"}
    # 同一房间幂等启动：手动点击、看门狗、重复 HTTP 请求都复用活动任务。
    active_states = ("starting", "recording", "restarting", "stopping", "saved")
    with _TASKS_LOCK:
        active = [t for t in _TASKS.values() if t.get("state") in active_states and not t.get("finalized")]
        for t in active:
            if str(t.get("room_id")) == str(room_id):
                return {"ok": True, "rec_id": t["rec_id"], "state": t.get("state"),
                        "mp4": t.get("mp4_path"), "name": t.get("name"), "existing": True}
        if len(active) >= C.MAX_CONCURRENT:
            return {"ok": False, "msg": "录制并发已达上限(%d)，请稍后重试" % C.MAX_CONCURRENT}
    # ---- 直录模式判定: cred 带 mode='direct' + play_url 时, 不开浏览器,
    # 直接 ffmpeg 拉源流(FLV/HLS)转封装。适用番茄/杏仁/青瑶/草莓/小妲己等直出平台。----
    is_direct = str((cred or {}).get("mode") or "") == "direct" and bool((cred or {}).get("play_url"))
    # web 模式但无 Agora 凭据(appid/token 缺失, 如番茄/小妲己的加密 HLS) → Web 录制页无法 join,
    # 明确拒绝而不是静默失败(之前会启动页面后报 Invalid token)。
    if not is_direct and not (cred or {}).get("appid"):
        return {"ok": False, "msg": "该平台暂无录制通道(加密 HLS 通道未接入)", "engine": None}
    rec_id = uuid.uuid4().hex[:12]
    # 三级目录: recordings/<平台名ID>/<主播名_房间号>/<文件>  (平台层便于按平台浏览)
    import re as _re
    import capturers as _cap
    _pid = int(plat_id)
    _plat_name = ""
    try:
        _plat_name = _cap.plat_name(_pid) or ""
    except Exception:
        _plat_name = ""
    _plat_dir = _re.sub(r'[\\/:*?"<>|\s]+', "_", str(_plat_name or ("plat%d" % _pid))).strip("_")
    if not _plat_dir:
        _plat_dir = "plat%d" % _pid
    anchor = _re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("_") or str(room_id)
    anchor = "%s_%s" % (anchor, room_id) if name else anchor
    out_dir = os.path.join(C.REC_DIR, *_plat_dir.split("/"), anchor) if _plat_dir else os.path.join(C.REC_DIR, anchor)
    os.makedirs(out_dir, exist_ok=True)
    # 文件名: <主播名>_<YYYYMMDD-HHMMSS> (无主播名则用 ROOM_<roomId>)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name_tag = _re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("_")
    fname = "%s_%s" % (name_tag or ("ROOM_%s" % room_id), stamp)
    webm_path = os.path.join(out_dir, fname + ".webm")
    mp4_path = os.path.join(out_dir, fname + ".mp4")
    task = {
        "rec_id": rec_id, "room_id": room_id, "plat_id": plat_id, "name": name,
        "folder": anchor, "plat_dir": _plat_dir, "cred": cred, "base_webm": webm_path, "base_mp4": mp4_path,
        "webm_path": webm_path, "mp4_path": mp4_path, "fh": None,
        "ff": None, "ff_mp4": mp4_path,
        "seg": 0, "seg_bytes": 0, "parts": [], "bytes": 0,
        "started_at": time.time(), "state": "starting", "page": None,
        "retry": 0, "finalized": False, "restarting": False,
        "mode": "direct" if is_direct else "web",
        "play_url": (cred or {}).get("play_url") or "",
        "engine": "direct_ffmpeg" if is_direct else "web_chromium",
    }
    with _TASKS_LOCK:
        _TASKS[rec_id] = task
    if is_direct:
        # HLS 源(番茄 inter_encrypt 等): 上游 m3u8 的 key.key 是自定义密文,
        # ffmpeg 直拉会解密失败(rc=183)。改走本机 /api/stream 代理 — 代理负责
        # key 解密(返回 16B 明文) + 显式 IV=0x0 重写 + ts 段同源透传。
        pu = task["play_url"]
        if pu.lower().endswith(".m3u8"):
            import urllib.parse as _up
            task["play_url"] = "http://127.0.0.1:%d/api/stream?url=%s" % (
                C.PORT, _up.quote(pu, safe=""))
            log("direct HLS via proxy ->", task["play_url"][:96])
        # 直录: 不需要 WS/页面, ffmpeg 常驻直拉源流; 看门狗负责断流重启。
        task["last_data"] = time.time()
        await _start_ff(task, mp4_path)
        if task.get("ff") and task["ff"].returncode is None:
            task["state"] = "recording"
            asyncio.create_task(_direct_monitor(task))
            log("direct record start ->", mp4_path, "src", task["play_url"][:80])
        else:
            task["state"] = "failed"
            log("direct ffmpeg 启动失败", task.get("play_url", "")[:80])
        _notify_stats()
        return {"ok": task["state"] == "recording", "rec_id": rec_id,
                "state": task["state"], "mp4": mp4_path, "name": name,
                "engine": "direct_ffmpeg"}
    # 先起一个占位 ffmpeg(webm/x264) 防止页面过早发数据时无进程;
    # 收到前端 __mime__ 声明后, 若 mime 变化会在首帧前重建为正确参数(copy/x264+格式)
    await _start_ff(task, mp4_path)
    ok = await _open_page(task)
    if ok:
        task["state"] = "recording"
    else:
        # 首次打开失败: 交给看门狗重启
        asyncio.create_task(_watchdog(task))
    _notify_stats()
    return {"ok": True, "rec_id": rec_id, "state": task["state"], "mp4": mp4_path,
            "name": name, "engine": "web_chromium"}


async def _direct_monitor(task):
    """直录守护: ffmpeg 存活监控 + 2GB切段 + 断流看门狗重启。严格单实例。"""
    if task.get("finalized") or task.get("user_stopped") or task.get("_dm_running"):
        return
    task["_dm_running"] = True
    try:
        while not task.get("finalized") and not task.get("user_stopped"):
            await asyncio.sleep(5)
            proc = task.get("ff")
            if proc is None or proc.returncode is not None:
                # ffmpeg 退出: 断流/源失效 -> 收尾当前段并看门狗重启
                log("direct ffmpeg exited rc=", getattr(proc, "returncode", None),
                    "rec", task.get("rec_id"))
                if not task.get("_seg_saved"):
                    await _save_current_segment(task, reason="direct_exit")
                task["restarting"] = True
                try:
                    await _watchdog(task)
                finally:
                    task["restarting"] = False
                return
            task["last_data"] = time.time()
    finally:
        task["_dm_running"] = False


async def _watchdog(task):
    """看门狗: 录制页异常/断连时自动重启录制；同一任务严格单实例。"""
    if task.get("finalized") or task.get("user_stopped") or task.get("_watchdog_running"):
        return
    if task["retry"] >= C.WATCHDOG_MAX_RETRY:
        log("watchdog 达到最大重试, 放弃 rec", task["rec_id"])
        task["state"] = "failed"
        task["finished_at"] = time.time()
        await _close_ctx(task)   # 重试耗尽: 释放 tab, 不再重启
        _notify_stats()
        return
    task["_watchdog_running"] = True
    try:
        task["retry"] += 1
        task["restarting"] = True
        log("watchdog 重启录制 rec=%s retry=%d" % (task["rec_id"], task["retry"]))
        # 关闭旧 context(含 page) + 收尾当前段(作为 part 保留)
        await _close_ctx(task)
        if task.get("finalized") or task.get("user_stopped"):
            return
        seg = task.get("seg", 0)
        if not task.get("_seg_saved"):
            old_mp4 = task.get("ff_mp4") or task["mp4_path"]
            ok = await _stop_ff(task, finalize=True)
            if ok:
                task.setdefault("segments", []).append(old_mp4)
        else:
            await _stop_ff(task, finalize=False)
        if task.get("finalized") or task.get("user_stopped"):
            return
        task["_seg_saved"] = False
        task["seg"] = seg + 1
        r = task["retry"]
        new_mp4 = task["base_mp4"].replace(".mp4", "_r%d.mp4" % r)
        task["mp4_path"] = new_mp4
        task["seg_bytes"] = 0
        await _start_ff(task, new_mp4)
        task["restarting"] = False
        task["state"] = "restarting"
        _notify_stats()
        await asyncio.sleep(C.WATCHDOG_RETRY_INTERVAL)
        # 睡眠期间用户可能已停止；绝不能把已关闭任务重新打开。
        if task.get("finalized") or task.get("user_stopped"):
            return
        if task.get("mode") == "direct":
            # 直录: 无页面可开, ffmpeg 已在 _start_ff 起新段; 失败则继续看门狗链
            if task.get("ff") and task["ff"].returncode is None:
                task["state"] = "recording"
                asyncio.create_task(_direct_monitor(task))
            else:
                asyncio.create_task(_watchdog(task))
            _notify_stats()
            return
        ok = await _open_page(task)
        if ok:
            task["state"] = "recording"
        else:
            asyncio.create_task(_watchdog(task))
        _notify_stats()
    finally:
        task["_watchdog_running"] = False
        if task.get("finalized") or task.get("user_stopped"):
            task["restarting"] = False


async def _faststart_mp4(mp4):
    """把 mp4 原地重封装为 moov 前置(faststart): 播放器秒开, 不用等整文件扫完。

    fragmented/webm容器混装等一律 `-c copy` 纯流拷贝(零重编码, 画质不变),
    只重写容器头。大文件 IO 走异步子进程不阻塞事件循环。
    返回 True=已重封装, False=失败(调用方保留原件兜底, 不阻断归档)。
    """
    if not mp4 or not os.path.exists(mp4) or os.path.getsize(mp4) < 4096:
        return False
    tmp = mp4 + ".fs.tmp.mp4"
    try:
        p = await asyncio.create_subprocess_exec(
            C.FFMPEG, "-y", "-loglevel", "error", "-i", mp4,
            "-c", "copy", "-movflags", "+faststart", tmp,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(p.communicate(), timeout=1800)
        if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) <= 0:
            log("faststart 失败, 保留原件", mp4, (err or b"")[-200:])
            try: os.remove(tmp)
            except Exception: pass
            return False
        os.replace(tmp, mp4)
        return True
    except Exception as e:
        log("faststart 异常, 保留原件", mp4, repr(e))
        try: os.remove(tmp)
        except Exception: pass
        return False


async def _archive_final(task):
    """web 录制完成后: 先把成品段 faststart 重封装(moov 前置秒开),
    再**移动**进 FINAL_REC_DIR 最终归档(机械盘)。

    目录结构对齐项目规范: {FINAL_REC_DIR}/{plat_dir}/{folder}/{原名}。
    移动即删除暂存原件, 文件只保留最终一份。失败只记日志不阻断。
    """
    segs = task.get("segments") or []
    if not segs:
        return
    plat_dir = task.get("plat_dir") or "plat%s" % task.get("plat_id", "0")
    folder = task.get("folder") or str(task.get("room_id", "room"))
    fdir = os.path.join(C.FINAL_REC_DIR, plat_dir, folder)
    try:
        os.makedirs(fdir, exist_ok=True)
    except Exception as e:
        log("归档目录创建失败", fdir, repr(e)); return
    for mp4 in segs:
        if not mp4 or not os.path.exists(mp4) or os.path.getsize(mp4) <= 0:
            continue
        try:
            # 先转 faststart(moov 前置), 失败保留 fragmented 原件
            await _faststart_mp4(mp4)
            dst = os.path.join(fdir, os.path.basename(mp4))
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(mp4, dst)
            # 移动成功后把成品指针更新到新位置, 状态接口返回有效路径
            if task.get("final_mp4") == mp4:
                task["final_mp4"] = dst
            if task.get("mp4_path") == mp4:
                task["mp4_path"] = dst
            segs = task.get("segments") or []
            if mp4 in segs:
                segs[segs.index(mp4)] = dst
                task["segments"] = segs
            log("已移动归档最终目录(删除暂存)", dst)
        except Exception as e:
            log("归档移动失败(保留暂存)", mp4, repr(e))


async def _finalize(task, reason="stop"):
    if task.get("finalized"):
        return {"ok": True, "rec_id": task.get("rec_id"), "mp4": task.get("final_mp4"),
                "state": task.get("state"), "segments": task.get("segments", [])}
    task["finalized"] = True
    mp4 = task.get("ff_mp4") or task.get("mp4_path")
    prev_segments = list(task.get("segments", []))   # 之前已保存的段(断连/重启累积)
    mp4_ok = False
    # 1) 断连回调已收尾保存过本段
    if task.get("_seg_saved") and mp4 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
        mp4_ok = True
        await _stop_ff(task, finalize=False)
    # 2) 正常停止: 关 ffmpeg stdin 实时收尾(秒级)
    elif task.get("ff"):
        mp4_ok = await _stop_ff(task, finalize=True)
    # 3) 兜底: 文件已是有效就认
    if not mp4_ok and mp4 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
        mp4_ok = True
    # 汇总所有成品段: 之前已保存的 + 当前段(去重保序)
    all_segs = list(prev_segments)
    if mp4_ok and mp4 and mp4 not in all_segs:
        all_segs.append(mp4)
    task["state"] = "done" if (mp4_ok or all_segs) else ("interrupted" if reason != "stop" else "fail")
    task["finished_at"] = time.time()
    task["final_mp4"] = mp4 if mp4_ok else (all_segs[-1] if all_segs else None)
    task["segments"] = all_segs
    # 录制收尾后把成品归档到最终目录(机械盘), 失败不阻断
    if task["state"] == "done":
        try:
            await _archive_final(task)
        except Exception as e:
            log("归档异常", repr(e))
    # 释放浏览器 tab/context(web 任务), 直录无 ctx 幂等
    if task.get("mode") != "direct":
        await _close_ctx(task)
    task["finished_at"] = time.time()
    log("finalize rec", task.get("rec_id"), "reason", reason, "mp4_ok", mp4_ok,
        "segments", len(task["segments"]))
    _notify_stats()
    _LAST_ACTIVE = time.time()   # 刚释放页面, 立即允许空闲关闭浏览器
    return {"ok": True, "rec_id": task.get("rec_id"), "mp4": task["final_mp4"],
            "state": task["state"], "segments": task.get("segments", [])}


async def _finalize_async(task, reason="disconnect"):
    try:
        await _finalize(task, reason)
    except Exception as e:
        log("finalize_async err", e)


async def stop_record(rec_id):
    with _TASKS_LOCK:
        task = _TASKS.get(rec_id)
    if not task:
        return {"ok": False, "msg": "无此录制任务"}
    task["user_stopped"] = True   # 主动停止: 断连回调不再触发看门狗重启
    task["state"] = "stopping"
    _notify_stats()
    if task.get("mode") == "direct":
        # 直录: 杀 ffmpeg 进程即可(stdin 是 DEVNULL, 关 stdin 收尾由 _stop_ff 处理)
        proc = task.get("ff")
        if proc and proc.returncode is None:
            try: proc.terminate()
            except Exception: pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try: proc.kill(); await proc.wait()
                except Exception: pass
        return await _finalize(task, reason="stop")
    page = task.get("page")
    try:
        if page:
            await page.evaluate("() => window.__STOP__ ? window.__STOP__() : null")
    except Exception as e:
        log("stop eval err", e)
    await asyncio.sleep(1.2)
    await _close_ctx(task)   # 统一关闭(幂等; _finalize 里也会再关一次, 无碍)
    # 等待断连回调(若有)完成当前段保存, 避免与其并发重复转码/竞态
    dt = task.get("_disc_task")
    if dt:
        try:
            await asyncio.wait_for(dt, timeout=65)
        except Exception as e:
            log("await disc_task err", repr(e))
    return await _finalize(task, reason="stop")


# ============ 同步桥接 (给 server.py) ============
def _sync_status():
    out = []
    now = time.time()
    # HTTP 线程也会读；与事件循环中的任务注册互斥，避免字典遍历时变更。
    with _TASKS_LOCK:
        items = list(_TASKS.items())
    for k, t in items:
        state = t.get("state")
        # 健壮性: stop 链路偶发卡死(ctx.close 等)时, stopping 超 90s 且已 user_stopped
        # 直接按已收尾处理, 避免状态永久卡在 stopping(占用并发额度)。
        if (state == "stopping" and t.get("user_stopped")
                and now - t.get("started_at", now) > 90):
            t["state"] = "done" if (t.get("final_mp4") or t.get("segments")) else "fail"
            state = t["state"]
        out.append({
            "rec_id": k, "room_id": t.get("room_id"), "plat_id": t.get("plat_id"),
            "name": t.get("name"), "state": t.get("state"), "bytes": t.get("bytes", 0),
            "seg": t.get("seg", 0), "seg_bytes": t.get("seg_bytes", 0),
            "mp4": t.get("final_mp4") if t.get("state") in ("done",) else None,
            "segments": t.get("segments", []), "started_at": t.get("started_at"),
            "retry": t.get("retry", 0),
            "mode": t.get("mode"), "engine": t.get("engine"),
        })
        # 直录模式: bytes 不随 ws 分片累加(恒 0), 改按磁盘上本次启动产生的文件统计
        if t.get("mode") == "direct" and t.get("state") in ("recording", "starting", "restarting"):
            sess = 0
            for p in (t.get("segments") or []):
                try:
                    if os.path.exists(p):
                        sess += os.path.getsize(p)
                except Exception:
                    pass
            cur = t.get("ff_mp4") or t.get("mp4_path")
            try:
                if cur and os.path.exists(cur):
                    sess += os.path.getsize(cur)
            except Exception:
                pass
            out[-1]["bytes"] = sess
    return out


_DB_FLUSH_STARTED = False


def _db_flush_loop():
    """每秒把当前全部任务 upsert 进 sqlite rec_tasks 表; 前端/重启不丢。"""
    while True:
        try:
            SDB.upsert_rec_tasks(_sync_status())
        except Exception as e:
            log("db flush err", repr(e))
        time.sleep(1)


def _ensure_db_flush():
    global _DB_FLUSH_STARTED
    if _DB_FLUSH_STARTED:
        return
    _DB_FLUSH_STARTED = True
    threading.Thread(target=_db_flush_loop, daemon=True, name="rec-db-flush").start()


def _ensure_loop():
    global _LOOP, _TRANSCODE_Q, _TRANSCODE_WORKER
    if _LOOP is not None:
        return _LOOP
    with _LOOP_INIT_LOCK:
        if _LOOP is not None:
            return _LOOP
        loop = asyncio.new_event_loop()
        _LOOP = loop

        def run():
            global _TRANSCODE_Q
            asyncio.set_event_loop(loop)
            _TRANSCODE_Q = asyncio.Queue()
            loop.run_until_complete(_start_ws_server())
            loop.create_task(_transcode_worker())
            loop.create_task(_task_reaper())
            loop.create_task(_idle_browser_reaper())
            loop.run_forever()
        threading.Thread(target=run, daemon=True, name="recorder-loop").start()
        for _ in range(50):
            if _WS_PORT: break
            time.sleep(0.1)
        _ensure_db_flush()
    return _LOOP


def run_async(coro):
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=180)


def api_start(room_id, plat_id, cred, name=None):
    return run_async(start_record(room_id, plat_id, cred, name))


def api_stop(rec_id):
    return run_async(stop_record(rec_id))


def api_restart(rec_id):
    """任务内重启(流异常自愈): 不建新任务, 看门狗滚动新段。
    与 ffmpeg 断流/断连回调同机制, 录制身份(rec_id/段列表)保持不变。"""
    return run_async(_force_restart(rec_id))


async def _force_restart(rec_id):
    with _TASKS_LOCK:
        task = _TASKS.get(rec_id)
    if not task:
        return {"ok": False, "msg": "无此录制任务"}
    if task.get("finalized") or task.get("user_stopped"):
        return {"ok": False, "msg": "任务已结束"}
    if not task.get("_watchdog_running"):
        task["restarting"] = True
        asyncio.create_task(_watchdog(task))
    return {"ok": True, "rec_id": rec_id, "state": task.get("state")}


def api_status():
    return _sync_status()


def ws_port():
    _ensure_loop()
    return _WS_PORT


if __name__ == "__main__":
    _ensure_loop()
    print("recorder engine ws_port=", ws_port())
    while True:
        time.sleep(1)
