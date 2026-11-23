#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鲍鱼纯 Python libkiwi WebSocket 客户端：不经 App、本地 relay 或浏览器。

9875 UDP = AES-128-CFB + KCP 变体 + XOR(route[18]) + HTTP/WS + Rabbit。
每次请求新建 conv，并用当次 bootstrap session 填 route[32:40]。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUOCAI_RE = Path("/Volumes/vol1T/project/cams/duocai/02_模拟器/duocai_py/kiwi_re")
import sys
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(DUOCAI_RE))
sys.path.insert(0, str(DUOCAI_RE / "loginlib"))
sys.path.insert(0, str(DUOCAI_RE / "pure_client"))

import duocai_pure as DP
import pure_guest_login as G
from rabbit import RabbitCipher

HOST = "162.14.199.148"
PORT = 9875
# 鲍鱼 9875 WebSocket 路由；[32:40] 每次连接必须替换为本次 bootstrap session。
LOGIN_ROUTE = bytearray(bytes.fromhex(
    "a1b243040000002678c977146afd21090300d1d1000000e200000000"
    "000000003edd8ef6d076ad43a1552b13bb88b4d051a7b01d78563411"
))
FIXED_KEY = bytes(int(x) & 0xff for x in (
    "106,81,66,85,79,38,50,112,102,83,53,115,67,227,128,141,194,"
    "165,63,65,116,103,108,88,118,42,119,43,80,99,82,75,56,37,105"
).split(","))


def rabbit_key(jwt, hour=None):
    if hour is None:
        hour = int(time.time()) // 3600
    salt = FIXED_KEY.decode("utf-8", "surrogateescape")
    return hashlib.md5((salt + str(hour) + jwt).encode("utf-8", "surrogateescape")).digest()


def rabbit_crypt(key, iv, data, offset=0):
    """Rabbit 是字节流；offset>0 时只初始化一次并跳过前 offset 字节。"""
    cipher = RabbitCipher()
    cipher.setup_key(key)
    cipher.setup_iv(iv)
    stream = cipher.key_stream(offset + len(data))[offset:]
    return bytes(a ^ b for a, b in zip(data, stream))


def xor(data, key):
    return bytes(value ^ key for value in data)


def encrypt_path(path):
    iv1, iv2 = os.urandom(11), os.urandom(13)
    inner = xor_multi(path.encode(), iv1)[::-1]
    packed = xor_multi(iv1 + inner, iv2)
    # URL path 使用 base64url；标准 base64 的 +/ 会被服务器判为非法路径。
    return base64.urlsafe_b64encode(packed[::-1] + iv2).decode()


def xor_multi(data, key):
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(data))


def varint(number):
    out = bytearray()
    while True:
        byte = number & 0x7f
        number >>= 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def pb_int(field, value):
    return varint(field << 3) + varint(int(value))


def pb_bytes(field, value):
    value = value.encode() if isinstance(value, str) else value
    return varint((field << 3) | 2) + varint(len(value)) + value


def build_request(random_id, method, path, data):
    return pb_int(1, random_id) + pb_int(2, method) + pb_bytes(3, path) + pb_bytes(4, data)


def parse_protobuf(buffer):
    result, index = {}, 0
    while index < len(buffer):
        key, shift = 0, 0
        while True:
            byte = buffer[index]
            index += 1
            key |= (byte & 0x7f) << shift
            shift += 7
            if not byte & 0x80:
                break
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, shift = 0, 0
            while True:
                byte = buffer[index]
                index += 1
                value |= (byte & 0x7f) << shift
                shift += 7
                if not byte & 0x80:
                    break
            result[field] = value
        elif wire == 2:
            length, shift = 0, 0
            while True:
                byte = buffer[index]
                index += 1
                length |= (byte & 0x7f) << shift
                shift += 7
                if not byte & 0x80:
                    break
            result[field] = buffer[index:index + length]
            index += length
        else:
            break
    return result


def ws_frame(payload):
    """客户端到服务端：FIN+binary，必须带 mask。"""
    mask = os.urandom(4)
    masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    length = len(payload)
    if length < 126:
        header = bytes((0x82, 0x80 | length))
    elif length <= 0xffff:
        header = bytes((0x82, 0xfe)) + struct.pack(">H", length)
    else:
        header = bytes((0x82, 0xff)) + struct.pack(">Q", length)
    return header + mask + masked


def iter_ws_frames(data):
    index = 0
    while index + 2 <= len(data):
        first, second = data[index], data[index + 1]
        opcode, masked = first & 0x0f, bool(second & 0x80)
        length, cursor = second & 0x7f, index + 2
        if length == 126:
            if cursor + 2 > len(data):
                return
            length = struct.unpack(">H", data[cursor:cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if cursor + 8 > len(data):
                return
            length = struct.unpack(">Q", data[cursor:cursor + 8])[0]
            cursor += 8
        mask = data[cursor:cursor + 4] if masked else b""
        if masked:
            cursor += 4
        if cursor + length > len(data):
            return
        payload = data[cursor:cursor + length]
        if masked:
            payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        yield opcode, payload
        index = cursor + length


class KiwiWS:
    def __init__(self, jwt, host=HOST, port=PORT, timeout=10.0):
        self.jwt = jwt
        self.host, self.port = host, port
        self.timeout = timeout
        self.session, self.pool = G.bootstrap_session()
        self.route = bytearray(LOGIN_ROUTE)
        self.route[32:40] = self.session
        self.key = self.route[18]
        self.conv = struct.unpack("<I", os.urandom(4))[0]
        self.ts = int(time.time() * 1000) & 0xffffffff
        self.iv = os.urandom(8)
        self.cipher_key = rabbit_key(jwt)
        # 必须与 App 一样保留 Rabbit 对象：每次 key_stream() 调用都有自己的
        # 16B/4B 字选择节奏，不能用“一次性长流再切片”模拟连续调用。
        self.send_cipher = RabbitCipher()
        self.send_cipher.setup_key(self.cipher_key)
        self.send_cipher.setup_iv(self.iv)
        self.recv_cipher = RabbitCipher()
        self.recv_cipher.setup_key(self.cipher_key)
        self.recv_cipher.setup_iv(self.iv)
        self.send_offset = 0
        self.recv_offset = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(0.5)
        self.socket.connect((host, port))
        self.data = {}
        self.handshake_done = False
        self.next_sn = 5
        self.frame_cursor = 0
        self.server_sns = set()

    def close(self):
        self.socket.close()

    def _segment(self, command, sn, payload=b"", una=0):
        return struct.pack("<IBBHIIII", self.conv, command, 0, 0x1000,
                           self.ts, sn, una, len(payload)) + payload

    def _send_segments(self, segments):
        stream = b"".join(self._segment(0x51, sn, payload) for sn, payload in segments)
        packet = struct.pack("<I", zlib.crc32(stream) & 0xffffffff) + stream
        salt = os.urandom(16)
        self.socket.send(salt + DP.enc(packet, salt))

    def _ack(self, sns):
        """KCP 变体仍要求 ACK；否则服务器发完 login 后会等待，不处理下一请求。"""
        if not sns:
            return
        una = max(self.server_sns) + 1 if self.server_sns else 0
        ts = int(time.time() * 1000) & 0xffffffff
        stream = b"".join(struct.pack("<IBBHIIII", self.conv, 0x52, 0, 0x1000,
                                     ts, sn, una, 0) for sn in sns)
        packet = struct.pack("<I", zlib.crc32(stream) & 0xffffffff) + stream
        salt = os.urandom(16)
        self.socket.send(salt + DP.enc(packet, salt))

    def _receive(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                packet = self.socket.recv(65535)
            except socket.timeout:
                continue
            try:
                plain = DP.dec(packet[16:], packet[:16])
            except Exception:
                continue
            new_sns = []
            for segment in DP.parse_kcp(plain):
                if segment["conv"] != self.conv or segment["cmd"] != 0x51 or not segment["payload"]:
                    continue
                sn = segment["sn"]
                if sn not in self.server_sns:
                    self.server_sns.add(sn)
                    new_sns.append(sn)
                payload = segment["payload"]
                if payload[:2] == b"\x57\x03" and len(payload) >= 4:
                    length = struct.unpack(">H", payload[2:4])[0]
                    decoded = xor(payload[4:4 + length], self.key)
                elif payload[:2] in (b"\x57\x01", b"\x57\x02", b"\x57\x04", b"\xa1\xb2"):
                    decoded = b""
                else:
                    decoded = xor(payload, self.key)
                # 同 sn 重传只保留首个完整段；避免拼接重复 101 头。
                self.data.setdefault(sn, decoded)
            self._ack(new_sns)

    def connect(self):
        path = encrypt_path("/ws/chat")
        header = (
            "GET /go/%s?z=1&nobase64=1 HTTP/1.1\r\n"
            "user-agent: Dart/3.5 (dart:io)\r\ncache-control: no-cache\r\n"
            "authorization: Bearer %s\r\ncipher: %s\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
            % (path, self.jwt, base64.b64encode(self.iv).decode(),
               base64.b64encode(os.urandom(16)).decode())
        ).encode()
        header_x = xor(header, self.key)
        self._send_segments([
            (0, b"\x57\x01\x00\x00"),
            (1, b"\x57\x03\x00\x38"),
            (2, bytes(self.route)),
            (3, b"\x57\x03" + struct.pack(">H", len(header_x))),
            (4, header_x),
        ])
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            self._receive(0.5)
            raw = b"".join(self.data[sn] for sn in sorted(self.data))
            if b"HTTP/1.1 101 Switching Protocols" in raw:
                self.handshake_done = True
                return True
        return False

    def request(self, path, body=b"", random_id=1):
        if not self.handshake_done:
            raise RuntimeError("WebSocket 握手未完成")
        request = build_request(random_id, 3, path, body)
        encrypted = bytes(a ^ b for a, b in zip(request, self.send_cipher.key_stream(len(request))))
        self.send_offset += len(request)
        body_x = xor(ws_frame(encrypted), self.key)
        self._send_segments([
            (self.next_sn, b"\x57\x03" + struct.pack(">H", len(body_x))),
            (self.next_sn + 1, body_x),
        ])
        self.next_sn += 2
        # 同一 WebSocket 可连续发 login/init 与 generate；只消费尚未读的新帧。
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            self._receive(0.5)
            raw = b"".join(self.data[sn] for sn in sorted(self.data))
            marker = raw.find(b"HTTP/1.1 101 Switching Protocols")
            if marker < 0:
                continue
            tail_start = raw.find(b"\r\n\r\n", marker)
            if tail_start < 0:
                continue
            frames = list(iter_ws_frames(raw[tail_start + 4:]))
            while self.frame_cursor < len(frames):
                opcode, payload = frames[self.frame_cursor]
                self.frame_cursor += 1
                if opcode != 2 or not payload:
                    continue
                plain = bytes(a ^ b for a, b in zip(payload, self.recv_cipher.key_stream(len(payload))))
                self.recv_offset += len(payload)
                parsed = parse_protobuf(plain)
                # 服务器会插入 field5 的控制/心跳 protobuf；响应以 random_id 对齐。
                if parsed.get(1) != random_id:
                    continue
                return parsed, plain
        raise TimeoutError("未收到 %s 的 WebSocket 响应" % path)


def generate_agora_token(jwt, channel, user_id, appver="20260226001", apns=G.APNS_CONST):
    client = KiwiWS(jwt)
    try:
        if not client.connect():
            raise TimeoutError("9875 WebSocket 握手超时")
        # login/init 的 field1 是 64B base64 apns；空值会让服务器进入“未登录”状态。
        login_data = pb_bytes(1, apns) + pb_bytes(2, jwt) + pb_bytes(3, appver)
        login_response, _ = client.request("/login/init", login_data, random_id=1)
        agora_data = pb_bytes(2, str(channel)) + pb_int(3, int(user_id))
        response, plain = client.request("/stream/generate_agora_token", agora_data, random_id=2)
        body = response.get(2, b"")
        inner = parse_protobuf(body)
        token = inner.get(2, b"")
        if isinstance(token, bytes):
            token = token.decode("utf-8", "replace")
        # 失败时 body = {1: {1: business_code, 2: message, 3: ...}}。
        wrapper = parse_protobuf(inner.get(1, b""))
        message = wrapper.get(2, b"")
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        return {"login": login_response, "status": response.get(3), "token": token,
                "business_code": wrapper.get(1), "message": message,
                "response": response, "plaintext": plain}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("channel")
    parser.add_argument("user_id", type=int)
    parser.add_argument("--jwt-file", default="/tmp/baoyu_jwt.txt")
    parser.add_argument("--appver", default="20260226001")
    args = parser.parse_args()
    jwt = Path(args.jwt_file).read_text().strip()
    result = generate_agora_token(jwt, args.channel, args.user_id, args.appver)
    # 默认输出完整结果，便于调用方把 appid/channel/token 直接喂给播放器。
    result["channel"] = args.channel
    result["uid"] = args.user_id
    result.pop("plaintext", None)
    print(json.dumps(result, ensure_ascii=False, default=lambda value: value.hex() if isinstance(value, bytes) else str(value)))


if __name__ == "__main__":
    main()
