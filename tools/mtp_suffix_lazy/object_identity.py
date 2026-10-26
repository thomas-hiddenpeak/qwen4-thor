"""Compare CUDA ELF objects, allowing only NVCC's STT_FILE temporary PID."""
import hashlib
import re
import struct
from pathlib import Path


def canonical(path):
    data=bytearray(Path(path).read_bytes())
    if data[:6]!=b'\x7fELF\x02\x01' or struct.unpack_from('<H',data,18)[0]!=183:
        raise ValueError('expected little-endian AArch64 ELF64')
    offset=struct.unpack_from('<Q',data,40)[0]
    size,count,string_index=struct.unpack_from('<HHH',data,58)
    if size!=64 or not 0<string_index<count or offset+size*count>len(data):raise ValueError('invalid ELF section table')
    sections=[struct.unpack_from('<IIQQQQIIQQ',data,offset+i*size) for i in range(count)]
    changed=[]
    for sym in sections:
        if sym[1]!=2:continue  # SHT_SYMTAB
        if sym[9]!=24 or sym[6]>=count or sym[4]+sym[5]>len(data):raise ValueError('invalid symtab')
        strings=sections[sym[6]]
        if strings[1]!=3 or strings[4]+strings[5]>len(data):raise ValueError('invalid symbol strings')
        for p in range(sym[4],sym[4]+sym[5],24):
            name,info,other,index,value,length=struct.unpack_from('<IBBHQQ',data,p)
            if info&15!=4:continue  # STT_FILE only; never normalize callable names.
            if name>=strings[5]:raise ValueError('invalid file symbol')
            start=strings[4]+name;end=data.find(0,start,strings[4]+strings[5])
            if end<0:raise ValueError('unterminated file symbol')
            text=bytes(data[start:end]);stem=Path(path).name.removesuffix('.cu.o').encode()
            pattern=rb'tmpxft_([0-9a-f]{8})_00000000-6_'+re.escape(stem)+rb'\.cudafe1\.cpp'
            m=re.fullmatch(pattern,text)
            if m:
                if info!=4 or other!=0 or index!=0xfff1 or value or length:raise ValueError('unexpected NVCC file symbol attributes')
                begin=start+m.start(1);data[begin:begin+8]=b'00000000'
                changed.append(dict(file_symbol=text.decode(),offset=begin))
    if len(changed)>1:raise ValueError('ambiguous NVCC metadata')
    return bytes(data),changed


def compare(a,b):
    x,xmeta=canonical(a);y,ymeta=canonical(b)
    raw_a=Path(a).read_bytes();raw_b=Path(b).read_bytes()
    if x!=y:raise ValueError('CUDA object differs beyond NVCC temporary PID: '+str(a))
    if bool(xmeta)!=bool(ymeta):raise ValueError('CUDA file metadata shape differs')
    return dict(raw_sha256=hashlib.sha256(raw_a).hexdigest(),reference_sha256=hashlib.sha256(raw_b).hexdigest(),
                raw_equal=raw_a==raw_b,canonical_sha256=hashlib.sha256(x).hexdigest(),equal=True,
                current_metadata=xmeta,reference_metadata=ymeta,
                scope='All ELF bytes identical except8hex PID of verified local ABS STT_FILE NVCC filename; code, fatbin, data, symbol/relocation tables and names retained.')
