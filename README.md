# 鲍鱼盒子 · 纯 Python 本地直播监控录屏

前后端分离的本地直播监控/自动录制系统。纯 Python 后端（stdlib http.server）+ 原生 HTML/JS 前端，
浏览器 headless 拉 Agora WebRTC 流，实时转封装落 mp4。开播自动录、下播自动停。

## 目录结构

```
baoyu-py/
├── backend/              后端 (Python)
│   ├── server.py          HTTP 入口 (REST + 静态托管), 默认 :8900
│   ├── config.py          集中配置 (密钥/端口/路径/录制参数)
│   ├── client.py          业务 API 客户端 (AES 签名请求 + 游客号管理)
│   ├── gateway.py         取流凭据 (room/detail → 签 Agora token)
│   ├── stream.py          定位 libkiwi 网关 + 签 007e token
│   ├── kiwi_client.py     纯 Python 复刻 libkiwi WS 协议 (Rabbit + protobuf)
│   ├── rabbit.py          Rabbit 流加密
│   ├── hub.py             前端实时推送 WebSocket (:8902)
│   ├── recorder.py        录制引擎 (headless Chromium + 实时转 mp4 + 看门狗)
│   ├── watchman.py        监守看门狗 (开播自动录 / 下播自动停)
│   └── watchlist.json     监控清单持久化
├── frontend/             前端 (原生)
│   ├── index.html         管理面板单页
│   ├── app.js             WS 订阅 + 渲染
│   ├── style.css          深色管理面板样式
│   └── recorder.html      headless 录制页 (Agora join → canvas → MediaRecorder)
├── recordings/           录制成品 (一主播一文件夹)
│   └── <主播名>_<roomId>/<主播名>_<时间>.mp4
├── docs/                 文档
└── run.sh                一键启动
```

## 依赖

- Python 3.9+，包：`requests`、`pycryptodome`、`websockets`、`playwright`
- `ffmpeg`（实时转封装 webm → h264 mp4）
- Playwright 的 Chromium（`python3 -m playwright install chromium`）
- 一台已装目标 App 并游客登录的 Android 设备/模拟器（Agora token 由 App 内 libkiwi 网关签）

## 运行

```bash
./run.sh
# 或手动:
cd backend && PORT=8900 python3 server.py
# 前端: 浏览器打开 http://127.0.0.1:8900/
```

- 后端 :8900 / 前端推送 :8902 / 录制分片 :8903
- 日志 `/tmp/baoyu_server.log`
- 停止 `lsof -ti :8900 | xargs kill -9`（同理 8902/8903）

## 使用

1. 面板「直播台」选平台 → 房间卡点「＋ 加入监控」进监控清单。
2. 看门狗每 25s 检查：开播自动拉流录制，下播自动停。
3. 「监控」页看清单实时状态；「录制记录」页播放/下载/删除成品。
4. 录制成品存 `recordings/<主播名>_<roomId>/`，文件名 `<主播名>_<时间>.mp4`。

## 说明

- 拉流分两类：Agora 房（`yunceng=true`）需 App 内 libkiwi 网关签 token；HLS 房（`yunceng=false`）走 `play_url`。
- 录制依赖 headless Chromium 的 Agora Web SDK join，音视频经 canvas + WebAudio 混流 → MediaRecorder(webm) → 后端 ffmpeg 实时转 h264/aac mp4。
- 业务接口「观看次数」按账号计（约 3 次/天），额度耗尽自动换游客号。
