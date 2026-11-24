#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监守录屏 后端 (server.py) — 资源化接口 + 实时推送
模块化: config(配置) / client(业务API) / gateway(libkiwi签token) / recorder(录制) / hub(WS推送)

接口:
  GET  /                          管理面板前端
  GET  /recorder.html             录制页(内部)
  GET  /api/health                健康 {ok,gw,token,switching}
  GET  /api/platforms             平台列表
  GET  /api/rooms?pid=&type=&kw=  房间列表
  GET  /api/detail?room_id=&pid=  房间详情
  GET  /api/play?room_id=&pid=    拉流凭据
  --- 录制(资源化) ---
  POST /api/streamers/start?room_id=&pid=   开始录(监控)
  POST /api/streamers/stop?rec_id=          停止录
  GET  /api/rec/status                      录制任务状态(全量)
  GET  /api/files                           已录制成品列表(分页/筛选)
  GET  /api/files/delete?name=              删除成品
  GET  /api/stats                           统计(磁盘/成品数/在录数/在播数)
  GET  /recordings/<fn>                     成品 mp4 下载/预览
"""
import os, sys, json, glob, time, threading, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(os.path.dirname(HERE), "static")
sys.path.insert(0, HERE)

import config as C
import client as CL
import gateway as GW
import recorder as REC
import hub as HUB
import watchman as WM
import imgproxy as IMGPROXY
import settings_store as SS

# 启动时把持久化设置应用到运行时(config 模块/录制新任务生效)

# ================= HLS AES-128 key 解密 =================
# 番茄(440 inter_encrypt) / 小妲己(383 xnm) 的 HLS key.key 返回自定义密文 payload
# (33B 常量), 需按 app 内 PlayerHttpServer 的 decryptKey 链解出 16B 明文 AES key。
# 已用 frida 在模拟器实测 + Python 向量 + 真实 ts 段(TV sync 4752/4752)三重验证。
_ALPHA = "GHI9+JKLxyz012MNOPYbcRSTUVW8/ABCw3DEZaFXefghijklm7=nopqrsdQtuv456"
_ALPHA_MAGIC = "24a1d6ff39d8"


def _md5hex(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _desc_data(payload: str) -> str:
    """descData(s): 返回 base64(utf8(s)) 的中间态 —— 内部就做一次 base64decode。
    逐条核对反汇编 + frida 入参: md5 输入 = MAGIC + last_char(实测 '24a1d6ff39d80')。"""
    import base64
    last = payload[-1]
    idx = _ALPHA.index(last)
    md5b = _md5hex(_ALPHA_MAGIC + last).encode("utf-8")   # 32 字节 hex
    out = []
    for i, ch in enumerate(payload[:-1]):
        k = (_ALPHA.index(ch) - idx - md5b[i % 32]) % 65
        out.append(_ALPHA[k])
    return base64.b64decode("".join(out)).decode("utf-8")


def _decrypt_key_payload(payload: bytes):
    """decryptKey(payload): 33B 密文 → 16B 明文 AES key bytes; 失败返回 None。
    若 payload 以 '123456' 结尾则直接截掉后 6 字符(另一分支)。"""
    import base64
    s = payload.decode("utf-8", "replace")
    if s.endswith("123456"):
        key = s[:-6].encode("utf-8")
    else:
        try:
            key = base64.b64decode(_desc_data(s))
        except Exception:
            key = None
    return key if isinstance(key, bytes) and len(key) == 16 else None
SS.apply_runtime()


_DUR_CACHE = {}
_PROBE_LOCK = threading.Lock()
_THUMB_LOCK = threading.Lock()
_FILES_CACHE = {"t": 0.0, "data": []}   # 成品列表缓存：录制中文件多时避免每次请求全量 glob+ffprobe
_FILES_CACHE_TTL = 8.0                  # 秒（列表刷新频率上限）

# 录制管理监控根目录：最终归档目录（文件移动归档后的真实存放处），可 env FILES_ROOT 覆盖
_FILES_ROOT = os.environ.get("FILES_ROOT", C.FINAL_REC_DIR)
try:
    os.makedirs(_FILES_ROOT, exist_ok=True)
except Exception:
    pass


def _probe_duration(path):
    # 防止多个 HTTP 线程同时对同一批视频起 ffprobe，CPU/进程数瞬间放大。
    with _PROBE_LOCK:
        return _probe_duration_inner(path)


def _probe_duration_inner(path):
    """用 ffprobe 取视频时长(秒); 按 (路径, mtime, size) 缓存。失败返回 0。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    key = (int(st.st_mtime), st.st_size)
    cached = _DUR_CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1]
    import subprocess, shutil, json as _json
    ff = os.environ.get("FFPROBE")
    if not ff:
        ff = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not os.path.isfile(ff):
        cand = os.path.join(os.path.dirname(os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")), "ffprobe")
        ff = cand if os.path.isfile(cand) else ff
    dur = 0.0
    try:
        r = subprocess.run(
            [ff, "-v", "quiet", "-print_format", "json", "-show_format", path],
            timeout=15, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        info = _json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
        dur = float((info.get("format") or {}).get("duration", 0) or 0)
    except Exception:
        dur = 0.0
    _DUR_CACHE[path] = (key, dur)
    return dur


def _files_list():
    # 成品列表带 TTL 缓存：录制中目录可能积累大量文件，/api/files 与 /api/stats
    # 频繁被前端轮询，若每次都 glob 全量 + 串行 ffprobe 会拖垮后端。
    now = time.time()
    if now - _FILES_CACHE["t"] < _FILES_CACHE_TTL:
        return _FILES_CACHE["data"]
    out = []
    # 递归扫描(一个主播一个文件夹): rel = "主播文件夹/文件名.(webm|mp4)"
    pats = [os.path.join(_FILES_ROOT, "**", "*.webm"), os.path.join(_FILES_ROOT, "**", "*.mp4")]
    files = []
    for p in pats:
        files.extend(glob.glob(p, recursive=True))
    entries = []
    for f in files:
        # 排除隔离区(损坏抢救文件, 不展示在监控录制列表)
        if os.sep + "_recovered" + os.sep in f:
            continue
        try:
            st = os.stat(f)  # 扫描与读取之间文件可能被删除/切段，跳过消失项而不是整请求 500
            entries.append((f, st))
        except OSError:
            continue
    for f, st in sorted(entries, key=lambda x: x[1].st_mtime, reverse=True):
        rel = os.path.relpath(f, _FILES_ROOT).replace(os.sep, "/")
        parts = rel.split("/")
        # 三级布局 recordings/平台/主播文件夹/文件; 旧二级布局 records/主播/文件 兼容
        plat = parts[0] if len(parts) >= 3 else ""
        folder = parts[1] if len(parts) >= 3 else (parts[0] if len(parts) > 1 else "")
        out.append({"name": parts[-1], "plat": plat, "folder": folder,
                    "rel": rel, "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "duration": round(_probe_duration(f), 1),
                    "thumb": "/api/thumb?name=" + quote(rel),
                    "url": "/recordings/" + rel})
    _FILES_CACHE["t"] = now
    _FILES_CACHE["data"] = out
    return out


# ---- 视频缩略图（ffmpeg 抓首帧，磁盘缓存） ----
THUMB_DIR = os.path.join(os.path.dirname(_FILES_ROOT), ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
_FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")


def _thumb_path(rel):
    import hashlib
    _src = os.path.join(_FILES_ROOT, rel)
    _mt = str(int(os.path.getmtime(_src))) if os.path.exists(_src) else rel
    h = hashlib.md5((rel + "|" + _mt).encode()).hexdigest()
    return os.path.join(THUMB_DIR, h + ".jpg"), h


def _get_thumb(rel):
    # 缩略图生成串行化，避免同一视频并发写同一临时文件及 ffmpeg 进程风暴。
    with _THUMB_LOCK:
        return _get_thumb_inner(rel)


def _get_thumb_inner(rel):
    """返回缩略图 jpg 的绝对路径；必要时用 ffmpeg 生成。失败返回 None。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    root = os.path.abspath(_FILES_ROOT)
    src = os.path.abspath(os.path.join(root, rel))
    if not (src.startswith(root + os.sep) and os.path.isfile(src)):
        return None
    tp, _h = _thumb_path(rel)
    if os.path.isfile(tp) and os.path.getsize(tp) > 0:
        return tp
    import subprocess, shutil
    ff = _FFMPEG if os.path.isfile(_FFMPEG) else (shutil.which("ffmpeg") or "ffmpeg")
    tmp = tp + ".tmp.jpg"
    # 抓 1s 处一帧（短视频退化到 0s），缩放到宽 480
    for ss in ("1", "0"):
        try:
            subprocess.run(
                [ff, "-y", "-ss", ss, "-i", src, "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", tmp],
                timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, tp)
                return tp
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    return None


def _stats():
    files = _files_list()
    total_bytes = sum(f["size"] for f in files)
    status = REC.api_status()
    recording = sum(1 for s in status if s.get("state") in ("recording", "starting", "restarting"))
    # 今日新增成品(按 mtime 当天)
    import datetime
    day0 = time.time() - time.time() % 86400 - time.timezone
    today = [f for f in files if f["mtime"] >= day0]
    today_bytes = sum(f["size"] for f in today)
    du = None
    try:
        st = os.statvfs(_FILES_ROOT)
        du = {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
    except Exception:
        pass
    return {"files": len(files), "total_bytes": total_bytes,
            "recording": recording, "tasks": len(status),
            "today_files": len(today), "today_bytes": today_bytes,
            "disk": du, "watch": len(WM.list_watch()),
            "watch_interval": WM.get_interval()}


def _snapshot():
    """前端连接 WS 时推的全量快照。"""
    return {"rec": REC.api_status(), "stats": _stats(), "watch": WM.list_watch()}


def _net_test():
    """设置页「服务端网络测试」：以直播IP链路可达性为主。
    逐项探测并计时, 每项 {name, ok, ms, msg}。"""
    import socket
    import time as _t
    import random as _rnd

    def _probe(name, target, fn):
        t0 = _t.time()
        try:
            msg = fn()
            return {"name": name, "target": target, "ok": True,
                    "ms": round((_t.time() - t0) * 1000), "msg": msg or ""}
        except Exception as e:
            return {"name": name, "target": target, "ok": False,
                    "ms": round((_t.time() - t0) * 1000), "msg": str(e)[:120]}

    out = []

    # 1. 签权隧道 UDP 162.14.199.148:9875 (KCP 变体)
    #    connect() 后直接发 STUN binding(0x00010000+cookie) 探测; KCP 服务端不回 STUN,
    #    但 UDP 无 ICMP 拒绝/可达 即 OK; ICMP refused 则网络层即不可达
    def _kiwi_udp():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.sendto(b"\x00\x01\x00\x00\x21\x12\xa4\x42" + _rnd.randbytes(12),
                     ("162.14.199.148", 9875))
            try:
                s.recvfrom(2048)
                return "UDP 有响应"
            except socket.timeout:
                return "UDP 已送达(无 STUN 回包, KCP 正常)"
        finally:
            s.close()
    out.append(_probe("签权隧道 (UDP/KCP)", "162.14.199.148:9875", _kiwi_udp))

    # 2. bootstrap 节点池 UDP 8003 (阿里云 bootstrap, 拿 Kiwi 节点池)
    def _bootstrap_udp():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.sendto(b"\x00" * 40, ("43.199.86.204", 8003))
            try:
                d, _ = s.recvfrom(65535)
                return "bootstrap 收到节点池 %dB" % len(d)
            except socket.timeout:
                return "bootstrap UDP 已送达(节点池稍后回)"
        except ConnectionRefusedError:
            raise RuntimeError("ICMP 拒达")
        finally:
            s.close()
    out.append(_probe("bootstrap 节点池 (UDP)", "43.199.86.204:8003", _bootstrap_udp))

    # 3. 业务 API TCP 103.44.236.131:8001 (直播间数据/real_room_id)
    def _biz():
        s = socket.create_connection(("103.44.236.131", 8001), timeout=5)
        s.close()
        return "TCP 已连通"
    out.append(_probe("业务 API (TCP)", "103.44.236.131:8001", _biz))

    # 4. 业务 API 加密接口(走 client, 验证 token/协议——直播间数据源)
    def _biz_api():
        import client as _CL
        r = _CL.platforms()
        n = len((r.get("data") or []))
        if n <= 0:
            raise RuntimeError("platforms 空")
        return "platforms %d 个平台" % n
    out.append(_probe("业务接口 (AES协议)", "103.44.236.131:8001/platforms", _biz_api))

    # 5. Agora 媒体边缘 TCP 443 (WebRTC 拉流信令/测速; SDK 会连 ap-web/edge)
    def _agora_edge():
        import socket as _sk
        results = []
        for host in (("ap-web-1-asia.agora.io", 443), ("ap-proxy-1.agora.io", 443)):
            try:
                s = _sk.create_connection(host, timeout=5)
                s.close()
                results.append("%s:443" % (host[0].split(".", 2)[0] + ".."))
            except Exception:
                continue
        if not results:
            raise RuntimeError("Agora 边缘 TCP 均连不通")
        return "可达 " + " ".join(results)
    out.append(_probe("Agora 媒体边缘 (TCP:443)", "ap-web-1-asia / ap-proxy-1", _agora_edge))

    # 6. 封面图床 IPv6(选一个平台真实封面抓一次——直播封面链路)
    cover = ""
    try:
        r = CL.platforms()
        for pl in (r.get("data") or []):
            if (pl.get("cover") or "").startswith("http"):
                cover = pl["cover"]
                break
    except Exception:
        pass

    def _img6():
        if not cover:
            raise RuntimeError("无可用封面 URL")
        import requests, urllib3
        urllib3.disable_warnings()
        import urllib3.util.connection as _uc
        orig = _uc.allowed_gai_family
        _uc.allowed_gai_family = lambda: __import__("socket").AF_INET6
        try:
            r = requests.get(cover, verify=False, timeout=8,
                             proxies={"http": None, "https": None})
        finally:
            _uc.allowed_gai_family = orig
        # 收到 HTTP 响应(任意状态码)即链路可达; 内容不一致只作提示
        return "IPv6 链路通 http=%s %d字节" % (r.status_code, len(r.content))
    out.append(_probe("封面图床 IPv6 (华为ens边缘)", "yn7ux.ooat88.com", _img6))

    # 7. 封面图床 IPv4 (阿里CDN回源)
    def _img4():
        if not cover:
            raise RuntimeError("无可用封面 URL")
        import requests, urllib3
        urllib3.disable_warnings()
        import urllib3.util.connection as _uc
        orig = _uc.allowed_gai_family
        _uc.allowed_gai_family = lambda: __import__("socket").AF_INET
        try:
            r = requests.get(cover, verify=False, timeout=8,
                             proxies={"http": None, "https": None})
        finally:
            _uc.allowed_gai_family = orig
        return "IPv4 链路通 http=%s %d字节" % (r.status_code, len(r.content))
    out.append(_probe("封面图床 IPv4 (阿里CDN回源)", "yn7ux.ooat88.com", _img4))

    # 8. DNS 双栈记录
    def _dns():
        if not cover:
            raise RuntimeError("无可用封面 URL")
        host = cover.split("://", 1)[1].split("/", 1)[0]
        a6 = sorted(set(x[4][0] for x in socket.getaddrinfo(host, 443, socket.AF_INET6)))
        a4 = sorted(set(x[4][0] for x in socket.getaddrinfo(host, 443, socket.AF_INET)))
        return "%s: AAAA×%d A×%d" % (host, len(a6), len(a4))
    out.append(_probe("DNS 双栈记录", "yn7ux.ooat88.com", _dns))

    return {"results": out, "proxy": SS.proxy_info(),
            "all_ok": all(r["ok"] for r in out)}


def _delete_file(name):
    # 支持 "主播文件夹/文件名.mp4" 相对路径; 防目录穿越
    rel = (name or "").replace("\\", "/").lstrip("/")
    fp = os.path.abspath(os.path.join(_FILES_ROOT, rel))
    if not fp.startswith(os.path.abspath(_FILES_ROOT) + os.sep):
        return {"ok": False, "msg": "非法路径"}
    if os.path.isfile(fp):
        try:
            os.remove(fp)
            # 顺带清掉空的主播目录
            d = os.path.dirname(fp)
            try:
                if d != os.path.abspath(_FILES_ROOT) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass
            _FILES_CACHE["t"] = 0.0   # 文件变更后立即使列表缓存失效
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
    return {"ok": False, "msg": "文件不存在"}


def _delete_folder(folder):
    # 删除整个主播文件夹（连同里面所有视频文件、子目录）; 防目录穿越
    import shutil
    rel = (folder or "").replace("\\", "/").strip().strip("/")
    if not rel or rel in (".", ".."):
        return {"ok": False, "msg": "非法文件夹"}
    root = os.path.abspath(_FILES_ROOT)
    fp = os.path.abspath(os.path.join(root, rel))
    # 如果传入的是文件路径(带扩展名)，退化为删除其所在文件夹
    if os.path.isfile(fp):
        fp = os.path.dirname(fp)
    if not (fp != root and fp.startswith(root + os.sep)):
        return {"ok": False, "msg": "非法路径"}
    if not os.path.isdir(fp):
        return {"ok": False, "msg": "文件夹不存在"}
    try:
        n = 0
        for _base, _dirs, fs in os.walk(fp):
            n += len(fs)
        shutil.rmtree(fp, ignore_errors=False)
        # 二次校验：确保目录真的被移除（防止个别占用残留）
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        if os.path.isdir(fp):
            return {"ok": False, "msg": "文件夹未能完全删除（可能被占用）"}
        _FILES_CACHE["t"] = 0.0   # 文件变更后立即使列表缓存失效
        return {"ok": True, "deleted": n}
    except Exception as e:
        # 尽力兜底
        try:
            shutil.rmtree(fp, ignore_errors=True)
        except Exception:
            pass
        if os.path.isdir(fp):
            return {"ok": False, "msg": str(e)}
        _FILES_CACHE["t"] = 0.0   # 文件变更后立即使列表缓存失效
        return {"ok": True, "deleted": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy_stream(self, url, hls_key=False):
        """直链流代理: m3u8/ts/flv 转发(hls.js 需要同源以绕 CORS)。流式管道不落盘。
        m3u8 内容特殊处理: 内部所有 URI(ts段/key) 重写为 /api/stream?url=<绝对地址>,
        让整个 HLS 会话(清单+段+密钥)全部走同源代理。
        hls_key=True 时视为 HLS AES-128 密钥请求: 上游返回的是自定义密文 payload
        (inter_encrypt 33B 常量等), 需经 decryptKey 链解出 16B 明文 key 再返回。
        返回的 m3u8 里所有资源(key/ts)都重写为**绝对 URL**(http://<本机Host>/api/stream?...),
        这样无论 hls.js(前端 :3000) 还是 ffmpeg(本机) 拉代理流都不会跨端口串线。
        """
        import requests as _rq
        import urllib.parse as _up
        try:
            s = _rq.Session()
            s.trust_env = False          # 直连, 不走系统代理
            s.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                              "Referer": url})
            low = url.split("?")[0].lower()
            if low.endswith(".m3u8"):
                txt = s.get(url, timeout=(5, 15), verify=False).text
                base = url
                def _abs(u, b=base):
                    return _up.urljoin(b, u)
                # 重写用**相对路径** /api/stream(不是绝对 host):
                #  - 前端 hls.js/媒体元素: 相对路径按页面 origin(:3000) 解析 → Next rewrites 兜底到 8900, 远程访问也通;
                #  - ffmpeg(recorder 直录): recorder 显式用 http://127.0.0.1:8900/api/stream?url=<m3u8> 拉取,
                #    相对 key/ts 按这个 base 解析 → 8900, 通。
                # 若重写成绝对 http://127.0.0.1:8900/... 对远程浏览器是死链(请求客户端自己机器)。
                def _wrap(u):
                    return "/api/stream?url=" + _up.quote(_abs(u), safe="")
                lines = []
                for ln in txt.splitlines():
                    ls = ln.strip()
                    if not ls:
                        continue
                    if ls.startswith("#EXT-X-KEY") or ls.startswith("#EXT-X-MAP"):
                        # key URI 加 &hls_key=1 让代理识别并解密;
                        # 显式给 IV=0x0 (上游 inter_encrypt 全程零 IV, 非 media seq)
                        def _key_rewrite(m):
                            return 'URI="%s&hls_key=1"' % _wrap(m.group(1))
                        ls = re.sub(r'URI="([^"]+)"', _key_rewrite, ls)
                        if "IV=" not in ls:
                            ls += ",IV=0x0"
                        else:
                            ls = re.sub(r'IV=0x[0-9A-Fa-f]{1,16}', "IV=0x0", ls)
                    elif not ls.startswith("#"):
                        ls = _wrap(ls)
                    lines.append(ls)
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            up = s.get(url, stream=True, timeout=(5, 30), verify=False)
            if up.status_code >= 400:
                up.close()
                return self._send(502, {"ok": False, "msg": "upstream %d" % up.status_code})
            ctype = up.headers.get("Content-Type", "application/octet-stream")
            # ---- HLS AES-128 key 解密分支 ----
            # 上游 key.key 返回自定义密文 payload(inter_encrypt: 33B 常量), 需经
            # decryptKey 链解出 16B 明文 key 返回 hls.js/ffmpeg, 否则解密失败白屏。
            if hls_key:
                payload = up.content  # 直接收完(密钥体很小)
                up.close()
                import base64, hashlib
                from Crypto.Cipher import AES as _AES
                plain = _decrypt_key_payload(payload)
                if not plain:
                    return self._send(502, {"ok": False, "msg": "hls key decrypt fail"})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(plain)))
                self.end_headers()
                self.wfile.write(plain)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            cl = up.headers.get("Content-Length")
            if cl:
                self.send_header("Content-Length", cl)
            self.end_headers()
            for chunk in up.iter_content(chunk_size=64 * 1024):
                if chunk:
                    try:
                        self.wfile.write(chunk)
                    except Exception:
                        break
            up.close()
        except Exception as e:
            try:
                self._send(502, {"ok": False, "msg": "stream proxy fail: %s" % e})
            except Exception:
                pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_file(self, fp, ctype, download=False):
        size = os.path.getsize(fp)
        # 支持 HTTP Range (浏览器 <video> 拖动进度必需)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            try:
                spec = rng[len("bytes="):].split(",")[0].strip()
                a, _, b = spec.partition("-")
                if a == "":            # 后缀: bytes=-N
                    start = max(0, size - int(b)); end = size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
                end = min(end, size - 1)
                if start <= end:
                    partial = True
            except Exception:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(fp))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except Exception:
            pass

    def _static(self, name, ctype):
        fp = os.path.join(FRONTEND, name)
        if os.path.exists(fp):
            with open(fp, "rb") as f:
                return self._send(200, f.read(), ctype)
        return self._send(404, {"ok": False})

    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        try:
            # ---- 静态页面 ----
            if p in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if p == "/recorder.html":
                return self._static("recorder.html", "text/html; charset=utf-8")
            if p == "/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if p == "/AgoraRTC_N-4.20.0.js":
                return self._static("AgoraRTC_N-4.20.0.js", "application/javascript; charset=utf-8")
            if p == "/style.css":
                return self._static("style.css", "text/css; charset=utf-8")
            # ---- PWA 已停用: sw.js/manifest/icons 一律 404(防止 Service Worker 续命) ----

            # ---- 基础 ----
            if p == "/api/health":
                return self._send(200, {"ok": True, "gw": GW.gateway_status(),
                                        "token": bool(CL._STATE["token"]),
                                        "switching": CL.switching(),
                                        "ws_port": C.WS_PORT})
            if p == "/api/settings/network":
                # 设置页「网络管理」：汇总网关/身份/代理状态
                return self._send(200, {"ok": True, "data": {
                    "gw": GW.gateway_status(),
                    "token": bool(CL._STATE["token"]),
                    "switching": CL.switching(),
                    "is_member": CL.is_account_active(),
                    "ws_port": C.WS_PORT,
                    "rec_port": C.REC_PORT,
                    "proxy": SS.proxy_info(),
                    "agora": SS.agora_info(),
                }})
            if p == "/api/settings/agora":
                # 设置页「网络管理」: agora 原生录制服务 IP/端口/输出目录
                upd = {}
                if q.get("enabled", [""])[0]:
                    upd["agora_enabled"] = q.get("enabled", [""])[0] in ("1", "true", "on")
                host = q.get("host", [""])[0].strip()
                if host:
                    upd["agora_host"] = host
                port = q.get("port", [""])[0].strip()
                if port.isdigit():
                    upd["agora_port"] = int(port)
                if upd:
                    SS.save(**upd)
                return self._send(200, {"ok": True, "data": SS.agora_info()})
            if p == "/api/settings/proxy":
                # 设置页「网络管理」配置: 走/不走代理
                enabled = q.get("enabled", [""])[0]
                url = q.get("url", [""])[0].strip()
                if enabled:
                    upd = {"proxy_enabled": enabled in ("1", "true", "on")}
                    if url:
                        upd["proxy_url"] = url
                    SS.save(**upd)
                    SS.apply_runtime()
                return self._send(200, {"ok": True, "data": SS.proxy_info()})
            if p in ("/api/settings/all", "/api/settings/modes"):
                return self._send(200, {"ok": True, "data": {
                    "proxy": SS.proxy_info(), "modes": SS.modes_dict()}})
            if p == "/api/settings/mode":
                # 运行模式/录制模式修改(新任务生效)
                def _b(k):
                    v = q.get(k, [""])[0].lower()
                    return None if v == "" else (v in ("1", "true", "on"))
                upd = {}
                for k in ("single_browser", "transcode_fast"):
                    v = _b(k)
                    if v is not None:
                        upd[k] = v
                for k in ("max_concurrent", "video_bitrate", "capture_fps"):
                    v = q.get(k, [""])[0]
                    if v:
                        try:
                            upd[k] = max(1, int(v))
                        except Exception:
                            pass
                # 画质档位: high/medium/low → 映射码率+帧率并持久化
                if q.get("quality", [""])[0]:
                    try:
                        return self._send(200, {"ok": True, "data": SS.set_quality(q.get("quality", [""])[0])})
                    except ValueError as e:
                        return self._send(400, {"ok": False, "msg": str(e)})
                if upd:
                    SS.save(**upd)
                    SS.apply_runtime()
                return self._send(200, {"ok": True, "data": SS.modes_dict()})
            if p == "/api/settings/nettest":
                return self._send(200, {"ok": True, "data": _net_test()})
            if p == "/api/settings/cache":
                # GET /api/settings/cache           -> 统计
                # GET /api/settings/cache?clear=1   -> 清空图片磁盘缓存
                if q.get("clear", ["0"])[0] == "1":
                    n = IMGPROXY.cache_clear()
                    return self._send(200, {"ok": True, "data": {
                        "cleared": n, **IMGPROXY.cache_stats()}})
                return self._send(200, {"ok": True, "data": IMGPROXY.cache_stats()})
            if p == "/api/platforms":
                return self._send(200, CL.platforms())
            if p == "/api/rooms":
                pid = q.get("pid", ["449"])[0]; rt = q.get("type", ["0"])[0]
                kw = q.get("kw", [""])[0].strip()
                res = CL.rooms(pid, rt, kw)
                # 支持 ?page=&size= 客户端分页(数据已一次拉全, 这里切片返回)
                try:
                    pg = int(q.get("page", ["0"])[0])
                    sz = int(q.get("size", ["0"])[0])
                except Exception:
                    pg = sz = 0
                if pg > 0 and sz > 0:
                    arr = res.get("data") or []
                    total = len(arr)
                    start = (pg - 1) * sz
                    res["data"] = arr[start:start + sz]
                    res["total"] = total
                    res["page"] = pg
                    res["has_more"] = start + sz < total
                res["switching"] = CL.switching()
                res["is_member"] = CL.is_account_active()
                return self._send(200, res)
            if p == "/api/img":
                # 封面图片代理: 后端 requests 取图(绕 referer/证书/防盗链), 若是 img_key 加密的 base64 则解密
                url = q.get("url", [""])[0]
                if not url:
                    return self._send(400, {"ok": False, "msg": "missing url"})
                try:
                    data = IMGPROXY.fetch(url)
                    ctype = IMGPROXY.sniff_ctype(data)
                    return self._send(200, data, ctype=ctype)
                except IMGPROXY.ImgMissing as e:
                    # 资源缺失(404): 前端 <img> onError 立即回退占位, 不拖慢列表
                    return self._send(404, {"ok": False, "msg": str(e)})
                except Exception as e:
                    return self._send(502, {"ok": False, "msg": "img fetch fail: %s" % e})
            if p == "/api/stream":
                # 直链流代理(HLS m3u8 / FLV): 前端 hls.js/mpegts.js 用 xhr 拉流受 CORS 限制,
                # 由后端 requests 转发(trust_env 直连, 流式管道, 不落盘)。
                # m3u8 内部重写的 URL 是 quote 过的, 这里统一 unquote 还原。
                url = q.get("url", [""])[0]
                if not url:
                    return self._send(400, {"ok": False, "msg": "missing url"})
                url = unquote(url).strip()
                hls_key = (q.get("hls_key", ["0"])[0] or "0") == "1"
                return self._proxy_stream(url, hls_key=hls_key)
            if p == "/api/detail":
                return self._send(200, CL.room_detail(q.get("room_id", [""])[0], q.get("pid", ["449"])[0]))
            if p == "/api/play":
                rid = q.get("room_id", [""])[0]; pid = q.get("pid", ["449"])[0]
                slot = int(q.get("slot", ["0"])[0])
                return self._send(200, GW.get_stream_credentials(rid, pid, slot))

            # ---- 录制(资源化) ----
            if p in ("/api/streamers/start", "/api/rec/start"):
                rid = q.get("room_id", [""])[0]; pid = q.get("pid", ["449"])[0]
                res = GW.get_stream_credentials(rid, pid, 0)
                if not res.get("ok"):
                    return self._send(200, {"ok": False, "msg": res.get("msg"),
                                            "switching": res.get("switching")})
                try:
                    cred = {
                        "appid": res["appid"], "channel": res["channel"],
                        "token": res["token"], "uid": res["uid"],
                        "real_room_id": res.get("real_room_id"),
                        "mode": res.get("mode"), "play_url": res.get("play_url") or ""}
                    out = REC.api_start(rid, pid, cred, name=res.get("name"))
                    out["name"] = res.get("name"); out["room_id"] = rid
                    out["engine"] = "direct_ffmpeg" if res.get("mode") == "direct" else "web_chromium"
                    HUB.broadcast("rec_status", REC.api_status())
                    return self._send(200, out)
                except Exception as e:
                    return self._send(200, {"ok": False, "msg": "启动录制失败:%s" % e})
            if p in ("/api/streamers/stop", "/api/rec/stop"):
                rec_id = q.get("rec_id", [""])[0]
                try:
                    out = REC.api_stop(rec_id)
                    HUB.broadcast("rec_status", REC.api_status())
                    return self._send(200, out)
                except Exception as e:
                    return self._send(200, {"ok": False, "msg": "停止失败:%s" % e})
            if p == "/api/rec/status":
                # 优先读 sqlite rec_tasks 表(录制引擎每秒写入) → 打开页面数据直接从数据库秒读
                try:
                    import status_db as _SDB
                    _recs = _SDB.read_rec_tasks()
                except Exception:
                    _recs = REC.api_status()
                return self._send(200, {"ok": True, "data": _recs})

            # ---- 监控清单(监守模式: 开播自动录/下播自动停) ----
            if p == "/api/watch/list":
                return self._send(200, {"ok": True, "data": WM.list_watch()})
            if p == "/api/watch/add":
                rid = q.get("room_id", [""])[0]; pid = q.get("pid", ["449"])[0]
                name = q.get("name", [""])[0] or None; cover = q.get("cover", [""])[0] or None
                if not rid:
                    return self._send(200, {"ok": False, "msg": "缺 room_id"})
                return self._send(200, WM.add(rid, pid, name, cover))
            if p == "/api/watch/remove":
                return self._send(200, WM.remove(q.get("room_id", [""])[0]))
            if p == "/api/watch/enable":
                # 启用/关闭监控: ?room_id=..&enabled=1|0 (关闭时自动停掉当前录制)
                rid = q.get("room_id", [""])[0]
                en = (q.get("enabled", ["1"])[0] or "1").lower() in ("1", "true", "yes", "on")
                if not rid:
                    return self._send(200, {"ok": False, "msg": "缺 room_id"})
                return self._send(200, WM.set_enabled(rid, en))
            if p == "/api/watch/check":
                # 手动触发一轮检查(调试/立即检测)
                try:
                    WM._tick()
                except Exception as e:
                    return self._send(200, {"ok": False, "msg": str(e)})
                return self._send(200, {"ok": True, "data": WM.list_watch()})
            if p == "/api/watch/interval":
                # GET 返回当前轮询秒; ?sec=N 设置(>=5)
                sec = q.get("sec", [""])[0]
                if sec:
                    try:
                        WM.set_interval(int(sec))
                    except Exception as e:
                        return self._send(200, {"ok": False, "msg": str(e)})
                return self._send(200, {"ok": True, "interval": WM.get_interval()})

            # ---- 成品文件 ----
            if p == "/api/files":
                files = _files_list()
                kw = q.get("kw", [""])[0].strip()
                if kw:
                    files = [f for f in files if kw in f["name"]]
                return self._send(200, {"ok": True, "data": files})
            if p == "/api/thumb":
                tp = _get_thumb(q.get("name", [""])[0])
                if not tp:
                    return self._send(404, {"ok": False, "msg": "缩略图生成失败"})
                try:
                    with open(tp, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception as e:
                    return self._send(500, {"ok": False, "msg": str(e)})
            if p == "/api/files/delete":
                return self._send(200, _delete_file(q.get("name", [""])[0]))
            if p == "/api/files/delete_folder":
                return self._send(200, _delete_folder(q.get("folder", [""])[0]))
            if p == "/api/stats":
                return self._send(200, {"ok": True, "data": _stats()})

            # ---- 账号登录 ----
            if p == "/api/login/status":
                acc = CL.account_token()
                used = CL._STATE.get("token", "")
                is_acc = bool(acc) and used == acc
                info = CL._ACC.get("info", {}) or {}
                return self._send(200, {"ok": True, "data": {
                    "has_account": bool(acc),
                    "account_active": is_acc,          # 当前全局 token 是否已是账号身份
                    "account_token": (acc[:12] + "…") if acc else "",
                    "guest_active": not is_acc,
                    "account_info": {
                        "mobile": info.get("mobile", ""),
                        "email": info.get("email", ""),
                        "nickname": info.get("nickname", ""),
                        "login_type": info.get("login_type", ""),
                        "login_at": info.get("login_at", 0),
                    },
                }})
            if p == "/api/login/send_code":
                mobile = q.get("mobile", [""])[0].strip()
                email = q.get("email", [""])[0].strip()
                if not mobile and not email:
                    return self._send(200, {"ok": False, "msg": "缺 mobile 或 email"})
                r = CL.send_code(mobile=mobile or None, email=email or None)
                return self._send(200, {"ok": r.get("code") in (200, 20000),
                                        "code": r.get("code"), "msg": r.get("msg")})
            if p == "/api/login/account":
                mobile = q.get("mobile", [""])[0].strip()
                email = q.get("email", [""])[0].strip()
                pwd = q.get("password", [""])[0]
                code = q.get("code", [""])[0]
                lt = q.get("login_type", ["mobile"])[0]
                r = CL.login_account(mobile=mobile or None, email=email or None,
                                      password=pwd, verify_code=code, login_type=lt)
                ok = bool(CL.account_token())
                return self._send(200, {"ok": ok, "code": r.get("code"),
                                        "msg": r.get("msg") or ("登录成功" if ok else "")})
            if p == "/api/login/use_account":
                ok = CL.use_account_token()
                return self._send(200, {"ok": ok,
                                        "msg": "已切换为会员身份" if ok else "尚无账号token"})
            if p == "/api/login/guest_new":
                try:
                    with CL._TOKEN_LOCK:
                        CL._STATE["token"] = CL.new_guest()
                        CL._ACC["token"] = ""
                        CL._ACC["info"] = {}
                    CL._del_acc_file()
                    return self._send(200, {"ok": True, "msg": "已切换新游客号"})
                except Exception as e:
                    return self._send(200, {"ok": False, "msg": "换号失败:%s" % e})
            if p == "/api/login/logout":
                CL.logout_account()
                try:
                    CL.use_guest()
                except Exception:
                    pass
                return self._send(200, {"ok": True, "msg": "已退出登录"})

            if p.startswith("/recordings/"):
                # 支持子目录: /recordings/主播文件夹/文件名.mp4 (unquote 用模块顶部导入)
                rel = unquote(p[len("/recordings/"):]).replace("\\", "/").lstrip("/")
                # 优先最终归档目录；暂存目录(录制中/未归档)兜底
                roots = (os.path.abspath(_FILES_ROOT), os.path.abspath(C.REC_DIR))
                fp = None
                for r in roots:
                    cand = os.path.abspath(os.path.join(r, rel))
                    if cand.startswith(r + os.sep) and os.path.isfile(cand):
                        fp = cand
                        break
                if fp is not None:
                    dl = q.get("dl", ["0"])[0] == "1"
                    ctype = "video/webm" if fp.lower().endswith(".webm") else "video/mp4"
                    return self._send_file(fp, ctype, download=dl)
                return self._send(404, {"ok": False})

            return self._send(404, {"ok": False, "msg": "not found"})
        except Exception as e:
            return self._send(500, {"ok": False, "msg": str(e)})

    def do_POST(self):
        # 与 GET 同逻辑(兼容 POST 调用)
        return self.do_GET()


def main():
    # 容器内 Python 是 PID 1: 不自动回收子进程 -> chrome/crashpad 僵尸越积越多。
    # 把 SIGCHLD 设为 SIG_IGN, 让内核自动 reap 已退出的子进程(僵尸不再堆积)。
    try:
        import signal as _sig
        _sig.signal(_sig.SIGCHLD, _sig.SIG_IGN)
    except Exception:
        pass
    # 兜底: 后台定期 reap(某些 libc 对 SIG_IGN+SIGCHLD 行为不同, 双保险)
    try:
        import threading as _th, os as _os
        def _reap():
            while True:
                try:
                    while _os.waitpid(-1, _os.WNOHANG)[0] > 0:
                        pass
                except Exception:
                    pass
                time.sleep(30)
        _th.Thread(target=_reap, daemon=True).start()
    except Exception:
        pass
    CL.ensure_token()
    HUB.set_snapshot_provider(_snapshot)
    HUB.start()
    REC.ws_port()  # 预热录制分片 WS
    WM.start()     # 启动监守看门狗(开播自动录/下播自动停)
    srv = ThreadingHTTPServer(("0.0.0.0", C.PORT), Handler)
    gw = GW.gateway_status()
    print("=" * 56)
    print(" 监守录屏 后端已启动")
    print("   前端  http://127.0.0.1:%d" % C.PORT)
    print("   实时  ws://127.0.0.1:%d" % C.WS_PORT)
    print("   录制分片 ws://127.0.0.1:%d" % C.REC_PORT)
    print("   网关  %s" % ("port=%s" % gw.get("port") if gw.get("ok")
                          else "未就绪(%s)" % gw.get("msg", "App未进房")))
    print("=" * 56)
    srv.serve_forever()


# 启动时: 把库里残留的活动录制标记为已中断(进程重启, 任务已丢)
try:
    import status_db as _SDB
    _SDB.mark_stale_active()
    print("[SERVER] 清理重启前残留的活动录制状态")
except Exception as e:
    print("[SERVER] mark_stale_active err:", e)


if __name__ == "__main__":
    main()
