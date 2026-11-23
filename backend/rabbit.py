# -*- coding: utf-8 -*-
"""移植自 com.rabbit.RabbitCipher (标准 Rabbit 流密码)"""
M32 = 0xFFFFFFFF

def _rotl(x, n):
    x &= M32
    return ((x << n) | (x >> (32 - n))) & M32

class RabbitCipher:
    def __init__(self):
        self.x = [0]*8; self.c = [0]*8
        self.masterX = [0]*8; self.masterC = [0]*8
        self.masterB = 0; self.b = 0
        self.keySetupCalled = False

    def reset(self):
        self.x=[0]*8; self.c=[0]*8; self.masterX=[0]*8; self.masterC=[0]*8
        self.masterB=0; self.b=0; self.keySetupCalled=False

    def _next_state(self):
        c=self.c; x=self.x
        carry=0
        for i in range(8):
            t=(c[i]&M32)+(x[i]&M32)+carry
            c[i]=t&M32
            carry=(t>>32)&1
        g=[0]*8
        for i in range(8):
            v=(x[i]&M32)+(c[i]&M32)
            j11=(v*v)&M32
            g[i]=(j11^(j11>>16))&M32
        x[0]=((g[0] + _rotl(g[7],16) + _rotl(g[6],16))&M32)
        x[1]=((g[1] + _rotl(g[0],8)  + g[7])&M32)
        x[2]=((g[2] + _rotl(g[1],16) + _rotl(g[0],16))&M32)
        x[3]=((g[3] + _rotl(g[2],8)  + g[1])&M32)
        x[4]=((g[4] + _rotl(g[3],16) + _rotl(g[2],16))&M32)
        x[5]=((g[5] + _rotl(g[4],8)  + g[3])&M32)
        x[6]=((g[6] + _rotl(g[5],16) + _rotl(g[4],16))&M32)
        x[7]=((g[7] + _rotl(g[6],8)  + g[5])&M32)

    @staticmethod
    def _u32(b): # big-endian 4 bytes -> int
        return (b[0]<<24)|(b[1]<<16)|(b[2]<<8)|b[3]

    def setup_key(self, key):
        assert len(key)==16
        self.reset(); self.keySetupCalled=True
        k=[0]*8
        for i in range(8):
            k[i]=((key[2*i]&0xFF)<<8)|(key[2*i+1]&0xFF)
        x=self.x; c=self.c
        x[0]=(k[0]<<16)|k[1]; x[1]=(k[1]<<16)|k[2]; x[2]=(k[2]<<16)|k[3]; x[3]=(k[3]<<16)|k[4]
        x[4]=(k[4]<<16)|k[5]; x[5]=(k[5]<<16)|k[6]; x[6]=(k[6]<<16)|k[7]; x[7]=(k[7]<<16)|k[0]
        c[0]=(k[4]<<16)|k[5]; c[1]=(k[5]<<16)|k[6]; c[2]=(k[6]<<16)|k[7]; c[3]=(k[7]<<16)|k[0]
        c[4]=(k[0]<<16)|k[1]; c[5]=(k[1]<<16)|k[2]; c[6]=(k[2]<<16)|k[3]; c[7]=(k[3]<<16)|k[4]
        for i in range(8):
            x[i]&=M32; c[i]&=M32
        self.masterX=x[:]; self.masterC=c[:]; self.masterB=self.b

    def setup_iv(self, iv):
        assert len(iv)==8
        assert self.keySetupCalled
        i0=self._u32(iv[0:4]); i1=self._u32(iv[4:8])
        x=self.x
        x[0]=(i0^x[0])&M32; x[1]=(i1^x[1])&M32
        for _ in range(4): self._next_state()

    def key_stream(self, n):
        out=bytearray(n)
        s=0
        for i in range(n):
            if (i & 15)==0:
                self._next_state()
                x=self.x
                s=(x[5]^x[0])&M32
            out[i]=s&0xFF
            if (i & 3)==3:
                j=((i>>2)+1)&7
                x=self.x
                s=(x[j]^x[(j+5)&7])&M32
            else:
                s>>=8
        return bytes(out)

    def encrypt_decrypt(self, data, ks):
        return bytes(a^b for a,b in zip(data,ks))
