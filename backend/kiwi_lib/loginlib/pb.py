"""极简 protobuf 编解码 (wire type 0 varint / 2 length-delimited / 5...) 用于 kiwi 协议"""
def _varint(n):
    if n<0: n+=(1<<64)
    out=bytearray()
    while True:
        b=n&0x7f; n>>=7
        if n: out.append(b|0x80)
        else: out.append(b); break
    return bytes(out)

def _tag(field, wire): return _varint((field<<3)|wire)

def enc_varint_field(field, value):
    """int32/uint32/enum 但 >2^31 用 varint(positive)"""
    if value<0: value+=(1<<64)
    return _tag(field,0)+_varint(value)

def enc_bytes_field(field, data):
    if isinstance(data,str): data=data.encode()
    return _tag(field,2)+_varint(len(data))+data

def enc_str_field(field, s): return enc_bytes_field(field, s.encode() if isinstance(s,str) else s)
def enc_msg_field(field, msg_bytes): return enc_bytes_field(field, msg_bytes)

def _read_varint(buf, i):
    shift=0; result=0
    while True:
        b=buf[i]; i+=1
        result |= (b&0x7f)<<shift
        if not (b&0x80): break
        shift+=7
    return result,i

def decode(buf):
    """返回 dict{field: value}, len-delimited→bytes, varint→int. repeated→list"""
    out={}
    i=0; n=len(buf)
    while i<n:
        key,i=_read_varint(buf,i)
        field=key>>3; wire=key&7
        if wire==0:
            v,i=_read_varint(buf,i)
        elif wire==2:
            ln,i=_read_varint(buf,i)
            v=buf[i:i+ln]; i+=ln
        elif wire==5:
            v=buf[i:i+4]; i+=4
        elif wire==1:
            v=buf[i:i+8]; i+=8
        else:
            raise ValueError("wire %d"%wire)
        if field in out:
            if not isinstance(out[field],list): out[field]=[out[field]]
            out[field].append(v)
        else:
            out[field]=v
    return out
