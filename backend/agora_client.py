#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agora_client.py — Agora 原生录制服务客户端(同机 NAS 部署)。

只做三件事:
  - health()            健康检查 GET /api/recording/
  - start_by_room(...)  调 POST /api/recording/startByRoom 开录, 返回 taskId/pid
  - stop(task_id)       GET /api/recording/stop?taskId=

服务地址从 settings_store.agora_info() 取(页面配的 host/port), 也可显式传。
输出文件落在服务端平铺目录(容器内 /tmp/recording/, 宿主机映射到 AGORA_REC_DIR)。
本模块不关心文件搬移, 由上层调度 watch 目录。
"""
import json
import urllib.request
import urllib.parse

import settings_store as SS


def _base(host=None, port=None):
    if host and port:
        return "http://%s:%d" % (host, int(port))
    info = SS.agora_info()
    return info["base_url"]


def _req(url, method="GET", body=None, timeout=8):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw


def enabled():
    return SS.agora_info().get("enabled", False)


def health(host=None, port=None, timeout=4):
    """健康检查: GET /api/recording/ 返回 200 即服务在。"""
    try:
        st, _ = _req(_base(host, port) + "/api/recording/", timeout=timeout)
        return st == 200
    except Exception as e:
        return False


def start_by_room(appid, channel, token, uid, duration, host=None, port=None, timeout=12):
    """调原生录制服务开录。返回 {ok, task_id, pid, msg}。

    duration 秒到点服务端自动 OVER_MAX_DURATION 停止并落盘。
    """
    url = _base(host, port) + "/api/recording/startByRoom"
    body = {"appid": appid, "channel": channel, "token": token,
            "uid": str(uid), "duration": int(duration)}
    try:
        st, js = _req(url, method="POST", body=body, timeout=timeout)
    except Exception as e:
        return {"ok": False, "msg": "连接录制服务失败: %r" % (e,)}
    if not isinstance(js, dict):
        return {"ok": False, "msg": "响应非JSON: %r" % (js,)}
    if js.get("code") == 0:
        return {"ok": True, "task_id": js.get("taskId"), "pid": js.get("pid"), "msg": "recording initiated"}
    # code=1 channel already recording 也当作已开(幂等)
    if js.get("code") == 1:
        return {"ok": True, "task_id": None, "pid": None, "msg": js.get("msg"), "already": True}
    return {"ok": False, "msg": js.get("msg") or js}


def stop(task_id, host=None, port=None, timeout=8):
    url = _base(host, port) + "/api/recording/stop?" + urllib.parse.urlencode({"taskId": task_id})
    try:
        st, js = _req(url, timeout=timeout)
        return {"ok": st == 200, "data": js}
    except Exception as e:
        return {"ok": False, "msg": repr(e)}
