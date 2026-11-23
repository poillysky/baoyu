# -*- coding: utf-8 -*-
"""
stream.py — 纯 Python Agora token 引擎（脱 App 版）

默认直连 162.14.199.148:9875：KCP 隧道 -> WebSocket -> Rabbit ->
login/init -> /stream/generate_agora_token。无需 App 进程、adb forward 或本地 relay。
身份(jwt/apns/user_id)保存在 backend/kiwi_identity.json；首次可从现有 App
配置导入一次，之后仅使用本地文件。channel 必须是 room/detail 的 real_room_id。
"""
import os, re, subprocess, threading, time, json

ADB = "/Users/poilly/Library/Android/sdk/platform-tools/adb"
EMU = "emulator-5554"
PKG = "me.hq80tw8.ws04i1m"
PREFS = "/data/data/%s/shared_prefs/demo_prefs.xml" % PKG
AGORA_APPID_REAL = "84437b2ceb75422ab1f7df9c2689241d"

_LOCK = threading.Lock()


def _sh(*a, timeout=30):
    try:
        return subprocess.run([ADB, "-s", EMU] + list(a),
                              capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def read_creds():
    """从 demo_prefs 读 jwt/apns/uid (App 当前游客身份)"""
    xml = _sh("shell", "cat %s" % PREFS)
    def grab(k):
        m = re.search(r'name="%s">([^<]+)' % k, xml)
        return m.group(1) if m else None
    jwt = grab("guest_token"); apns = grab("apns_code"); uid = grab("user_id")
    return jwt, apns, uid


def discover_port():
    """从 logcat 找 libkiwi WS 网关端口 (AgoraPlayManager: Kiwi port=NNN)"""
    log = _sh("logcat", "-d")
    m = re.findall(r'AgoraPlayManager: Kiwi port=(\d+)', log)
    if not m:
        # 兜底: 扫 App 监听的高位端口(排除 5555/8872)
        return None
    return int(m[-1])


def _ensure_app_alive():
    """确保 App 进程活着(libkiwi 网关在)。死则冷启动。"""
    pid = _sh("shell", "pidof %s" % PKG).strip()
    if pid:
        return pid
    _sh("shell", "am start -n %s/com.example.untitled.MainActivity" % PKG)
    time.sleep(7)
    # 关欢迎弹窗
    _sh("shell", "input tap 540 1579")
    time.sleep(1)
    return _sh("shell", "pidof %s" % PKG).strip()


def ensure_gateway():
    """确保 libkiwi 网关已建立并返回可用端口。若还没网关(需先'进过一次房'初始化过),
       则触发一次最小初始化: 冷启动App后 libkiwi 在首次进房才 ServerToLocal。
       返回 (port, jwt, apns, uid) 或抛错。"""
    _ensure_app_alive()
    port = discover_port()
    jwt, apns, uid = read_creds()
    if not (jwt and apns and uid):
        raise RuntimeError("读不到 App 游客身份(demo_prefs 缺字段), 请让 App 先完成游客登录")
    if not port:
        raise RuntimeError("未发现 libkiwi 网关端口; App 需先成功进过一次房以初始化 libkiwi "
                           "(可用 warmup())")
    _sh("forward", "tcp:%d" % port, "tcp:%d" % port)
    return port, jwt, apns, uid


def warmup():
    """初始化 libkiwi 网关(首次需要): 确保 App 有一间能进的成功房, 让 libkiwi ServerToLocal。
       这里只做: 启动 App + 点推荐页最近成功免费房的坐标；若 libkiwi 已在则秒返回。"""
    if discover_port():
        return True
    _ensure_app_alive()
    _sh("logcat", "-c")
    # 点推荐页第一格免费房(270,900), 触发一次进房 -> 初始化 libkiwi
    _sh("shell", "input tap 131 212")  # 回推荐tab
    time.sleep(1)
    _sh("shell", "input tap 270 900")
    # 等 libkiwi 初始化
    for _ in range(15):
        time.sleep(1)
        if discover_port():
            return True
    return bool(discover_port())


def _identity_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiwi_identity.json")


def _load_identity():
    """优先读本地身份；仅首次导入时读一次 App 配置并立即持久化。"""
    path = _identity_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            identity = json.load(f)
        if identity.get("jwt") and identity.get("apns") and identity.get("user_id"):
            return identity["jwt"], identity["apns"], str(identity["user_id"])
    except FileNotFoundError:
        pass
    jwt, apns, uid = read_creds()
    if not (jwt and apns and uid):
        raise RuntimeError("本地 kiwi_identity.json 无有效身份，且首次导入未读到 jwt/apns/user_id")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"jwt": jwt, "apns": apns, "user_id": str(uid)}, f, ensure_ascii=False)
    os.replace(tmp, path)
    return jwt, apns, str(uid)


def get_token(channel, user_id=None, retries=2):
    """纯 Python 直连 9875 签 007e token；全程不调用 App relay。"""
    import pure_kiwi_ws as PW
    with _LOCK:
        jwt, apns, uid = _load_identity()
        uid = str(user_id or uid)
        last = None
        for _ in range(retries):
            try:
                result = PW.generate_agora_token(jwt, str(channel), uid, apns=apns)
                token = result.get("token") or ""
                if token.startswith("007e"):
                    return {"appid": AGORA_APPID_REAL, "channel": str(channel),
                            "uid": uid, "token": token}
                last = RuntimeError("generate 返回 code=%s msg=%s" %
                                    (result.get("business_code"), result.get("message")))
            except Exception as exc:
                last = exc
        raise last or RuntimeError("get_token 失败")


if __name__ == "__main__":
    import sys
    ch = sys.argv[1] if len(sys.argv) > 1 else "10003764"
    print("warmup:", warmup())
    print(json.dumps(get_token(ch), ensure_ascii=False))
