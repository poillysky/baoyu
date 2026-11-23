#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面图片代理 (imgproxy.py)

为什么需要:
- 各平台封面域名各异(部分 CDN 有防盗链/证书/连接复用差异), 浏览器 <img> 直连经常裂图;
- 后端用 requests 统一抓取(带 UA/Referer, 忽略证书)再回吐, 前端只认 localhost, 稳定;
- 少数平台(如"密语")封面是 img_key 定制置换加密的 base64 文本, 需解密还原成图。

img_key 来自初始化接口 /api/common/domain_config -> img_key 字段。
解密算法 = libkiwi App 内 encrypt_utils.dart 的 sort-table 置换(见 getSortTable/decrypt)。
"""
import base64
import hashlib
import os
import socket as _socket
import time
import requests
import urllib3

urllib3.disable_warnings()

# ---------------- 磁盘持久化缓存 ----------------
# 第一次从上游拉图成功后写入本地缓存目录, 之后该 URL 直接读本地文件,
# 不再请求上游 —— 断网/图床挂掉也能秒开, 且省流量(老大需求: 本地持久化, 不反复拉)。
_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "img_cache",
)
_CACHE_MAX_AGE = 30 * 24 * 3600  # 30天过期(图片基本不变, 覆盖删除场景)


def _cache_path(url):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, h + ".bin")


def _cache_get(url):
    """命中返回 bytes(检查过期), 未命中返回 None。不抛异常。"""
    try:
        p = _cache_path(url)
        if not os.path.exists(p):
            return None
        if time.time() - os.path.getmtime(p) > _CACHE_MAX_AGE:
            return None
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def _cache_put(url, data):
    """落盘缓存(原子写)。失败静默忽略。"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        p = _cache_path(url)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception:
        pass


def cache_stats():
    """缓存统计: {count, size_bytes}。"""
    n = sz = 0
    try:
        for fn in os.listdir(_CACHE_DIR):
            fp = os.path.join(_CACHE_DIR, fn)
            if os.path.isfile(fp) and fn.endswith(".bin"):
                n += 1
                try:
                    sz += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return {"count": n, "size_bytes": sz}


def cache_clear():
    """清空图片磁盘缓存, 返回删除文件数。"""
    n = 0
    try:
        for fn in os.listdir(_CACHE_DIR):
            fp = os.path.join(_CACHE_DIR, fn)
            if os.path.isfile(fp) and fn.endswith(".bin"):
                try:
                    os.remove(fp)
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass
    return n

# ---------------- IPv6 优先连接(happy-eyeballs) ----------------
# 根因: 图床 CDN 双栈线路不一致 —— IPv4(阿里CDN)回源 OSS 深圳桶 NoSuchKey,
# IPv6(华为 ens 边缘)才有旧图缓存。urllib3 默认按 getaddrinfo 顺序(常 IPv4)。
# 这里 patch create_connection: 先试全部 AAAA, 全败再回落 IPv4, 对无 AAAA 域名无影响。
def _install_v6_first():
    import urllib3.util.connection as _uconn
    if getattr(_uconn, "_v6_first_installed", False):
        return
    _orig = _uconn.create_connection

    def _create_connection(address, *args, **kwargs):
        host, port = address
        try:
            infos = _socket.getaddrinfo(host, port, 0, _socket.SOCK_STREAM)
        except _socket.gaierror:
            return _orig(address, *args, **kwargs)
        infos.sort(key=lambda i: 0 if i[0] == _socket.AF_INET6 else 1)
        last = None
        for af, socktype, proto, canon, sa in infos:
            try:
                return _orig(sa[:2], *args, **kwargs)
            except OSError as e:
                last = e
        raise last or OSError("no addresses for %s" % host)

    _uconn.create_connection = _create_connection
    _uconn._v6_first_installed = True


_install_v6_first()

# 与 App 一致: 优先 img_domain 前缀; 图床域名映射(旧死域名 -> 新域名)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}
_TIMEOUT = 12

# 复用连接池：封面并发请求多，每次新建 TCP/TLS 连接开销大且易触发限流。
# 用线程本地 Session 复用 keep-alive，避免跨线程共享 Session 的线程安全问题。
import threading as _threading
_LOCAL = _threading.local()


def _session():
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HEADERS)
        s.verify = False
        # 关键: trust_env=False 屏蔽 macOS 系统代理(Clash Verge 会注入系统代理,
        # requests 的 resolve_proxies 会把 {'http':None} 的 None 值丢掉再合并系统代理)
        s.trust_env = False
        _LOCAL.s = s
    # 代理开关实时读取设置(设置页可切换 走/不走 代理)
    try:
        import settings_store as SS
        p = SS.proxy_dict()
        if p.get("http"):
            s.proxies = p
        else:
            s.proxies = {}   # trust_env=False 时空 dict = 纯直连
    except Exception:
        s.proxies = {}
    return s

# 图片关键配置缓存(从 domain_config 拉取)
_CFG = {"img_key": "", "img_domain": "", "img_domain2": ""}
_CFG_TS = 0.0


def _load_cfg(force=False):
    global _CFG_TS
    import time
    now = time.time()
    if not force and _CFG["img_key"] and (now - _CFG_TS) < 3600:
        return _CFG
    try:
        import client as CL
        CL.ensure_token()
        r = CL.api("/api/common/domain_config", {"module": "live"})
        d = r.get("data") or {}
        _CFG["img_key"] = d.get("img_key") or ""
        _CFG["img_domain"] = (d.get("img_domain") or "").rstrip("/")
        _CFG["img_domain2"] = (d.get("img_domain2") or "").rstrip("/")
        _CFG_TS = now
    except Exception:
        pass
    return _CFG


# ---------------- sort-table 置换解密(复刻 App encrypt_utils) ----------------
def _sort_table(key):
    kb = key.encode("utf-8")
    kl = len(kb)
    m = {i: i for i in range(256)}
    if kl == 0:
        return m
    for i in range(0, 255):        # App: #0xff b.ge 退出 => i 0..254
        s = 0
        for j in range(kl):
            s += kb[j] + (i + 10)
        if s < 0:
            s = -s
        x1 = 256 - (i + 1)
        if x1 == 0:
            break
        r = s % x1
        idx = (i + 1) + r
        a = m[i + 1]
        b = m[idx]
        m[idx] = a
        m[i + 1] = b
    return m


def _dec_map(key):
    st = _sort_table(key)
    return {v: k for k, v in st.items()}


def decrypt_bytes(data, key):
    dec = _dec_map(key)
    return bytes(dec.get(b, b) for b in data)


def _looks_like_image(b):
    if not b:
        return False
    if b[:2] == b"\xff\xd8":          # JPEG
        return True
    if b[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        return True
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":  # WEBP
        return True
    return False


def _try_decrypt(b, key):
    """封面可能是 base64 文本密文, 也可能裸密文字节。尝试解出图片。"""
    if not key:
        return None
    cands = []
    stripped = b.strip()
    # 1) base64 文本
    try:
        cands.append(base64.b64decode(stripped, validate=False))
    except Exception:
        pass
    # 2) 裸字节
    cands.append(b)
    for c in cands:
        if not c:
            continue
        out = decrypt_bytes(c, key)
        if _looks_like_image(out):
            return out
    return None


def sniff_ctype(data):
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _candidate_urls(url):
    """返回按顺序尝试的 URL 列表: 原图 -> 换 img_domain -> 换 img_domain2。"""
    cfg = _load_cfg()
    urls = [url]
    if "://" in url:
        path = url.split("://", 1)[1]
        slash = path.find("/")
        rel = path[slash:] if slash >= 0 else ""
        for dom in (cfg.get("img_domain"), cfg.get("img_domain2")):
            if dom and rel:
                cand = dom + rel
                if cand not in urls:
                    urls.append(cand)
    return urls


class ImgMissing(Exception):
    """资源缺失(4xx 或内容为空), 对应前端 <img> 404 回退占位。"""


# 需要直连即走代理的图床域名后缀(本机直连会被 RST, 只有国内 CDN 经代理可达)
_NEED_PROXY_DOMAINS = ("jrbanquan.com",)


def _need_proxy(url):
    if "://" not in url:
        return False
    host = url.split("://", 1)[1].split("/", 1)[0].lower()
    return any(d in host for d in _NEED_PROXY_DOMAINS)


def _current_proxy():
    """从设置读代理地址; 无配置返回 None(保持纯直连语义)。"""
    try:
        import settings_store as SS
        pu = (SS.load().get("proxy_url") or "").strip()
        return {"http": pu, "https": pu} if pu else None
    except Exception:
        return None


def fetch(url):
    """抓取封面, 若是加密图则解密。返回图片字节。失败抛异常。

    策略:
    - 磁盘缓存优先: 同一 URL 第一次拉上游成功后落盘, 之后直接读本地, 不再请求上游。
    - jrbanquan 等 _NEED_PROXY_DOMAINS 图床: 直连必 RST, 直接走代理(经 Clash 回源)。
    - 其他图床: 直连优先(IPv6 优先 + trust_env=False 纯直连);
      直连网络层失败(超时/RST)时, 有代理地址就降级走代理重试一轮。
    - 4xx(不含连接异常) 是资源缺失(404), 不浪费代理轮, 直接抛 ImgMissing。
    """
    # 0) 本地缓存优先
    hit = _cache_get(url)
    if hit:
        return hit
    last_err = None
    px = _current_proxy()
    attempts = _candidate_urls(url)
    saw_4xx = False

    # 主轮
    for u in attempts:
        use_px = px and _need_proxy(u)
        try:
            r = _session().get(u, timeout=_TIMEOUT, proxies=px if use_px else None)
            if r.status_code != 200:
                last_err = "http %s @ %s" % (r.status_code, u)
                if 400 <= r.status_code < 500:
                    saw_4xx = True
                continue
            b = r.content
            if _looks_like_image(b):
                _cache_put(url, b)
                return b
            # 不像图片 -> 尝试解密
            cfg = _load_cfg()
            out = _try_decrypt(b, cfg.get("img_key", ""))
            if out:
                _cache_put(url, out)
                return out
            # 解密失败: 原样返回(前端至少有个字节流)
            last_err = "not image nor decryptable @ %s (%d bytes)" % (u, len(b))
            # 保留最后一个 200 的字节(即使不认识)作为兜底
            if len(b) > 0 and b[:1] not in (b"<",):
                _cache_put(url, b)
                return b
        except Exception as e:
            last_err = "%s @ %s" % (e, u)
            continue
    # 4xx 缺失(如 OSS NoSuchKey): 资源真不存在, 抛 ImgMissing, 前端立即回退占位
    if saw_4xx:
        raise ImgMissing(last_err or "missing (4xx)")
    # 兜底代理轮：前一round 全是网络层异常(无4xx) 且配了代理地址
    if px:
        for u in attempts:
            try:
                r = _session().get(u, timeout=_TIMEOUT, proxies=px)
                if r.status_code != 200:
                    last_err = "http %s (proxy) @ %s" % (r.status_code, u)
                    continue
                b = r.content
                if _looks_like_image(b):
                    _cache_put(url, b)
                    return b
                cfg = _load_cfg()
                out = _try_decrypt(b, cfg.get("img_key", ""))
                if out:
                    _cache_put(url, out)
                    return out
                if len(b) > 0 and b[:1] not in (b"<",):
                    _cache_put(url, b)
                    return b
                last_err = "not image nor decryptable (proxy) @ %s" % u
            except Exception as e:
                last_err = "%s (proxy) @ %s" % (e, u)
                continue
    raise RuntimeError(last_err or "all urls failed")
