#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多彩纯 Python 游客登录。

算法已由同一次 App 登录的 HTTP 头/密文/明文对拍确认：
  path = base64url(encrypt_path("login/quick"))
  body = Rabbit(md5(FIXED_KEY_STR + unix_hour).digest(), random_iv8, protobuf)
  iv8  = base64.b64decode(HTTP 请求头 cipher)

不读取 App 的 JWT，也不需要本地 relay。当前网络层仍复用 duocai_pure 的
业务首包；首包来源将在 pure_bootstrap_v2 的 route 模板接入后彻底移除。
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "loginlib"))

from crypto import (FIXED_KEY_BYTES, Rabbit, generate_apns_code,
                    generate_client_token)
from duocai_pure import DuocaiPure

CHECKSUM = b"618fb21db0b0956bd4335b80fc931cec"
PACKAGE = "com.dwdedmq.glvcbdiwubby"


def rabbit_key(hour=None):
    if hour is None:
        hour = int(time.time()) // 3600
    salt = FIXED_KEY_BYTES.decode("utf-8")
    return hashlib.md5((salt + str(hour)).encode("utf-8")).digest()


def rabbit_crypt(key, iv, data):
    cipher = Rabbit()
    cipher.setup_key(key)
    cipher.setup_iv(iv)
    stream = cipher.key_stream(len(data))
    return bytes(a ^ b for a, b in zip(data, stream))


def _xor(data, key):
    return bytes(v ^ key[i % len(key)] for i, v in enumerate(data))


def encrypt_login_path(path="login/quick", iv1=None, iv2=None):
    iv1 = iv1 or os.urandom(11)
    iv2 = iv2 or os.urandom(13)
    inner = _xor(path.encode(), iv1)[::-1]
    x = _xor(iv1 + inner, iv2)
    return base64.urlsafe_b64encode(x[::-1] + iv2).decode()


def decrypt_login_path(value):
    raw = base64.urlsafe_b64decode(value)
    x = raw[:-13][::-1]
    iv2 = raw[-13:]
    y = _xor(x, iv2)
    iv1, inner = y[:11], y[11:][::-1]
    return _xor(inner, iv1).decode()


def _varint(value):
    out = bytearray()
    while True:
        b = value & 0x7f
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _pb_bytes(field, value):
    value = value.encode() if isinstance(value, str) else value
    return _varint(field << 3 | 2) + _varint(len(value)) + value


def _pb_int(field, value):
    return _varint(field << 3) + _varint(value)


def build_guest_protobuf(uuid_str, model="google_sdk_gphone64_arm64"):
    """字段布局直接来自已解密的 280B login/quick 请求。"""
    body = b"".join([
        _pb_bytes(1, b""),
        _pb_bytes(2, b""),
        _pb_bytes(3, b""),
        _pb_bytes(4, generate_apns_code(uuid_str)),
        _pb_bytes(5, PACKAGE),
        _pb_bytes(6, "8.6.7"),
        _pb_bytes(7, "20261102001"),
        _pb_bytes(8, "android"),
        _pb_bytes(10, "tui1"),
        _pb_bytes(11, b""),
        _pb_bytes(12, model),
        _pb_bytes(13, "sdk_gphone64_arm64-userdebug 14 UE1A.230829.050 12077443 dev-keys_14_34"),
        _pb_int(14, 0),
        _pb_bytes(15, "多彩"),
        _pb_bytes(16, generate_client_token(uuid_str)),
    ])
    assert len(body) == 280, len(body)
    return body


def _read_varint(data, offset):
    value = shift = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        value |= (b & 0x7f) << shift
        if b < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid protobuf varint")


def parse_protobuf(data):
    """无 .proto 依赖的字段投影，保留全部原始 bytes。"""
    fields = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field, wire = key >> 3, key & 7
        if not field:
            raise ValueError("invalid protobuf field 0")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            value = data[offset:offset + size]
            if len(value) != size:
                raise ValueError("truncated protobuf bytes")
            offset += size
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire)
        fields.append((field, wire, value))
    return fields


def extract_strings(data, prefix=b"", depth=0):
    strings = []
    try:
        fields = parse_protobuf(data)
    except (ValueError, IndexError):
        return strings
    for field, wire, value in fields:
        if wire != 2:
            continue
        if value.startswith(prefix) and all(32 <= b < 127 for b in value):
            strings.append(value)
        if depth < 4:
            strings.extend(extract_strings(value, prefix, depth + 1))
    return strings


def guest_login(client, uuid_str=None, collect=10.0):
    uuid_str = uuid_str or os.urandom(16).hex()
    plain = build_guest_protobuf(uuid_str)
    key = rabbit_key()
    iv = os.urandom(8)
    rabbit = Rabbit()
    rabbit.setup_key(key)
    rabbit.setup_iv(iv)
    encrypted = bytes(a ^ b for a, b in zip(plain, rabbit.key_stream(len(plain))))
    path = ("/go/" + encrypt_login_path() + "?z=1&nobase64=1").encode()
    headers = [
        (b"checksum", CHECKSUM),
        (b"cipher", base64.b64encode(iv)),
    ]
    status, response_headers, response_body = client.request(
        path, encrypted, collect=collect,
        content_type=b"application/proto", extra_headers=headers, chunked=False,
        body_encrypted=True)
    result = {
        "uuid": uuid_str,
        "client_token": generate_client_token(uuid_str),
        "status": status,
        "headers": [x.decode("latin1") for x in response_headers],
        "request_key": key.hex(),
        "request_iv": base64.b64encode(iv).decode(),
        "request_plaintext": plain,
        "response_ciphertext": response_body,
    }
    if not response_body:
        result["error"] = "empty response"
        return result
    try:
        # 服务端沿用请求的 Rabbit 状态：请求已消费 280B 密钥流。
        response_plain = bytes(a ^ b for a, b in zip(
            response_body, rabbit.key_stream(len(response_body))))
        if response_plain[:2] == b"\x1f\x8b":
            response_plain = gzip.decompress(response_plain)
        result["response_plaintext"] = response_plain
        tokens = extract_strings(response_plain, b"eyJ")
        if tokens:
            result["jwt"] = tokens[0].decode()
            client.jwt = tokens[0]
        else:
            # 某些响应可能直接是 JSON。
            try:
                obj = json.loads(response_plain.decode())
                result["response_json"] = obj
                for k in ("jwt", "token", "access_token"):
                    if isinstance(obj, dict) and isinstance(obj.get(k), str):
                        result["jwt"] = obj[k]
                        client.jwt = obj[k].encode()
                        break
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    except Exception as exc:
        result["decrypt_error"] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description="多彩纯 Python 游客登录")
    parser.add_argument("--host", default="159.75.78.225")
    parser.add_argument("--port", type=int, default=9836)
    parser.add_argument("--first-packet", default=str(HERE / "biz_pkt.hex"))
    parser.add_argument("--uuid", help="32 位十六进制设备 ID；默认随机生成")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--save-session", default=str(HERE / "guest_session.json"))
    args = parser.parse_args()

    first_packet = Path(args.first_packet).read_text().strip()
    client = DuocaiPure(first_packet, args.host, args.port).connect()
    print("隧道 conv=%#x xor=%#x sn=%d" % (client.conv, client.key, client.maxsn))
    result = guest_login(client, args.uuid, args.timeout)
    print("HTTP", result["status"])
    for line in result["headers"]:
        print(" ", line)
    plain = result.get("response_plaintext")
    if plain is not None:
        print("响应明文(%dB):" % len(plain), repr(plain[:1000]))
    else:
        print("响应密文(%dB):" % len(result["response_ciphertext"]),
              result["response_ciphertext"][:200].hex())
    if result.get("jwt"):
        (HERE / "jwt.txt").write_text(result["jwt"])
        print("JWT 已写入", HERE / "jwt.txt")

    serializable = {}
    for k, v in result.items():
        if isinstance(v, bytes):
            serializable[k] = {"hex": v.hex(), "utf8": v.decode("utf-8", "replace")}
        else:
            serializable[k] = v
    Path(args.save_session).write_text(json.dumps(serializable, ensure_ascii=False, indent=2))
    print("会话已保存", args.save_session)
    return 0 if result.get("jwt") else 2


if __name__ == "__main__":
    raise SystemExit(main())
