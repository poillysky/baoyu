#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端实时推送中心 (hub.py) — WebSocket 广播 (抄 frigate 的 topic/payload 模式)
- 后端录制状态变化 -> 广播给所有前端页面
- 前端连 ws://127.0.0.1:WS_PORT/live, 收到 {topic,payload}
- 新连接时立即推一次全量快照 {"topic":"snapshot", ...}
纯 stdlib asyncio + websockets (已有依赖)。独立线程跑自己的 event loop。
"""
import os, sys, json, asyncio, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C

try:
    import websockets
    HAVE_WS = True
except Exception:
    HAVE_WS = False

_CLIENTS = set()          # 已连接的前端 ws
_LOOP = None
_READY = threading.Event()
_SNAPSHOT_FN = None       # 由 server 注入: 返回当前全量状态 dict


def set_snapshot_provider(fn):
    global _SNAPSHOT_FN
    _SNAPSHOT_FN = fn


async def _handler(ws):
    # websockets 新版用 ws.request.path, 旧版 ws.path; 不区分路径统一接管
    _CLIENTS.add(ws)
    try:
        # 新连接: 先推全量快照
        if _SNAPSHOT_FN:
            try:
                snap = _SNAPSHOT_FN()
                await asyncio.wait_for(ws.send(json.dumps({"topic": "snapshot", "payload": snap}, ensure_ascii=False)), timeout=5)
            except Exception:
                pass
        async for _ in ws:
            pass  # 前端一般不发送, 只是保活
    except Exception:
        pass
    finally:
        _CLIENTS.discard(ws)


async def _broadcast(msg):
    if not _CLIENTS:
        return
    data = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(_CLIENTS):
        try:
            # 单连接发送加超时：某个前端卡死/假死不能拖垮整个推送循环，
            # 否则其余所有前端都会跟着收不到状态更新。
            await asyncio.wait_for(ws.send(data), timeout=5)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _CLIENTS.discard(ws)


def broadcast(topic, payload):
    """线程安全地从任意线程广播。"""
    if _LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast({"topic": topic, "payload": payload}), _LOOP)
    except Exception:
        pass


async def _serve():
    await websockets.serve(_handler, "127.0.0.1", C.WS_PORT)
    _READY.set()


def start():
    global _LOOP
    if not HAVE_WS:
        print("[HUB] !! websockets 未安装, 实时推送不可用", flush=True)
        return
    if _LOOP is not None:
        return

    def run():
        global _LOOP
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
        _LOOP.run_until_complete(_serve())
        _LOOP.run_forever()
    threading.Thread(target=run, daemon=True).start()
    _READY.wait(timeout=5)
    print("[HUB] 实时推送 WebSocket on 127.0.0.1:%d" % C.WS_PORT, flush=True)
