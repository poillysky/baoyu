#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多彩 纯 Python 凭空游客登录（不读 App、不读 pcap、不走本地 relay）。

完整链路（全部实证跑通）：
  1) 随机 umid -> DNS -> 8003 引导 -> 拿到本次 session（每次随机）
  2) 本地算 token = md5("token:"+deviceUUID+"_tui1")
  3) apns 用设备常量（同一 deviceUUID 的确定性密文，实测跨次稳定）
  4) 本地构造 280B body(protobuf) -> Rabbit 加密
  5) route 用真实 login route（[2:4]=cf04 业务子类型），[32:40] 填本次 bootstrap 的 session
  6) 一次性发 7 段 KCP：5701 + route + 5703声明(header) + HTTP头 + 5703声明(body) + body
  7) 服务器回 HTTP/1.1 200 + 598B application/proto
  8) 响应 Rabbit 解：skip=288（请求已消费 280B + 8B 响应盐），取出 JWT

关键常量均来自实测字节，非臆造。
"""
import base64
import hashlib
import os
import re
import socket
import struct
import time
import zlib

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loginlib"))

from crypto import FIXED_KEY_BYTES, Rabbit  # noqa: E402
import pure_login as PL  # noqa: E402  （复用其 encrypt_login_path、多字节 _xor）
import duocai_pure as DP  # noqa: E402  （复用 AES-CFB 正确 segment 配置的 enc/dec）
import pure_bootstrap_v2 as B  # noqa: E402

encrypt_login_path = PL.encrypt_login_path

# ------------------------------------------------------------------ 常量
PACKAGE = "com.dwdedmq.glvcbdiwubby"
CHECKSUM = b"618fb21db0b0956bd4335b80fc931cec"      # 固定请求校验

# 本设备持久 deviceUUID（prefs dev_id / flutter.deviceUUID）
DEVICE_UUID = "dcd5e94b-6584-35b6-8eb9-8c1c31de1e77"
# 该 deviceUUID 对应的 apns 确定性密文（跨次抓包实测稳定）
APNS_CONST = "Xh1SHmBE8wbSwk+1VbDJk6upMv5C0W1CzL7+AsBYcQJ12/lrl72aEMPiT6R3ZL3/"

# 真实登录 route 模板（56B 明文，实测原样）。运行时替换 [32:40]=session。[18]=XOR key。
LOGIN_ROUTE = bytearray(bytes.fromhex(
    "a1b2cf040000003579344b546b001dd603001818000001a800000000"
    "00000000347efd49d9408c9ba680ead161154a4f1a9e3e4578563411"
))
LOGIN_KEY = LOGIN_ROUTE[18]  # 0x18，实测固定（route 子类型 cf04 绑定的 XOR key）


def rabbit_key(hour=None):
    if hour is None:
        hour = int(time.time()) // 3600
    return hashlib.md5((FIXED_KEY_BYTES.decode("utf-8") + str(hour)).encode("utf-8")).digest()


def generate_client_token(uuid_str, channel="tui1"):
    return hashlib.md5(("token:" + uuid_str + "_" + channel).encode()).hexdigest()


def _xor(data, key):
    # 多字节轮转 XOR（与 pure_login._xor 一致）：key 为 int 时退化为单字节
    if isinstance(key, int):
        return bytes(v ^ key for v in data)
    return bytes(v ^ key[i % len(key)] for i, v in enumerate(data))


def _varint(value):
    out = bytearray()
    while True:
        b = value & 0x7f
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _pb(field, val):
    if isinstance(val, str):
        val = val.encode()
    return _varint((field << 3) | 2) + _varint(len(val)) + val


def _pbi(field, val):
    return _varint((field << 3) | 0) + _varint(val)


def build_body(uuid=DEVICE_UUID):
    body = b"".join([
        _pb(1, b""), _pb(2, b""), _pb(3, b""),
        _pb(4, APNS_CONST),
        _pb(5, PACKAGE), _pb(6, "8.6.7"), _pb(7, "20261102001"),
        _pb(8, "android"), _pb(10, "tui1"), _pb(11, b""),
        _pb(12, "google_sdk_gphone64_arm64"),
        _pb(13, "sdk_gphone64_arm64-userdebug 14 UE1A.230829.050 12077443 dev-keys_14_34"),
        _pbi(14, 0), _pb(15, "多彩"),
        _pb(16, generate_client_token(uuid)),
    ])
    assert len(body) == 280, len(body)
    return body


# ------------------------------------------------------------- bootstrap
def bootstrap_session(retries=3, timeout=5.0):
    """凭空 bootstrap，返回 (session_bytes8, pool_dict)。"""
    for _ in range(retries):
        umid = B.make_umid()
        try:
            ip = socket.gethostbyname(umid + ".aliyunl.com")
        except Exception:
            ip = "106.55.158.133"
        pkt, _dj = B.build_bootstrap_packet(umid, B.make_android_id())
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(pkt, (ip, 8003))
        segs = []
        t = time.time() + timeout
        while time.time() < t:
            try:
                d, _ = s.recvfrom(65535)
                segs += B.parse_segs(d)
            except Exception:
                break
        s.close()
        pool = B.extract_nodepool(segs)
        if isinstance(pool, dict) and pool.get("session"):
            return base64.b64decode(pool["session"]), pool
    raise RuntimeError("bootstrap 失败：多次未取到 session")


# ------------------------------------------------------------------ KCP
def _seg(conv, sn, payload, ts):
    return struct.pack("<IBBHIIII", conv, 0x51, 0, 0x1000, ts, sn, 0, len(payload)) + payload


def _aes_enc(data, salt):
    return DP.enc(data, salt)


def _aes_dec(data, salt):
    return DP.dec(data, salt)


def _parse_kcp(w, conv):
    """保留：备用从带CRC前缀的解密明文里按conv过滤解析（实际用 DP.parse_kcp）。"""
    segs = []
    i = 4  # 跳过CRC32前缀
    while i + 24 <= len(w):
        cv, cmd, frg, wnd, ts, sn, una, ln = struct.unpack("<IBBHIIII", w[i:i + 24])
        if cv != conv:
            break
        segs.append({"conv": cv, "sn": sn, "payload": w[i + 24:i + 24 + ln]})
        i += 24 + ln
    return segs


def login(host="162.14.199.148", port=9875, uuid=DEVICE_UUID, collect=6.0):
    """凭空游客登录，返回 dict（含 status / jwt / route / ...）。"""
    sess, pool = bootstrap_session()
    route = bytearray(LOGIN_ROUTE)
    route[32:40] = sess                     # 本次 bootstrap session
    key = route[18]

    body = build_body(uuid)
    iv = os.urandom(8)
    rb = Rabbit()
    rb.setup_key(rabbit_key())
    rb.setup_iv(iv)
    body_enc = bytes(a ^ b for a, b in zip(body, rb.key_stream(280)))

    header = (
        "POST /go/%s?z=1&nobase64=1 HTTP/1.1\r\n"
        "user-agent: Dart/3.13 (dart:io)\r\n"
        "accept: application/proto\r\n"
        "accept-encoding: gzip\r\n"
        "content-length: 280\r\n"
        "host: 127.0.0.1:38347\r\n"
        "checksum: %s\r\n"
        "content-type: application/proto\r\n"
        "cipher: %s\r\n\r\n"
        % (encrypt_login_path("login/quick"), CHECKSUM.decode(), base64.b64encode(iv).decode())
    ).encode()
    header_x = _xor(header, key)
    body_x = _xor(body_enc, key)

    conv = struct.unpack("<I", os.urandom(4))[0]
    ts = int(time.time() * 1000) & 0xffffffff
    kcp = b"".join([
        _seg(conv, 0, b"\x57\x01\x00\x00", ts),
        _seg(conv, 1, b"\x57\x03\x00\x38", ts),
        _seg(conv, 2, bytes(route), ts),
        _seg(conv, 3, b"\x57\x03" + struct.pack(">H", len(header_x)), ts),
        _seg(conv, 4, header_x, ts),
        _seg(conv, 5, b"\x57\x03" + struct.pack(">H", len(body_x)), ts),
        _seg(conv, 6, body_x, ts),
    ])
    packet = struct.pack("<I", zlib.crc32(kcp) & 0xffffffff) + kcp

    salt = os.urandom(16)
    out = salt + _aes_enc(packet, salt)

    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.settimeout(1)
    sk.connect((host, port))
    sk.send(out)
    resp = {}
    t0 = time.time()
    while time.time() - t0 < collect:
        try:
            dd = sk.recv(65535)
        except Exception:
            continue
        try:
            dpt = _aes_dec(dd[16:], dd[:16])
        except Exception:
            continue
        for s in [x for x in DP.parse_kcp(dpt) if x["conv"]==conv]:
            resp[s["sn"]] = s["payload"]
    sk.close()

    raw = b""
    for sn in sorted(resp):
        pn = resp[sn]
        if pn[:2] == b"\x57\x03" and len(pn) >= 4:
            ln = struct.unpack(">H", pn[2:4])[0]
            d = _xor(pn[4:4 + ln], key)
            if d[:2] == b"\x76\x22":
                d = d[4:]
            raw += d

    h = raw.find(b"HTTP/1.1")
    result = {"session": sess.hex(), "route": bytes(route).hex(), "segs": len(resp)}
    if h < 0:
        result["status"] = None
        result["raw_head"] = raw[:80].hex()
        return result
    head_end = raw.find(b"\r\n\r\n", h)
    header_blob = raw[h:head_end]
    body_blob = raw[head_end + 4:]
    m = re.match(rb"HTTP/1.1 (\d+)", header_blob)
    result["status"] = int(m.group(1)) if m else None
    result["resp_len"] = len(body_blob)

    # 响应 Rabbit：skip=288（请求已消费 280 + 8B 响应盐）
    if body_blob:
        rb2 = Rabbit()
        rb2.setup_key(rabbit_key())
        rb2.setup_iv(iv)
        ks = rb2.key_stream(288 + len(body_blob))[288:]
        plain = bytes(a ^ b for a, b in zip(body_blob, ks))
        if plain[:2] == b"\x1f\x8b":
            import gzip
            plain = gzip.decompress(plain)
        result["response_plaintext"] = plain
        mm = re.search(rb"eyJ[A-Za-z0-9_\-\.]+", plain)
        result["jwt"] = mm.group().decode() if mm else None
    return result


def main():
    import argparse
    ap = argparse.ArgumentParser(description="多彩纯 Python 凭空游客登录")
    ap.add_argument("--host", default="162.14.199.148")
    ap.add_argument("--port", type=int, default=9875)
    ap.add_argument("--uuid", default=DEVICE_UUID)
    ap.add_argument("--save", default=str(HERE / "guest_jwt.txt"))
    args = ap.parse_args()
    r = login(args.host, args.port, args.uuid)
    print("status:", r.get("status"), "seg:", r.get("segs"), "resp_len:", r.get("resp_len"))
    if r.get("jwt"):
        Path(args.save).write_text(r["jwt"])
        print("JWT:", r["jwt"][:60], "...")
        print("已写入", args.save)
    else:
        print("plaintext:", repr(r.get("response_plaintext", b"")[:120]))
        print("raw_head:", r.get("raw_head"))
    return 0 if r.get("jwt") else 2


if __name__ == "__main__":
    raise SystemExit(main())
