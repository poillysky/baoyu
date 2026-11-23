# -*- coding: utf-8 -*-
"""
纯 Python 复刻 libkiwi 协议：直接向 App 本地中继 (127.0.0.1:PORT) 请求 Agora token。
App 只当固定桥接器（后台常驻，连 162 中继）。零 UI 交互。
"""
import asyncio, hashlib, base64, os, time, struct, json
import websockets

FIXED_KEY = "106,81,66,85,79,38,50,112,102,83,53,115,67,227,128,141,194,165,63,65,116,103,108,88,118,42,119,43,80,99,82,75,56,37,105"

def _fixed_bytes():
    return bytes(int(x) & 0xFF for x in FIXED_KEY.split(","))

def generate_key(jwt):
    h = (time.time()//1)//1
    j = int(time.time()//3600)
    s = _fixed_bytes().decode("utf-8", "surrogateescape") + str(j) + jwt
    return hashlib.md5(s.encode("utf-8", "surrogateescape")).digest()

def generate_without_jwt_key():
    j = int(time.time()//3600)
    s = _fixed_bytes().decode("utf-8","surrogateescape") + str(j)
    return hashlib.md5(s.encode("utf-8","surrogateescape")).digest()

# ---- protobuf 手写编码 (只用到简单 varint/len-delimited) ----
def _varint(n):
    out=b""
    while True:
        b=n&0x7f; n>>=7
        if n: out+=bytes([b|0x80])
        else: out+=bytes([b]); return out

def _tag(field, wire): return _varint((field<<3)|wire)
def _pb_int(field, val): return _tag(field,0)+_varint(int(val))
def _pb_str(field, s):
    b=s.encode() if isinstance(s,str) else s
    return _tag(field,2)+_varint(len(b))+b

def build_request(random, method, path, data_bytes):
    # Request{random=1 int32, method=2 enum, path=3 str, data=4 bytes}
    out=_pb_int(1,random)+_pb_int(2,method)+_pb_str(3,path)+_pb_str(4,data_bytes)
    return out

def build_login_init(apns, jwt, appver):
    return _pb_str(1,apns)+_pb_str(2,jwt)+_pb_str(3,appver)

def build_agora_req(channel, user_id):
    return _pb_str(2,channel)+_pb_int(3,int(user_id))

def parse_response(buf):
    # Response{random=1, body=2 bytes, httpStatus=3}
    i=0; out={}
    while i<len(buf):
        key=0; shift=0
        while True:
            b=buf[i]; i+=1; key|=(b&0x7f)<<shift; shift+=7
            if not b&0x80: break
        field=key>>3; wire=key&7
        if wire==0:
            v=0; shift=0
            while True:
                b=buf[i]; i+=1; v|=(b&0x7f)<<shift; shift+=7
                if not b&0x80: break
            out[field]=v
        elif wire==2:
            ln=0; shift=0
            while True:
                b=buf[i]; i+=1; ln|=(b&0x7f)<<shift; shift+=7
                if not b&0x80: break
            out[field]=buf[i:i+ln]; i+=ln
        else: break
    return out

def parse_agora_reply(body):
    # AgoraReply{data=2 string}
    r=parse_response(body)
    d=r.get(2)
    return d.decode("utf-8") if isinstance(d,(bytes,bytearray)) else (d if isinstance(d,str) else None)

async def _exchange(port, jwt, apns, channel, user_id, appver="20260226001"):
    import urllib.parse
    import string
    _ok=set(string.ascii_letters+string.digits+"=")
    for _ in range(200):
        path_enc = encrypt_path("/ws/chat")
        if set(path_enc) <= _ok:
            break
    path_enc_q = path_enc
    url=f"ws://127.0.0.1:{port}/go/{path_enc_q}?z=1&nobase64=1"
    iv=os.urandom(8)
    iv_b64=base64.b64encode(iv).decode()
    key=generate_key(jwt)
    from rabbit import RabbitCipher
    def new_cipher():
        c=RabbitCipher(); c.setup_key(key); c.setup_iv(iv); return c
    hdrs=[("authorization","Bearer "+jwt),("cipher",iv_b64),("user-agent","Dart/3.5 (dart:io)")]
    send_cipher=new_cipher()  # 请求侧: 同一cipher连续加密所有帧
    async with websockets.connect(url, extra_headers=hdrs, open_timeout=8, max_size=None) as ws:
        # 1) login/init
        li=build_login_init(apns,jwt,appver)
        rnd=1
        req=build_request(rnd,3,"/login/init",li)
        await ws.send(send_cipher.encrypt_decrypt(req,send_cipher.key_stream(len(req))))
        # 读 login/init 响应
        try:
            resp=await asyncio.wait_for(ws.recv(),timeout=12)
        except asyncio.TimeoutError:
            print("[DBG] login/init no response; sent bytes:",req.hex()[:60]); raise
        rb=resp if isinstance(resp,(bytes,bytearray)) else resp.encode()
        open("/tmp/live_login_s.bin","wb").write(rb)
        print("[DBG] login/init resp rawlen",len(rb),"hex",rb.hex()[:60])
        cipher2=RabbitCipher(); cipher2.setup_key(key); cipher2.setup_iv(iv)
        ks2=cipher2.key_stream(len(rb))
        plain=cipher2.encrypt_decrypt(rb,ks2)
        print("[DBG] login/init plain hex",plain.hex()[:60])
        r=parse_response(plain)
        print("[DBG] login/init parsed",{k:(v.hex() if isinstance(v,bytes) else v) for k,v in r.items()})
        # 2) generate_agora_token
        ag=build_agora_req(channel,user_id)
        rnd=2
        req=build_request(rnd,3,"/stream/generate_agora_token",ag)
        await ws.send(send_cipher.encrypt_decrypt(req,send_cipher.key_stream(len(req))))
        print("[DBG] generate req sent", req.hex())
        try:
            resp=await asyncio.wait_for(ws.recv(),timeout=12)
        except asyncio.TimeoutError:
            print("[DBG] generate NO RESPONSE timeout"); raise
        rb=resp if isinstance(resp,(bytes,bytearray)) else resp.encode()
        open("/tmp/live_gen_s.bin","wb").write(rb)
        print("[DBG] generate resp rawlen",len(rb),"hex",rb.hex())
        import json as _j
        open("/tmp/live_meta.json","w").write(_j.dumps({"key":key.hex(),"iv":iv.hex()}))
        # ★关键: 响应是连续流! generate 响应复用 cipher2(已推进过login_resp长度) 继续解密
        ks2=cipher2.key_stream(len(rb))
        plain=cipher2.encrypt_decrypt(rb,ks2)
        print("[DBG] generate plain hex",plain.hex())
        r=parse_response(plain)
        print("[DBG] generate parsed",{k:(v.hex() if isinstance(v,bytes) else v) for k,v in r.items()})
        status=r.get(3)
        body=r.get(2)
        token=parse_agora_reply(body) if body else None
        return {"httpStatus":status,"token":token}

def encrypt_path(s):
    # 复刻 Utils.encryptPath
    iv11=os.urandom(11); iv13=os.urandom(13)
    def xorwithkey(b,k): return bytes(b[i]^k[i%len(k)] for i in range(len(b)))
    def rev(b): return b[::-1]
    sb=s.encode()
    step1=xorwithkey(sb,iv11)
    step2=rev(step1)
    step3=iv11+step2
    step4=xorwithkey(step3,iv13)
    step5=rev(step4)
    final=step5+iv13
    return base64.b64encode(final).decode()

def request_token(port, jwt, apns, channel, user_id, appver="20260226001"):
    return asyncio.run(_exchange(port, jwt, apns, channel, user_id, appver))

if __name__=="__main__":
    import sys
    # 测试: 从 demo_prefs 读
    import subprocess
    ADB="/Users/poilly/Library/Android/sdk/platform-tools/adb"
    xml=subprocess.run([ADB,"-s","emulator-5554","shell",
        "cat /data/data/me.hq80tw8.ws04i1m/shared_prefs/demo_prefs.xml"],capture_output=True,text=True).stdout
    import re
    jwt=re.search(r'guest_token">([^<]+)',xml).group(1)
    uid=re.search(r'user_id">([^<]+)',xml).group(1)
    apns=re.search(r'apns_code">([^<]+)',xml).group(1)
    print("jwt",jwt[:30],"uid",uid,"apns",apns[:20])
    ch=sys.argv[1] if len(sys.argv)>1 else "10003764"
    r=request_token(37959,jwt,apns,ch,uid)
    print("RESULT:",json.dumps(r)[:400])
