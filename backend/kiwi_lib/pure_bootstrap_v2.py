#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pure_bootstrap_v2.py —— 完全凭空闭环（不读 pcap）
纯代码构造 device_json + 随机 umid → DNS 解析 → 连 8003 → 发引导首包 → 拿节点池

验证结论（m03714/m03720）：
  - pkt11 引导首包反复重放都有效
  - umid 随便编（701#数字#7#hex）服务器照样下发节点池，不校验真伪
  - 唯一动态的是节点 IP/端口（每次重新分配）
"""
import socket, struct, json, time, os, sys

ENTRY_HOST = "75a89a88ffdd4b5eab6d58994a3854a0.aliyunl.com"
ENTRY_PORT = 8003
CONV = 0x0bead30f
XOR_KEY = 0x3f

# 引导 route a1b2 头 56B（[3]=0x02 引导, [18:20]=0x3f3f）
A1B2_HEADER = bytes.fromhex(
    "a1b2f7020000000078cbb2626affe4ed03003f3fe27eb56ff7"
    "ec479dffe97b30a311f5105ee8db11ba4bb95756aeb7b7d2f1e75ee8de40df"
)

def rand_hex(n):
    return os.urandom(n).hex()

def make_umid():
    # 格式 701#<3位数字>#7#<8位hex>  —— 随便编，服务器不校验
    return "701#%03d#7#%s" % (int.from_bytes(os.urandom(2),'big') % 1000, rand_hex(4))

def make_android_id():
    return rand_hex(8)

def build_device_json(umid, android_id):
    dev = {
        "appid": 53,
        "umid": umid,
        "sdkver": "A521",
        "appname": "多--彩##com.dwdedmq.glvcbdiwubby",
        "connection_detail": [],
        "connection_count": 0,
        "connection_count_failed": 0,
        "connection_avg": 0,
        "connection_avg_success": 0,
        "network": "wifi",
        "session": "init",
        "device": {
            "model": "google sdk_gphone64_arm64",
            "os": "Android14",
            "screen": "0*0",
            "android_id": android_id,
            "device_id": "Unknown DeviceID NoPermission",
            "device_name": "sdk_gphone64_arm64",
            "operator": "Unknown Operator 310260",
            "install": int(time.time() * 1000),
            "boot": 138,
        },
    }
    # 用与真实抓包一致的 separators：严格紧凑，但 appname 中文保留
    return json.dumps(dev, separators=(', ', ':'), ensure_ascii=False).encode('utf-8')

def build_seg(sn, payload, ts=0):
    return struct.pack('<I B B H I I I I',
                       CONV, 0x51, 0, 0x1000, ts, sn, 0, len(payload)) + payload

def build_bootstrap_packet(umid, android_id):
    dj = build_device_json(umid, android_id)
    dj_xor = bytes(b ^ XOR_KEY for b in dj)
    pkt = b''
    pkt += build_seg(0, b'\x57\x01\x00\x00')                       # 5701 声明
    pkt += build_seg(1, b'\x57\x03\x00\x38')                       # 5703 声明 len=56
    pkt += build_seg(2, A1B2_HEADER)                               # a1b2 引导头
    pkt += build_seg(3, b'\x57\x03' + struct.pack('>H', len(dj_xor)))  # 5703 声明 len=dj
    pkt += build_seg(4, dj_xor)                                    # device_json
    return pkt, dj

def parse_segs(w):
    segs = []
    i = 0
    while i + 24 <= len(w):
        cv, cmd, frg, wnd, ts, sn, una, ln = struct.unpack('<I B B H I I I I', w[i:i+24])
        if cv != CONV:
            break
        segs.append((sn, w[i+24:i+24+ln]))
        i += 24 + ln
    return segs

def extract_nodepool(segs):
    body = b''
    for sn, pay in segs:
        if pay[:2] == b'\x57\x03':
            dl = struct.unpack('>H', pay[2:4])[0]
            b = pay[4:4+dl]
            if b[:2] == b'\x31\x32' and len(b) >= 16:
                b = b[16:]
            body += b
    if not body:
        return None
    dec = bytes(x ^ XOR_KEY for x in body)
    s = dec.decode('latin1', errors='replace')
    st = s.find('{')
    if st < 0:
        return None
    depth = 0; end = st
    for j in range(st, len(s)):
        if s[j] == '{': depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                end = j + 1; break
    try:
        return json.loads(s[st:end])
    except Exception:
        return s[st:end]

def main():
    print("=" * 68)
    print("完全凭空闭环：随机 umid -> DNS -> 8003 -> 引导首包 -> 节点池")
    print("=" * 68)

    umid = make_umid()
    android_id = make_android_id()
    print("\n[1] 随机生成设备身份:")
    print("    umid       = %s" % umid)
    print("    android_id = %s" % android_id)

    print("\n[2] DNS 解析 %s ..." % ENTRY_HOST)
    try:
        ip = socket.gethostbyname(ENTRY_HOST)
        print("    -> %s" % ip)
    except Exception as e:
        ip = "106.55.158.133"
        print("    DNS 失败(%s)，回退 %s" % (e, ip))

    pkt, dj = build_bootstrap_packet(umid, android_id)
    print("\n[3] 构造引导首包: %d 字节 (device_json %d 字节)" % (len(pkt), len(dj)))

    print("\n[4] 发送到 %s:%d ..." % (ip, ENTRY_PORT))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    sock.sendto(pkt, (ip, ENTRY_PORT))

    print("\n[5] 收响应（最多 6 秒）...")
    all_segs = []
    resp = []
    t = time.time() + 6
    while time.time() < t:
        try:
            sock.settimeout(max(0.1, t - time.time()))
            d, addr = sock.recvfrom(65535)
            resp.append(d)
            all_segs.extend(parse_segs(d))
        except socket.timeout:
            break
        except Exception:
            break
    sock.close()
    print("    收到 %d 个 UDP 包, %d 段" % (len(resp), len(all_segs)))

    if not all_segs:
        print("\n!! 无响应")
        return 1

    pool = extract_nodepool(all_segs)
    print("\n" + "=" * 68)
    if isinstance(pool, dict):
        nodes = pool.get('node', [])
        print("★★★★ 节点池获取成功（凭空 umid，未读 pcap）★★★★")
        print("=" * 68)
        print("node 节点: %d 个" % len(nodes))
        for nd in nodes:
            print("   %-16s:%-6s gid=%s" % (nd.get('IP'), nd.get('Port'), nd.get('gid')))
        print("back_node: %s" % [(b.get('IP'), b.get('Port')) for b in pool.get('back_node', [])])
        dns = pool.get('dns', [])
        print("dns route 模板: %d 个 -> %s" % (len(dns), [d.get('name') for d in dns]))
        print("\n完整节点池 JSON 已保存到 /tmp/nodepool_live.json")
        with open('/tmp/nodepool_live.json', 'w') as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        return 0
    else:
        print("!! 未解析出节点池")
        for sn, pay in all_segs[:8]:
            print("   sn=%d len=%d %s" % (sn, len(pay), pay[:20].hex()))
        return 1

if __name__ == '__main__':
    sys.exit(main() or 0)
