#!/bin/bash
# ============================================================
#  鲍鱼盒子 · 纯 Python 本地直播监控录屏 —— 一键启动
#  用法: ./run.sh          (启动后端 + 打开前端)
#        PORT=8901 ./run.sh (换端口)
# ============================================================
cd "$(dirname "$0")" || exit 1
export PORT="${PORT:-8900}"

# --- Python 解释器: 优先 3.12 venv(.venv-native312), 退回 3.9 系统 python3 ---
PYBIN="${PYBIN:-}"
if [ -z "$PYBIN" ] && [ -x "backend/.venv-native312/bin/python" ]; then
  PYBIN="$(pwd)/backend/.venv-native312/bin/python"
fi
[ -z "$PYBIN" ] && PYBIN="python3"
echo "[*] Python: $PYBIN ($($PYBIN --version 2>&1))"

CHROME="${CHROMIUM:-$($PYBIN -c 'import sys;sys.path.insert(0,"backend");import config as c;print(c.CHROMIUM)' 2>/dev/null)}"

echo "============================================================"
echo "  鲍鱼盒子 本地客户端 · 启动中"
echo "============================================================"

# --- 0. 环境自检 (可选: 检测模拟器 App) ---
ADB="${ADB:-/Users/poilly/Library/Android/sdk/platform-tools/adb}"
if [ -x "$ADB" ]; then
  APPPID=$("$ADB" -s emulator-5554 shell pidof me.hq80tw8.ws04i1m 2>/dev/null | tr -d '\r')
  if [ -n "$APPPID" ]; then echo "[i] 检测到 App 在运行(pid=$APPPID) — 本系统已脱App, 无需它"; fi
fi

# --- 1. 端口清理 + 起后端 ---
for p in $PORT $((PORT+2)) $((PORT+3)) 8902 8903; do lsof -ti :$p 2>/dev/null | xargs kill -9 2>/dev/null; done
sleep 1
echo "[*] 启动后端 :$PORT ..."
( cd backend && PORT=$PORT nohup "$PYBIN" server.py >/tmp/baoyu_server.log 2>&1 & echo $! > /tmp/baoyu_server.pid )
sleep 4
if curl --noproxy '*' -s -m5 "http://127.0.0.1:$PORT/api/health" >/tmp/_health.json 2>/dev/null; then
  echo "[+] 后端就绪 (pid=$(cat /tmp/baoyu_server.pid 2>/dev/null))"
  "$PYBIN" - <<'PY' 2>/dev/null
import json
try:
    h=json.load(open('/tmp/_health.json')); gw=h.get('gw') or {}
    print("    网关:", ("✓ port=%s"%gw.get('port')) if gw.get('ok') else "✗ 未就绪(需 App 先进过一次房)")
    print("    游客token:", "✓" if h.get('token') else "✗")
except Exception: pass
PY
else
  echo "[!] 后端启动失败, 查看 /tmp/baoyu_server.log:"; tail -12 /tmp/baoyu_server.log; exit 1
fi

# --- 2. 打开前端 (Next.js, 端口 3000) ---
FE_PORT="${FE_PORT:-3000}"
URL="http://127.0.0.1:$FE_PORT/"
echo "[*] 打开前端 $URL"
if [ -n "$CHROME" ] && [ -x "$CHROME" ]; then
  rm -rf /tmp/baoyu_profile
  "$CHROME" --no-sandbox --user-data-dir=/tmp/baoyu_profile --no-first-run --no-default-browser-check \
    --password-store=basic --use-mock-keychain --autoplay-policy=no-user-gesture-required \
    --app="$URL" --window-size=1400,900 >/tmp/baoyu_frontend.log 2>&1 &
else
  open "$URL"
fi

echo "[+] 完成。面板里点房间「＋加入监控」即自动录制。"
echo "    后端日志 /tmp/baoyu_server.log   停止: lsof -ti :$PORT | xargs kill -9"
echo "============================================================"
