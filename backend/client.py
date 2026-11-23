#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务接口客户端 (client.py)
封装: AES 加密调用鲍鱼盒子服务端 + 游客号(设备号)登录/换号/额度管理。
纯 Python, 完全本地造设备号, 不依赖 App token。
"""
import os, sys, json, time, threading, uuid, hashlib, base64, random

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C

import requests, urllib3
urllib3.disable_warnings()
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


def _enc(s):
    return base64.b64encode(AES.new(C.KEY, AES.MODE_CBC, C.IV).encrypt(pad(s.encode(), 16))).decode()


def _dec(b):
    return unpad(AES.new(C.KEY, AES.MODE_CBC, C.IV).decrypt(base64.b64decode(b)), 16).decode()


# 当前游客 token + 换号状态
_STATE = {"token": "", "switching": False}
_ACC = {"token": "", "info": {}}   # 当前账号(会员/普通) token + 展示信息, 与游客 token 分开存
_ACC_FILE = os.path.join(HERE, "account_state.json")   # 账号登录态持久化(重启/web刷新不丢)


def _save_account():
    """把账号登录态写盘(原子替换)。"""
    try:
        tmp = _ACC_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_ACC, f, ensure_ascii=False)
        os.replace(tmp, _ACC_FILE)
    except Exception as e:
        print("[!] 保存账号态失败:", e, flush=True)


def _load_account():
    """启动时读回账号登录态; 读到就把全局 _STATE 也切成账号身份。"""
    try:
        if os.path.exists(_ACC_FILE):
            with open(_ACC_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("token"):
                _ACC["token"] = d.get("token", "")
                _ACC["info"] = d.get("info", {}) or {}
                _STATE["token"] = _ACC["token"]   # 恢复即会员身份
                print("[*] 已恢复账号登录态:", _ACC["token"][:24], flush=True)
    except Exception as e:
        print("[!] 读取账号态失败:", e, flush=True)
_TOKEN_LOCK = threading.Lock()
_SWITCH_LOCK = threading.Lock()
_QUOTA_HINT = "次数不足"


def api(path, biz, token=None, timeout=60, retry=5, method="GET"):
    """调用业务接口. 返回解密后的 dict。

    method="GET":  q(query) 里带 version/auth/once/auth_timestamp_s/os/data (游客/列表/详情)
    method="POST": 全部 6 个字段(含 data) 拼成一个 JSON body 用 dio.post(json:) 发
                   (账号登录/发验证码等账号接口 —— 实测铁证: 分开放 body 会 1001, 合并成 JSON body 才通)
    """
    token = token if token is not None else _STATE["token"]
    last = {}
    for k in range(retry):
        try:
            ts = str(int(time.time()))
            q = {"version": "1.2.11",
                 "auth": hashlib.md5((ts + C.APPKEY).encode()).hexdigest(),
                 "once": hashlib.md5(("once" + str(uuid.uuid4())).encode()).hexdigest(),
                 "auth_timestamp_s": ts, "os": "android"}
            data_enc = _enc(json.dumps(biz, separators=(',', ':')))
            # 关键: proxies=None 覆盖掉 macOS 系统代理(clash), 否则 requests 全部 ProxyError
            _px = {"http": None, "https": None}
            if method.upper() == "POST":
                q["data"] = data_enc              # 全部字段合并成一个 JSON body
                r = requests.post(C.HOST + path, json=q,
                                  headers={"Authorization": token},
                                  verify=False, timeout=timeout, proxies=_px)
            else:
                q["data"] = data_enc
                r = requests.get(C.HOST + path, params=q,
                                 headers={"Authorization": token},
                                 verify=False, timeout=timeout, proxies=_px)
            return json.loads(_dec(r.text))
        except Exception as e:
            last = {"code": -1, "msg": str(e)}
            if k == retry - 1:
                return last
            time.sleep(2)


def _rand_device():
    """本地随机设备指纹: 让服务端视作全新设备 -> 新账号 -> 新额度。"""
    MANUF = ["samsung", "Xiaomi", "OPPO", "vivo", "HUAWEI", "Google", "OnePlus", "realme", "HONOR"]
    DEV = ["dm3q", "umi", "PDEM10", "M2102K1C", "ELS-AN00", "kona", "redfin", "missi", "RVL-AN00"]
    MODELS = ["SM-G998B", "Mi 11", "PEEM00", "V2049A", "ELS-AN00", "Pixel 6", "LE2120", "RMX3366", "ANY-AN00"]
    i = random.randrange(len(MANUF))
    brand, device, model = MANUF[i], DEV[i], MODELS[i]
    dev_id = uuid.uuid4().hex
    dev = {"brand": brand, "model": model, "device": device, "os": "android", "manufacturer": brand}
    machine = "%s_%s" % (brand, device)
    osv = "UE1A.230829.050_14_34"
    return dev_id, dev, machine, osv


def new_guest():
    """本地造随机设备号登录, 拿 access_token。空 token(限流)时退避重试。"""
    for attempt in range(5):
        dev_id, dev, machine, osv = _rand_device()
        body = {"device_id": dev_id, "device": json.dumps(dev), "source": "android",
                "machine_info": machine, "os_version": osv}
        try:
            r = api("/api/tourist_login", body, token="")
        except Exception:
            r = {}
        at = (r.get("data") or {}).get("access_token") or ""
        if len(at) >= 20:
            print("[*] 新设备号: device_id=%s machine=%s" % (dev_id[:12], machine), flush=True)
            return "bearer " + at
        print("[!] new_guest 空token (attempt %d code=%s msg=%s), 退避重试" %
              (attempt + 1, r.get("code"), r.get("msg")), flush=True)
        time.sleep(1.2 * (attempt + 1))
    return "bearer "


def ensure_token(force=False):
    with _TOKEN_LOCK:
        if force or not _STATE["token"] or len(_STATE["token"]) < 20:
            try:
                _STATE["token"] = new_guest()
                print("[*] guest token ready:", _STATE["token"][:20], flush=True)
            except Exception as e:
                print("[!] ensure_token err", e, flush=True)
        return _STATE["token"]


def switching():
    return bool(_STATE.get("switching"))


def is_account_active():
    """当前全局 token 是否是账号(会员)身份. 会员身份不看次数、永不自动换号."""
    acc = _ACC.get("token", "")
    return bool(acc) and _STATE.get("token") == acc


def switch_guest_async():
    """异步换新游客号 (已在换号则忽略)。"""
    with _SWITCH_LOCK:
        if _STATE["switching"]:
            return
        _STATE["switching"] = True

    def _worker():
        try:
            print("[*] 换号开始", flush=True)
            tok = ""
            try:
                tok = new_guest()
            except Exception as e:
                print("[!] 换号异常:", e, flush=True)
            if len(tok) > 20:
                with _TOKEN_LOCK:
                    _STATE["token"] = tok
                print("[*] 换号完成", tok[:20], flush=True)
            else:
                print("[!] 换号失败(空token), 保留旧token", flush=True)
        finally:
            _STATE["switching"] = False
    threading.Thread(target=_worker, daemon=True).start()


# ---- 业务便捷封装 ----
def platforms():
    ensure_token()
    r = api("/api/live/plat/list", {"module": "live"})
    return {"ok": True, "data": (r.get("data") or {}).get("list", [])}


_ROOMS_CACHE = {}          # pid -> (ts, result)  房间列表短缓存: 上游限频/抖动时秒回
_ROOMS_TTL = 20            # 秒

def rooms(pid=449, room_type=0, kw=""):
    key = (int(pid), int(room_type), str(kw or ""))
    hit = _ROOMS_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _ROOMS_TTL:
        return hit[1]
    ensure_token()
    biz = {"id": int(pid), "room_type": int(room_type), "page": 1, "page_size": 1000}
    if kw:
        biz["keyword"] = kw
    r = api("/api/live/plat/detail", biz)
    d = r.get("data") or {}
    arr = d.get("room_list", [])
    code = r.get("code")
    if not arr and code not in (200, 20000):
        busy = ("频繁" in (r.get("msg") or "")) or ("Token" in (r.get("msg") or ""))
        # 上游限频/抖动时回退最近一次成功列表(不超过 90s), 避免 UI 直接报错
        if hit and now - hit[0] < 90:
            return hit[1]
        return {"ok": False, "data": [], "count": 0, "limited": busy,
                "msg": r.get("msg") or ("code=%s" % code)}
    out = {"ok": True, "data": arr, "count": d.get("room_count") or len(arr)}
    _ROOMS_CACHE[key] = (now, out)
    return out


def room_detail_raw(room_id, plat_id):
    return api("/api/live/room/detail", {"id": int(room_id), "plat_id": int(plat_id)})


def room_detail(room_id, plat_id):
    ensure_token()
    rd = room_detail_raw(room_id, plat_id)
    if rd.get("code") not in (200, 20000):
        return {"ok": False, "msg": rd.get("msg") or ("code=%s" % rd.get("code"))}
    d = rd.get("data") or {}
    playable = bool(d.get("real_room_id") or d.get("current_room_id"))
    return {"ok": True, "playable": playable, "vip_wall": not playable,
            "name": d.get("name"), "real_room_id": d.get("real_room_id"),
            "cover": d.get("cover") or d.get("bg_img")}


# ============================================================
# 账号登录 (POST 接口, 明文 AES+md5, 与游客登录同源, 不碰 libkiwi)
# ============================================================
# 说明: 所有账号相关接口都是 POST, 全部字段(含 data) 合并成一个 JSON body (dio.post(json:)).
# 返回体解密后 code==200(success) / 20000, 其它为错误码.



def _java_hash(s):
    """复刻 Java String.hashCode(): 有符号 32 位."""
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - 0x100000000 if h >= 0x80000000 else h


def gen_device_id(seed=None):
    """复刻 App 的 device 字段 = MyDeviceIdUtils.getDeviceId() = MD5(getUniquePsuedoID()+imei/androidId).

    getUniquePsuedoID() = new UUID(str.hashCode(), serial.hashCode()).toString()
      其中 str = "35"+各Build字段长度%10, serial = Build.SERIAL.
    账号登录 /api/login 的 device 字段就是这个 32 位 hex 字符串 (不是 JSON 对象!).
    随机造一组合法 Build 字段 + 随机 androidId 即得全新合法设备号.
    """
    import random as _r
    b = {"BOARD": "goldfish", "BRAND": "google", "CPU_ABI": "arm64-v8a", "DEVICE": "redfin",
         "DISPLAY": "AP1A", "HOST": "android", "ID": "RQ1A", "MANUFACTURER": "Google",
         "MODEL": "Pixel 6", "PRODUCT": "redfin", "TAGS": "release-keys",
         "TYPE": "user", "USER": "android"}
    order = ["BOARD", "BRAND", "CPU_ABI", "DEVICE", "DISPLAY", "HOST", "ID",
             "MANUFACTURER", "MODEL", "PRODUCT", "TAGS", "TYPE", "USER"]
    s = "35" + "".join(str(len(b[k]) % 10) for k in order)
    if seed:
        serial = seed
    else:
        serial = "".join(_r.choice("0123456789ABCDEF") for _ in range(16))
    hi = _java_hash(s) & 0xFFFFFFFFFFFFFFFF
    lo = _java_hash(serial) & 0xFFFFFFFFFFFFFFFF
    pseudo = str(uuid.UUID(int=(hi << 64) | lo))
    android_id = "%016x" % _r.getrandbits(64)
    return hashlib.md5((pseudo + android_id).encode()).hexdigest()


def _acc_device():
    """账号登录用 device 字段 = device_id 字符串(MD5 hex)."""
    return gen_device_id()


def send_code(mobile=None, email=None, code_type=None):
    """发验证码. /api/common/sendCode  (POST)

    实测铁证: type 是**字符串** "mobile"/"email"(不是数字).
      - 手机: {"type":"mobile","mobile":<号>}  => code 200 success (短信已发)
      - 邮箱: {"type":"email","email":<邮箱>}  => code 200 / 1002 账号不存在
    返回 dict(原始解密响应).
    """
    if email:
        biz = {"type": "email", "email": email}
    else:
        biz = {"type": "mobile", "mobile": mobile}
    return api("/api/common/sendCode", biz, method="POST")


def login_account(mobile=None, email=None, password="", verify_code="",
                  login_type="mobile", token=None):
    """账号登录. /api/login  (POST)

    字段: source="android", device, login_type, mobile/email, password, token(验证码)
    实测铁证: login_type 是**字符串** "mobile"(手机号登录) / "email"(邮箱登录),
              不是数字 (传数字服务端回 code=1001「请选择登陆方式」).
    mobile/email 二选一; password=密码; verify_code→token 字段(验证码登录时).
    成功时把 access_token 存入 _ACC 并返回完整响应 dict.
    """
    dev = _acc_device()
    biz = {"source": "android", "device": dev,
           "login_type": str(login_type)}
    if mobile:
        biz["mobile"] = mobile
    if email:
        biz["email"] = email
    if password:
        biz["password"] = password
    if verify_code:
        biz["token"] = verify_code
    r = api("/api/login", biz, token="", method="POST")
    data = r.get("data") or {}
    at = data.get("access_token") or data.get("token") or ""
    if at:
        tok = at if str(at).lower().startswith("bearer ") else ("bearer " + str(at))
        _ACC["token"] = tok
        # 记录展示信息(账号/昵称, 若返回)
        _ACC["info"] = {
            "mobile": data.get("mobile") or mobile or "",
            "email": data.get("email") or email or "",
            "nickname": data.get("nickname") or data.get("name") or "",
            "login_type": str(login_type),
            "login_at": int(time.time()),
        }
        # 登录即会员: 直接把账号 token 设为全局身份, 免次数限制
        with _TOKEN_LOCK:
            _STATE["token"] = tok
        _save_account()
        print("[*] 账号登录成功并切换为会员身份, token:", tok[:24], flush=True)
    else:
        print("[!] 账号登录未返回 token, code=%s msg=%s" % (r.get("code"), r.get("msg")), flush=True)
    return r


def account_token():
    """取当前账号 token (账号登录成功后可用, 用于后续以会员身份调接口)."""
    return _ACC["token"]


def use_account_token():
    """把账号 token 设为全局 _STATE token, 之后 api()/rooms()/room_detail() 全部以会员身份."""
    if _ACC["token"]:
        with _TOKEN_LOCK:
            _STATE["token"] = _ACC["token"]
        _save_account()
        return True
    return False


def logout_account():
    """退出账号登录: 清 _ACC 并删除持久化文件."""
    with _TOKEN_LOCK:
        _ACC["token"] = ""
        _ACC["info"] = {}
    _del_acc_file()
    return True


def _del_acc_file():
    """删除账号态持久化文件(如存在)."""
    try:
        if os.path.exists(_ACC_FILE):
            os.remove(_ACC_FILE)
    except Exception as e:
        print("[!] 删除账号态文件失败:", e, flush=True)


def use_guest():
    """切回游客身份(退出登录后): 若无游客 token 则新申请一个."""
    with _TOKEN_LOCK:
        if not _STATE.get("token"):
            _STATE["token"] = new_guest()
    return True


# 启动时恢复账号登录态(必须在所有定义之后)
_load_account()
