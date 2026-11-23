# 鲍鱼盒子 录制系统 · 单 Chromium 多 Tab 优化版
# 基础镜像自带 Chromium + 全部系统依赖(比自建 chromium 省心, 国内镜像源)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 国内 apt 镜像(阿里)
RUN sed -i 's@http://archive.ubuntu.com@https://mirrors.aliyun.com@g; s@http://security.ubuntu.com@https://mirrors.aliyun.com@g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依赖(国内 pip 源)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r /app/backend/requirements.txt

# Playwright 的 Chromium 已在基础镜像内; 这里再确保浏览器可用(离线场景跳过)
# RUN python -m playwright install chromium

COPY backend  /app/backend
COPY static   /app/static

# 三个录制目录挂 volume(与 docker-compose 挂载点一致)
VOLUME ["/app/agora_recordings", "/app/baoyu_recordings", "/app/final_recordings"]
ENV AGORA_REC_DIR=/app/agora_recordings \
    BAOYU_REC_DIR=/app/baoyu_recordings \
    FINAL_REC_DIR=/app/final_recordings

# 单浏览器模式 + 并发上限
ENV SINGLE_BROWSER=1 \
    MAX_CONCURRENT=12 \
    CAPTURE_FPS=20 \
    VIDEO_BITRATE=2500000 \
    TRANSCODE_FAST=1 \
    PORT=8900 WS_PORT=8902 REC_PORT=8903

EXPOSE 8900 8902 8903

WORKDIR /app/backend
CMD ["python3", "server.py"]
