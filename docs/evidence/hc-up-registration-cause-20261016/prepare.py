"""OFF-only diagnostic: skip exactly the HC CUDA constructor, no relink."""
import hashlib,importlib.util,json,shutil,struct,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');out=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',root/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
sha=lambda b:hashlib.sha256(b).hexdigest()
binary=elf.SOURCES['B'];data=binary.read_bytes();assert sha(data)==elf.EXPECTED['B']
sec=elf.sections(data)
def offset(va):
 matches=[s[4]+va-s[3] for s in sec.values() if s[1]!=8 and s[3]<=va<s[3]+s[5] and s[2]&2];assert len(matches)==1;return matches[0]
# Validate the constructor identity using its registration string, host stub,
# exact instruction words, and .rela.dyn init-array entry, not name alone.
assert data[offset(0x136d78):].split(b'\0',1)[0]==b'_ZN3q4t5model49_GLOBAL__N__a4d86fa5_16_hc_up_small_m_cu_d321225518HcUpFourRowsKernelEPKtS3_Ptf'
for va,word in {0x9020:0xd100c3ff,0x9044:0xb0000963,0x9048:0x9135e063,0x9064:0xf00002c1,0x9068:0x912eb021,0x906c:0x97fff965}.items():assert struct.unpack_from('<I',data,offset(va))[0]==word
r=sec['.rela.dyn'];rel=[struct.unpack_from('<QQq',data,i) for i in range(r[4],r[4]+r[5],24)];assert (0x3ef098,0x403,0x9020) in rel
ia=sec['.init_array'];assert ia[3]<=0x3ef098<ia[3]+ia[5]
changed=bytearray(data);struct.pack_into('<I',changed,offset(0x9020),0xd65f03c0) # RET before stack adjustment
assert len(changed)==len(data)
assert all(a==b or offset(0x9020)<=i<offset(0x9020)+4 for i,(a,b) in enumerate(zip(data,changed)))
for name,s in sec.items():
 if name!='.text':assert data[s[4]:s[4]+s[5]]==changed[s[4]:s[4]+s[5]]
manifest={'scope':'OFF-only diagnostic, never deploy or execute MTP; HC kernel unregistered','variants':{}}
for name,source,payload in [('A',elf.SOURCES['A'],elf.SOURCES['A'].read_bytes()),('B',binary,data),('C',binary,bytes(changed))]:
 assert sha(source.read_bytes())==elf.EXPECTED['A' if name=='A' else 'B']
 d=out/name;d.mkdir();(d/'q4t').write_bytes(payload);(d/'q4t').chmod(0o755);shutil.copy2(source.parent/'CMakeCache.txt',d/'CMakeCache.txt')
 item={'source':str(source),'source_sha256':sha(source.read_bytes()),'sha256':sha(payload),'deployable':False,'patched':name=='C','patch':{'va':'0x9020','old':'0xd100c3ff','new':'0xd65f03c0'} if name=='C' else None,'relative_layout_unchanged':True}
 manifest['variants'][name]=item;(d/'derivation.json').write_text(json.dumps(item,indent=2)+'\n')
(out/'derivation.json').write_text(json.dumps(manifest,indent=2)+'\n');print(json.dumps(manifest,indent=2))
