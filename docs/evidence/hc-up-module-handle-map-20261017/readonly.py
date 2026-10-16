"""Check exact access spans for the remaining six rodata target pairs."""
import hashlib,importlib.util,json,struct
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',ROOT/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
images={v:p.read_bytes() for v,p in elf.SOURCES.items()};sections={v:elf.sections(b) for v,b in images.items()}
for v,b in images.items():assert hashlib.sha256(b).hexdigest()==elf.EXPECTED[v]
def read(v,a,n):
 s=sections[v]['.rodata'];assert s[3]<=a and a+n<=s[3]+s[5]
 offset=s[4]+a-s[3];return images[v][offset:offset+n]
ranges={0x13d8f8:(0x13ce58,1,'empty NUL string; equality claim limited to NUL byte'),
        0x1424f4:(0x141a54,52,'stbi format: unsigned index <=0x19, halfword table'),
        0x1424c0:(0x141a20,52,'stbi format16: unsigned index <=0x19, halfword table'),
        0x141df0:(0x141350,48,'RunGenerate: LDP Q30/Q29 at base and LDR Q31 at +32'),
        0x141e40:(0x1413a0,32,'MtpForward: LDP Q30/Q31 at base'),
        0x142d40:(0x1422a0,32,'ComputeMemoryBudget: LD1 {V30.16B-V31.16B}')}
remaining=json.loads((OUT/'result.json').read_text())['remaining'];results=[]
for r in remaining:
 a=int(r['A']['address'],16);b=int(r['B']['address'],16)
 expected,n,basis=ranges[a];assert b==expected
 left,right=read('A',a,n),read('B',b,n);assert left==right,(r,left.hex(),right.hex())
 if n==1:assert left==b'\0'
 results.append({'function':r['function'],'index':r['index'],'A':hex(a),'B':hex(b),'bytes':n,'sha256':hashlib.sha256(left).hexdigest(),'basis':basis})
report={'references':results,'reference_count':len(results),'unique_target_pairs':len({r['A'] for r in results}),
 'limits':['Empty-string equality covers one NUL, not any wider unrelated memory read','Table byte equality does not imply equal code addresses or timing','CLI generate, MTP and media functions are not asserted reached by off serve fixture','This closes only the previously selected adjacent reference batch'], 'new_gpu_requests':0}
(OUT/'readonly.json').write_text(json.dumps(report,indent=2)+'\n');print('All 21 remaining reference spans match; six distinct target pairs.')
