"""Diagnostic-only, layout-preserving removal of proven LDR/B veneers.

Never writes the source or a deployment binary. Refuses unknown instructions,
CPU identity, ELF format, multiple incoming branches, and changed input hashes.
"""
from pathlib import Path
import hashlib,json,struct,subprocess,shutil
ROOT=Path('/home/rm01/models/dev/qwen4-thor'); OUT=Path(__file__).resolve().parent
EXPECTED={'A':'be74946d815904e84899c64fd88511cb0474166bef9d199c22330e8413db5072','B':'f01a0ee63f626c352f66677952368d43db02c9231576a2b8ba1ca33a915ce573'}
SOURCES={'A':ROOT/'.q4t-work/mtp-decode-20261016/q4t-main','B':ROOT/'.q4t-work/mtp-decode-20261016/hc-up/q4t'}
sha=lambda b:hashlib.sha256(b).hexdigest()

def target(pc,word):
 assert word&0xfc000000==0x14000000
 imm=word&0x3ffffff
 if imm&(1<<25):imm-=1<<26
 return pc+imm*4

def sections(data):
 assert data[:6]==b'\x7fELF\x02\x01' and struct.unpack_from('<H',data,18)[0]==183
 off=struct.unpack_from('<Q',data,40)[0]
 size,num,strings=struct.unpack_from('<HHH',data,58);assert size==64
 records=[struct.unpack_from('<IIQQQQIIQQ',data,off+i*size) for i in range(num)]
 names=data[records[strings][4]:records[strings][4]+records[strings][5]]
 return {names[x[0]:].split(b'\0',1)[0].decode():x for x in records}

def prepare():
 parts={line.split(':')[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('CPU part')}
 assert parts=={'0xd83'},parts
 manifest={'cpu_parts':sorted(parts),'scope':'diagnostic only; no deployable artifact','variants':{}}
 for side,source in SOURCES.items():
  data=source.read_bytes();assert sha(data)==EXPECTED[side]
  sec=sections(data);text=sec['.text'];base,offset,size=text[3:6];assert text[2]&4
  code=data[offset:offset+size];thunks=[]
  for line in subprocess.check_output(['nm','-S','--defined-only',str(source)],text=True).splitlines():
   parts=line.split()
   if len(parts)==4 and parts[3].startswith('e843419@'):
    address,length=int(parts[0],16),int(parts[1],16);assert length==8
    first,back=struct.unpack_from('<II',code,address-base)
    assert first&0xffc00000==0xf9400000,hex(first) # LDR Xt,[Xn,#unsigned imm]
    resume=target(address+4,back);site=resume-4
    incoming=[]
    for i in range(0,len(code),4):
     word=struct.unpack_from('<I',code,i)[0]
     if word&0xfc000000==0x14000000 and target(base+i,word)==address:incoming.append(base+i)
    assert incoming==[site],incoming
    assert base<=site<base+size
    old=struct.unpack_from('<I',code,site-base)[0]
    thunks.append({'symbol':parts[3],'thunk_va':address,'site_va':site,'file_offset':offset+site-base,'old_word':hex(old),'new_word':hex(first),'resume_va':resume})
  assert len(thunks)=={'A':6,'B':5}[side]
  changed=bytearray(data)
  for t in thunks:struct.pack_into('<I',changed,t['file_offset'],int(t['new_word'],16))
  permitted={i for t in thunks for i in range(t['file_offset'],t['file_offset']+4)}
  actual={i for i,(a,b) in enumerate(zip(data,changed)) if a!=b};assert actual<=permitted
  assert len(changed)==len(data) and len(actual)>0
  for name,x in sec.items():
   if name!='.text':assert data[x[4]:x[4]+x[5]]==changed[x[4]:x[4]+x[5]]
  for enabled,payload in [('0',data),('1',bytes(changed))]:
   variant=side+enabled;d=OUT/variant;d.mkdir(exist_ok=False)
   (d/'q4t').write_bytes(payload);(d/'q4t').chmod(0o755)
   shutil.copy2(source.parent/'CMakeCache.txt',d/'CMakeCache.txt')
   item={'source':str(source),'source_sha256':sha(data),'sha256':sha(payload),'patched':enabled=='1','patches':thunks if enabled=='1' else [],'layout_unchanged':True,'non_text_sections_byte_identical':True,'deployable':False}
   (d/'derivation.json').write_text(json.dumps(item,indent=2)+'\n');manifest['variants'][variant]=item
 (OUT/'derivation.json').write_text(json.dumps(manifest,indent=2)+'\n')
 print(json.dumps({v:{'sha256':x['sha256'],'patches':len(x['patches'])} for v,x in manifest['variants'].items()},indent=2))
if __name__=='__main__':prepare()
