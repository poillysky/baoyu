#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多彩 纯 Python 客户端 (无 App 依赖, 仅需采集一次真实首包)
================================================================
加密体系(已完全破解):
  外层: AES-128-CFB  key=PJkem8euJ42BT09O  IV=每包16B随机salt(明文前置)
  内层: 单字节 XOR, key 明文在 a1b2头[18:20] (每条连接独立)
  帧: 5703+len(大端2B) 长度声明段 + DATA段(XOR后的HTTP)
  KCP变体: 24B段头 conv(4)cmd(1)frg(1)wnd(2)ts(4)sn(4)una(4)len(4)
链路:
  一次真实首包(采自App冷启动, 存first_pkt.hex) --重放--> 握手
  --> 同一conv发 POST /game_xxx --> 服务器执行 --> XOR解 + dechunk + gunzip
"""
import socket, struct, time, os, gzip
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import warnings; warnings.filterwarnings('ignore')

KEY = b"PJkem8euJ42BT09O"
HOST, PORT = "159.75.78.225", 9836

def enc(d, iv):
    c = Cipher(algorithms.AES(KEY), modes.CFB(iv)); e = c.encryptor()
    return e.update(d) + e.finalize()

def dec(d, iv):
    c = Cipher(algorithms.AES(KEY), modes.CFB(iv)); x = c.decryptor()
    return x.update(d) + x.finalize()

def parse_kcp(pt):
    i = 4 if len(pt) > 4 else 0
    S = []
    while i + 24 <= len(pt):
        cv, cmd, frg, wnd, ts, sn, una, ln = struct.unpack('<I B B H I I I I', pt[i:i+24])
        if cmd not in (0x51, 0x52, 0x57): break
        if i + 24 + ln > len(pt): break
        S.append(dict(conv=cv, cmd=cmd, sn=sn, una=una, ts=ts, payload=pt[i+24:i+24+ln]))
        i += 24 + ln
    return S

def dechunk(b):
    out = b""; i = 0
    while i < len(b):
        j = b.find(b"\r\n", i)
        if j < 0: break
        try: sz = int(b[i:j], 16)
        except: break
        if sz == 0: break
        out += b[j+2:j+2+sz]; i = j + 2 + sz + 2
    return out


class DuocaiPure:
    def __init__(self, first_pkt_hex, host=HOST, port=PORT):
        """first_pkt_hex: 一条真实业务连接首包(3段5701/5703/a1b2)的完整wire hex。
        用房列表/游戏列表路由(a1b2 xx04)的首包最通用。"""
        self.host = host; self.port = port
        self.wire = bytes.fromhex(first_pkt_hex)
        iv = self.wire[:16]; pt = dec(self.wire[16:], iv)
        segs = parse_kcp(pt)
        self.conv = segs[0]['conv']
        self.key = None; self.route_seg = None
        for s in segs:
            if s['payload'][:2] == b'\xa1\xb2':
                self.key = s['payload'][18]; self.route_seg = s['payload']; break
        self.sock = None; self.maxsn = 0
        self.jwt = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(3.0); self.sock.connect((self.host, self.port))
        self.sock.send(self.wire)  # 重放真实业务首包握手
        end = time.time() + 2.5
        try:
            while time.time() < end:
                d = self.sock.recv(65535)
                for s in parse_kcp(dec(d[16:], d[:16])):
                    self.maxsn = max(self.maxsn, s['sn'])
        except socket.timeout:
            pass
        return self

    def _seg(self, sn, payload, ts=None):
        if ts is None: ts = int(time.time() * 1000) & 0xffffffff
        return struct.pack('<I B B H I I I I', self.conv, 0x51, 0, 0x1000, ts, sn, 0, len(payload)) + payload

    def request(self, path: bytes, body=b"{}", collect=6.0,
                content_type=b"application/json", extra_headers=None, chunked=True,
                body_encrypted=False):
        """发业务请求，复刻 App 真实首包结构：
        5701 握手段 + route 段 + 5703声明(HTTP头) + HTTP头明文XORkey +
        5703声明(body) + body(可为已Rabbit加密的密文)。
        返回 (status, headers, body)。
        """
        lines = [
            b"POST " + path + b" HTTP/1.1",
            b"user-agent: Dart/3.13 (dart:io)",
            b"accept: application/proto" if content_type == b"application/proto" else b"accept: application/json",
            b"accept-encoding: gzip",
        ]
        if chunked:
            lines.append(b"transfer-encoding: chunked")
        else:
            lines.append(b"content-length: " + str(len(body)).encode())
        if self.jwt and not body_encrypted:
            lines.append(b"authorization: Bearer " + self.jwt)
        for k, v in (extra_headers or []):
            lines.append(k + b": " + v)
        lines.extend([b"", b""])
        header_block = b"\r\n".join(lines)
        if chunked and not body_encrypted:
            payload = ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n"
        else:
            payload = body
        hdr_x = bytes(x ^ self.key for x in header_block)
        body_x = payload if body_encrypted else bytes(x ^ self.key for x in payload)
        ts = int(time.time() * 1000) & 0xffffffff
        sn = self.maxsn + 1
        # 5701 握手 4B
        pt = os.urandom(4)
        pt += self._seg(sn, b'\x57\x01\x00\x00', ts=ts); sn += 1
        # route 段（服务器 IP 路由，登录/业务通用）
        pt += self._seg(sn, self.route_seg, ts=ts); sn += 1
        # HTTP 头段
        pt += self._seg(sn, b'\x57\x03' + struct.pack('>H', len(hdr_x)), ts=ts); sn += 1
        pt += self._seg(sn, hdr_x, ts=ts); sn += 1
        # body 段
        pt += self._seg(sn, b'\x57\x03' + struct.pack('>H', len(body_x)), ts=ts); sn += 1
        pt += self._seg(sn, body_x, ts=ts); self.maxsn = sn
        salt = os.urandom(16); self.sock.send(salt + enc(pt, salt))
        # 响应结构：每个 5703 段 = payload 内 '5703' + >H 长度 + data(XOR后)。
        # data 明文前可能带 76 22 xx xx 帧头，剥掉。拼接全部数据段后按 HTTP 起始切。
        best = {}
        deadline = time.time() + collect
        while time.time() < deadline:
            try:
                d = self.sock.recv(65535)
            except socket.timeout:
                continue
            for s in parse_kcp(dec(d[16:], d[:16])):
                if s['conv'] != self.conv: continue
                pay = s['payload']
                if pay[:2] == b'\x57\x03' and len(pay) >= 4:
                    ln = struct.unpack('>H', pay[2:4])[0]
                    dd = bytes(x ^ self.key for x in pay[4:4 + ln])
                elif pay[:2] in (b'\x57\x01', b'\xa1\xb2', b'\x57\x02', b'\x57\x04'):
                    continue
                else:
                    dd = bytes(x ^ self.key for x in pay)
                if dd[:2] == b'\x76\x22':
                    dd = dd[4:]
                if dd:
                    best.setdefault(s['sn'], b'')
                    best[s['sn']] += dd
        raw = b"".join(best[sn] for sn in sorted(best))
        # 去掉 HTTP 前的会话前缀字节
        h = raw.find(b'HTTP/1.1 ')
        if h > 0:
            raw = raw[h:]
        elif h < 0:
            h2 = raw.find(b'HTTP/1.0 ')
            if h2 > 0: raw = raw[h2:]
        return self._parse_http(raw)

    def _parse_http(self, raw):
        import zlib
        if b'\r\n\r\n' not in raw:
            return (0, [], raw)
        hdr, body = raw.split(b'\r\n\r\n', 1)
        lines = hdr.split(b'\r\n')
        status = 0
        try: status = int(lines[0].split(b' ')[1])
        except: pass
        if any(line.lower().startswith(b'transfer-encoding:') and b'chunked' in line.lower()
               for line in lines):
            body = dechunk(body)
        if body[:2] == b'\x1f\x8b':
            try: body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
            except Exception: pass
        return (status, lines, body)

    # ---- 便捷业务封装 ----
    def game_list(self, is_inside=0, channel_id="tui1"):
        body = ('{"is_inside":%d,"channel_id":"%s"}' % (is_inside, channel_id)).encode()
        s, h, b = self.request(b"/game_list/list", body=body)
        return b
    def game_type_list(self):
        return self.request(b"/game_type/list")[2]
    def user_info(self):
        return self.request(b"/user/get_user_info")[2]
    def get_money(self):
        return self.request(b"/game_club_member/get_money")[2]


if __name__ == "__main__":
    import json
    d = os.path.dirname(__file__)
    fp = open(os.path.join(d, "biz_pkt.hex")).read().strip()
    jwt = open(os.path.join(d, "jwt.txt")).read().strip()
    c = DuocaiPure(fp).connect()
    c.jwt = jwt.encode()
    print("conv=%#x key=%#x maxsn=%d" % (c.conv, c.key, c.maxsn))
    b = c.game_list()
    j = json.loads(b.decode('utf-8', 'replace'))
    r = j.get('result', j)
    print("房列表项数:", len(r) if isinstance(r, list) else 'dict')
    if isinstance(r, list):
        for it in r[:5]: print("  -", it.get('name'))
