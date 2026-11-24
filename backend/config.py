#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集中配置 (config.py) — 所有常量/路径/密钥一处维护, 其它模块 import 本文件。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 业务服务端 (鲍鱼盒子 AES 加密 API) ----
KEY = b"433204914B1B8C53EAA43267A3AEE87D"
IV = KEY[:16]
APPKEY = "kk_baoyu"
HOST = "https://103.44.236.131:8001"
AGORA_APPID_REAL = "84437b2ceb75422ab1f7df9c2689241d"

# ---- 本地服务 ----
PORT = int(os.environ.get("PORT", "8900"))          # 后端 HTTP 端口
WS_PORT = int(os.environ.get("WS_PORT", "8902"))    # 前端实时推送 WebSocket 端口
REC_PORT = int(os.environ.get("REC_PORT", "8903"))  # 录制分片接收 WebSocket 端口

# ---- 设备/工具 ----
ADB = os.environ.get("ADB", "/Users/poilly/Library/Android/sdk/platform-tools/adb")
PKG = "me.hq80tw8.ws04i1m"
FLUTTER_PREFS = "/data/data/%s/shared_prefs/FlutterSharedPreferences.xml" % PKG

# 优先环境变量 CHROMIUM; 未指定则跨平台自动探测(完整 Chrome 优先, 含 H.264 专利解码,
# 开源 Chromium 无 H.264 会导致 Agora h264 流在容器内无法解码 → canvas 空 → 0 分片):
#  - Linux(Docker): $PLAYWRIGHT_BROWSERS_PATH(默认 /ms-playwright) 下:
#       chrome-linux*/chrome (完整 Chrome, 优先)  →  chromium-*/chrome-linux*/chrome (开源, 无H264)
#  - macOS: ~/Library/Caches/ms-playwright 下 chrome-mac* 优先, 其次 chromium-*
CHROMIUM = os.environ.get("CHROMIUM", "")
if not CHROMIUM:
    import glob as _glob
    import shutil as _sh
    # 0) 系统完整 Chrome(含 H.264 专利解码; Debian/Ubuntu 上 playwright install chrome 装的)
    for _c in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        _p = _sh.which(_c)
        if _p:
            CHROMIUM = _p
            break
if not CHROMIUM:
    import glob as _glob
    _BP = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")
    _cands = sorted(_glob.glob(os.path.join(_BP, "chrome-linux*/chrome")))          # 完整 Chrome(含H264)
    _cands += sorted(_glob.glob(os.path.join(_BP, "chromium-*/chrome-linux*/chrome")))  # 开源 Chromium
    _cands += sorted(_glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/**/Google Chrome for Testing"),
        recursive=True))
    _cands += sorted(_glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium")))
    CHROMIUM = _cands[-1] if _cands else ""
    if not CHROMIUM:
        # 兜底: 若未找到, 交给 playwright 默认(自动下载目录), 不写死 Mac 路径
        CHROMIUM = None
FFMPEG = os.environ.get("FFMPEG", "")
if not FFMPEG:
    import shutil as _sh
    FFMPEG = _sh.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
# ffprobe 通常与 ffmpeg 同目录
FFPROBE = os.environ.get("FFPROBE", "")
if not FFPROBE:
    import shutil as _sh2
    FFPROBE = _sh2.which("ffprobe") or os.path.join(os.path.dirname(FFMPEG), "ffprobe")
# ffmpeg 视频编码器: mac 用 videotoolbox 硬编; linux 用 h264 软编/硬编按可用性
FFMPEG_VENC = os.environ.get("FFMPEG_VENC", "h264_videotoolbox")  # mac 硬编; linux 用 nvenc/libx264
# ffmpeg(OpenSSL) 用的 CA 包: 部分 CDN(ooat88 系)证书链含自签交叉根, 系统钥匙串判定失败;
# 显式喂全根包后子请求(hls ts/crypto)继承环境变量即可通过验证。
FFMPEG_CA_FILE = os.environ.get("FFMPEG_CA_FILE", os.path.join(HERE, "ca_roots.pem"))

# ---- 录制目录 (三目录布局, 均由 docker compose 挂载) ----
# 1) agora 原生录制输出目录(agora 容器 docker 目录映射进来) —— 固态, 持续录制
AGORA_REC_DIR = os.environ.get("AGORA_REC_DIR", os.path.join(os.path.dirname(HERE), "agora recordings"))
# 2) 本项目录制暂存(web/direct 录制落地) —— 固态, 持续录制
REC_DIR = os.environ.get("BAOYU_REC_DIR", os.path.join(os.path.dirname(HERE), "baoyu recordings"))
# 3) 最终完整文件归档 —— 机械硬盘, 录完整一次性写入
FINAL_REC_DIR = os.environ.get("FINAL_REC_DIR", os.path.join(os.path.dirname(HERE), "recordings"))
for _d in (AGORA_REC_DIR, REC_DIR, FINAL_REC_DIR):
    os.makedirs(_d, exist_ok=True)

# ---- 录制性能调优 (单 Chromium 多 Tab + 极限参数) ----
# 单浏览器共享进程: 全局只 launch 一个 Chromium, 每房一个独立 BrowserContext(隔离 storage)
SINGLE_BROWSER = os.environ.get("SINGLE_BROWSER", "1") == "1"
# 每房最大并发(软提示, 供调度/告警用)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "12"))
# canvas 采集帧率: 降低重绘与编码开销(25->20), 直播够用
CAPTURE_FPS = int(os.environ.get("CAPTURE_FPS", "20"))
# 录制码率(VP8/VP9 软件编码): 6M->2.5M 画质仍清晰, 编码 CPU 大幅下降
VIDEO_BITRATE = int(os.environ.get("VIDEO_BITRATE", str(2_500_000)))
# 优先尝试的 MediaRecorder mime(按序取第一个 isTypeSupported):
#  h264 直出最省后续转码; 其次 vp9(比 vp8 同画质更省码率); 最后 vp8 兜底
REC_MIME_CANDS = [
    "video/webm;codecs=h264,opus",
    "video/x-matroska;codecs=h264,opus",
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
]
# 转码: 若 MediaRecorder 已出 h264 -> -c:v copy (零重编, CPU 最低)
#        否则 webm(vp8/vp9) -> libx264 ultrafast 实时转
TRANSCODE_FAST = os.environ.get("TRANSCODE_FAST", "1") == "1"


def _detect_board_name():
    """取主板/机器名(优先级: 环境变量 BOARD_NAME > macOS 主板型号 > 主机名), 清洗非法字符。"""
    import re
    import socket
    import subprocess
    name = os.environ.get("BOARD_NAME", "").strip()
    if not name:
        try:
            out = subprocess.run(
                ["system_profiler", "SPHardwareDataType"],
                capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"Model Identifier:\s*(\S+)", out)
            if m:
                name = m.group(1)   # 如 Mac16,10
        except Exception:
            pass
    if not name:
        try:
            name = socket.gethostname().split(".")[0]
        except Exception:
            name = "BOARD"
    # 只保留字母数字/下划线/短横线, 去掉逗号点等(文件名安全)
    name = re.sub(r"[^A-Za-z0-9_\-]", "-", name).strip("-") or "BOARD"
    return name


BOARD_NAME = _detect_board_name()   # 主板/机器名, 用于录制文件命名

MAX_SEG_BYTES = int(os.environ.get("MAX_SEG_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2GB 自动切分(可env覆盖便于测试)
VIDEO_FPS = 25
AUDIO_BITRATE = 128_000                  # 音轨 128k (aac, 画质优先)

# ffmpeg 转码参数 (webm -> mp4), 吸收 vidgear/ihmily 经验
TRANSCODE_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                  "-pix_fmt", "yuv420p", "-g", "50",
                  "-c:a", "aac", "-b:a", "128k",
                  "-movflags", "+faststart"]

# 看门狗: 录制页/进程异常退出后自动重启, 最多连续重试次数 & 间隔
WATCHDOG_MAX_RETRY = 5
WATCHDOG_RETRY_INTERVAL = 3   # 秒
