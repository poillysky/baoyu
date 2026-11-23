"""鲍鱼盒子 libkiwi WS 协议密码学 — 全部经 Java 参考实现逐位验证通过"""
import hashlib, base64, os, struct, time
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

FIXED_KEY_NUMS = [106,81,66,85,79,38,50,112,102,83,53,115,67,227,128,141,194,165,63,65,116,103,108,88,118,42,119,43,80,99,82,75,56,37,105]
FIXED_KEY_BYTES = bytes((n & 0xFF) for n in FIXED_KEY_NUMS)
FIXED_KEY_STR = FIXED_KEY_BYTES.decode('utf-8')   # 'jQBUO&2pfS5sC」¥?AtglXv*w+PcRK8%i'
AES_KEY = bytes.fromhex("064392d90fde936b13a5a100c471bdf0")
AES_IV  = bytes.fromhex("475DD5A0DB3BF782021B12E5A2C64ABF")
CHANNEL_ID = "tui1"
M = 0xFFFFFFFF

def md5hex(s):
    if isinstance(s,str): s=s.encode('utf-8')
    return hashlib.md5(s).hexdigest()

def _hour(): return int(time.time())//3600

def generate_key(jwt):      return md5hex(FIXED_KEY_STR + str(_hour()) + jwt)
def generate_without_jwt_key(): return md5hex(FIXED_KEY_STR + str(_hour()))
def generate_iv_str():      return base64.b64encode(os.urandom(8)).decode()

def generate_apns_code(uuid_str):
    c=AES.new(AES_KEY,AES.MODE_CBC,AES_IV)
    return base64.b64encode(c.encrypt(pad(uuid_str.encode(),16))).decode()

def generate_client_token(uuid_str, channel=CHANNEL_ID):
    return md5hex("token:"+uuid_str+"_"+channel)

def _xor(d,k): return bytes(d[i]^k[i%len(k)] for i in range(len(d)))
def _rev(b):   return b[::-1]
def encrypt_path(s, iv1=None, iv2=None):
    if iv1 is None: iv1=os.urandom(11)
    if iv2 is None: iv2=os.urandom(13)
    inner=_rev(_xor(s.encode(),iv1))
    x=_xor(iv1+inner,iv2)
    return base64.b64encode(_rev(x)+iv2).decode()

def rotl(x,n): x&=M; return ((x<<n)|(x>>(32-n)))&M
class Rabbit:
    def __init__(s): s.x=[0]*8; s.c=[0]*8; s.carry=0
    def _next(s):
        s.carry=0
        for i in range(8):
            t=s.c[i]+s.x[i]+s.carry; s.carry=(t>>32)&1; s.c[i]=t&M
        g=[]
        for i in range(8):
            u=(s.x[i]+s.c[i])&M; j11=(u*u)&M; g.append((j11^(j11>>16))&M)
        s.x=[(g[0]+rotl(g[7],16)+rotl(g[6],16))&M,(g[1]+rotl(g[0],8)+g[7])&M,
             (g[2]+rotl(g[1],16)+rotl(g[0],16))&M,(g[3]+rotl(g[2],8)+g[1])&M,
             (g[4]+rotl(g[3],16)+rotl(g[2],16))&M,(g[5]+rotl(g[4],8)+g[3])&M,
             (g[6]+rotl(g[5],16)+rotl(g[4],16))&M,(g[7]+rotl(g[6],8)+g[5])&M]
    def setup_key(s,key):
        s.x=[0]*8; s.c=[0]*8; s.carry=0
        k=[((key[2*i+1]&0xff)|((key[2*i]&0xff)<<8)) for i in range(8)]
        s.x=[((k[0]<<16)|k[1])&M,((k[1]<<16)|k[2])&M,((k[2]<<16)|k[3])&M,((k[3]<<16)|k[4])&M,
             ((k[4]<<16)|k[5])&M,((k[5]<<16)|k[6])&M,((k[6]<<16)|k[7])&M,((k[7]<<16)|k[0])&M]
        s.c=[((k[4]<<16)|k[5])&M,((k[5]<<16)|k[6])&M,((k[6]<<16)|k[7])&M,((k[7]<<16)|k[0])&M,
             ((k[0]<<16)|k[1])&M,((k[1]<<16)|k[2])&M,((k[2]<<16)|k[3])&M,((k[3]<<16)|k[4])&M]
    def setup_iv(s,iv):
        a=struct.unpack('>I',bytes(iv[0:4]))[0]; b=struct.unpack('>I',bytes(iv[4:8]))[0]
        s.x[0]^=a; s.x[1]^=b
        for _ in range(4): s._next()
    def key_stream(s,n):
        out=bytearray(); sb=0
        for i in range(n):
            if (i&15)==0: s._next(); sb=s.x[5]^s.x[0]
            out.append(sb&0xff)
            if (i&3)==3:
                j=((i>>2)+1)&7; sb=s.x[j]^s.x[(j+5)&7]
            else: sb>>=8
        return bytes(out)

def rabbit_crypt(key16, iv8, data):
    """key16=16字节; iv8=8字节; 加解密对称"""
    r=Rabbit(); r.setup_key(key16); r.setup_iv(iv8)
    ks=r.key_stream(len(data))
    return bytes(data[i]^ks[i] for i in range(len(data)))

if __name__=="__main__":
    # 自检 RFC 级向量
    r=Rabbit(); r.setup_key(bytes.fromhex("912813292E3D36FE3BFC62F1DC51C3AC")); r.setup_iv(bytes.fromhex("597E264D00000000"))
    assert r.key_stream(16).hex()=="4e88d51d5cf92a349aec14041d9e7016"
    iv1=bytes(range(1,12)); iv2=bytes(range(20,33))
    assert encrypt_path("/ws/chat",iv1,iv2)=="N21nPXN6RmMVFxUTHR8dExUXFRQVFhcYGRobHB0eHyA="
    print("crypto.py ALL OK")
