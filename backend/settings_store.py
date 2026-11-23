#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings_store.py — 设置持久化(代理开关/运行模式/录制模式)。

- 持久化到 backend/settings.json, 重启后保留;
- 代理开关只影响外网 HTTP 抓取(imgproxy 封面/图床), 不影响:
    · 签权隧道 pure_kiwi_ws(原生 UDP socket 直连 162.14.199.148:9875, 走不了代理)
    · 业务 API client.py(显式 proxies=None 直连 103.44.236.131:8001)
    · Agora 拉流(浏览器 WebRTC UDP, 不经 HTTP 代理)
"""
import os, json, threading

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "settings.json")

_LOCK = threading.Lock()
_DEFAULTS = {
    "proxy_enabled": False,
    "proxy_url": "http://127.0.0.1:7897",
    # 运行模式
    "single_browser": True,     # True=单 Chromium 多 Tab(省资源) / False=每房独立浏览器
    "max_concurrent": 12,       # 录制并发上限
    # 录制模式
    "transcode_fast": True,     # True=h264 直出零重编 / False=强制 libx264 实时转码(兼容)
    "video_bitrate": 10000000,  # 采集码率上限(最大档, 不分档; 上游给多大就按多大)
    "capture_fps": 30,          # 采集帧率上限(最大档)
    "quality": "max",           # 画质档位: 固定 max(不分档, 按上游最大分辨率/码率/帧率)
    # Agora 原生录制服务(同机部署): 项目按此 IP:端口 连 agora 录制服务
    "agora_enabled": False,     # 是否启用原生 agora 录制(密语等标准 Agora 平台)
    "agora_host": "192.168.2.38",
    "agora_port": 8080,
}
# 画质档位(唯一 max): 录制=最大分辨率/码率/帧率, 不压缩
QUALITY_PRESETS = {
    "max": {"video_bitrate": 10000000, "capture_fps": 30},
}
_CACHE = None


def load():
    """读设置(带缓存), 缺省项补齐默认值。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return dict(_CACHE)
        data = dict(_DEFAULTS)
        try:
            with open(PATH, "r", encoding="utf-8") as f:
                for k, v in (json.load(f) or {}).items():
                    if k in _DEFAULTS:
                        data[k] = v
        except FileNotFoundError:
            pass
        except Exception as e:
            print("[SETTINGS] 读取失败, 用默认值: %s" % e)
        _CACHE = data
        return dict(_CACHE)


def save(**updates):
    """更新若干字段并落盘(原子写), 返回最新全量。"""
    global _CACHE
    with _LOCK:
        data = dict(_CACHE if _CACHE is not None else load())
        for k, v in updates.items():
            if k in _DEFAULTS:
                data[k] = v
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PATH)
        _CACHE = data
        return dict(_CACHE)


def proxy_dict():
    """requests 用的 proxies 字典(关闭时显式禁用, 防吃系统代理)。"""
    s = load()
    if s.get("proxy_enabled") and s.get("proxy_url"):
        return {"http": s["proxy_url"], "https": s["proxy_url"]}
    return {"http": None, "https": None}


def apply_runtime():
    """把设置写进运行中的 config 模块(新任务生效)。"""
    s = load()
    try:
        import config as C
        C.SINGLE_BROWSER = bool(s.get("single_browser"))
        C.MAX_CONCURRENT = int(s.get("max_concurrent"))
        C.TRANSCODE_FAST = bool(s.get("transcode_fast"))
        C.VIDEO_BITRATE = int(s.get("video_bitrate"))
        C.CAPTURE_FPS = int(s.get("capture_fps"))
    except Exception as e:
        print("[SETTINGS] 应用到 config 失败: %s" % e)


def modes_dict():
    s = load()
    return {
        "single_browser": bool(s.get("single_browser")),
        "max_concurrent": int(s.get("max_concurrent")),
        "transcode_fast": bool(s.get("transcode_fast")),
        "video_bitrate": int(s.get("video_bitrate")),
        "capture_fps": int(s.get("capture_fps")),
        "quality": s.get("quality") or "high",
    }


def set_quality(q):
    """画质档位: 固定 max(不分档), 映射最大码率+帧率并持久化。q = max。"""
    q = str(q or "").lower()
    if q not in QUALITY_PRESETS:
        raise ValueError("未知画质档位: %s" % q)
    preset = QUALITY_PRESETS[q]
    save(video_bitrate=preset["video_bitrate"], capture_fps=preset["capture_fps"], quality=q)
    apply_runtime()
    return modes_dict()


def proxy_info():
    s = load()
    return {
        "proxy_enabled": bool(s.get("proxy_enabled")),
        "proxy_url": s.get("proxy_url") or "",
    }


def agora_info():
    """Agora 原生录制服务配置(同机 NAS 部署, 项目按此连接; 输出目录由 compose 挂载, 不在页面配)。"""
    s = load()
    host = s.get("agora_host") or "192.168.2.38"
    port = int(s.get("agora_port") or 8080)
    return {
        "enabled": bool(s.get("agora_enabled")),
        "host": host,
        "port": port,
        "base_url": "http://%s:%d" % (host, port),
    }
