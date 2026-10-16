"""Resolve duplicate CUDA handle instances via actual registration constructors."""
import collections,hashlib,importlib.util,json,re,struct
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',ROOT/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
modules={};images={};sections={}
for v,path in elf.SOURCES.items():
 data=path.read_bytes();assert hashlib.sha256(data).hexdigest()==elf.EXPECTED[v]
 images[v]=data;sections[v]=elf.sections(data)
def read(v,address,size):
 found=[s for s in sections[v].values() if s[2]&2 and s[1]!=8 and s[3]<=address and address+size<=s[3]+s[5]]
 assert len(found)==1,(v,hex(address),size)
 s=found[0];start=s[4]+address-s[3];return images[v][start:start+size]
def string(v,address):
 b=bytearray()
 for i in range(4096):
  x=read(v,address+i,1)
  if x==b'\0':return b.decode()
  b+=x
 raise AssertionError('unterminated CUDA name')
for v in images:
 rows=json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text())
 constructors=[r for r in rows if r['name']=='_ZL24__sti____cudaRegisterAllv']
 assert len(constructors)==(22 if v=='A' else 23)
 result=[]
 for ctor in constructors:
  registers={};item={'constructor':hex(ctor['address']),'functions':[]}
  for text in ctor['instructions']:
   ins=text.split('//',1)[0].strip()
   if m:=re.match(r'adrp\s+(x\d+), ([0-9a-f]+)',ins):registers[m[1]]=int(m[2],16)
   elif m:=re.match(r'add\s+(x\d+), (x\d+), #0x([0-9a-f]+)',ins):
    value=registers.get(m[2]);registers[m[1]]=value+int(m[3],16) if isinstance(value,int) else None
   elif m:=re.match(r'mov\s+(x\d+), (x\d+)$',ins):registers[m[1]]=registers.get(m[2])
   elif m:=re.match(r'str\s+x0, \[(x\d+), #(\d+)\]',ins):
    if registers.get('x0')=='HANDLE':
     assert isinstance(registers.get(m[1]),int)
     item['handle_address']=hex(registers[m[1]]+int(m[2]))
   elif m:=re.match(r'bl\s+[0-9a-f]+ <([^>]+)>',ins):
    name=m[1]
    if name=='__cudaRegisterFatBinary@plt':
     wrapper=registers['x0'];assert isinstance(wrapper,int)
     magic,version,payload,unused=struct.unpack('<IIQQ',read(v,wrapper,24))
     assert magic==0x466243b1 and version==1 and unused==0
     magic2,version2,header_size,payload_size=struct.unpack('<IHHQ',read(v,payload,16))
     assert magic2==0xba55ed50 and version2==1 and header_size==16
     blob=read(v,payload,header_size+payload_size)
     item.update(wrapper=hex(wrapper),fatbin_address=hex(payload),fatbin_bytes=len(blob),fatbin_sha256=hashlib.sha256(blob).hexdigest())
    elif name=='__cudaRegisterFunction@plt':
     item['functions'].append(string(v,registers['x3']))
    for i in range(19):registers.pop(f'x{i}',None)
    if name=='__cudaRegisterFatBinary@plt':registers['x0']='HANDLE'
  # A CUDA translation unit may register a fatbin with no host functions.
  assert 'handle_address' in item and 'fatbin_sha256' in item, (v,item)
  result.append(item)
 assert len({m['handle_address'] for m in result})==len(result)
 modules[v]=result
ma={m['fatbin_sha256']:m for m in modules['A']};mb={m['fatbin_sha256']:m for m in modules['B']}
assert len(ma)==22 and len(mb)==23 and ma.keys()<mb.keys()
for sha in ma:assert ma[sha]['functions']==mb[sha]['functions']
extra=[mb[s] for s in mb.keys()-ma.keys()];assert len(extra)==1 and any('HcUpFourRowsKernel' in n for n in extra[0]['functions'])
by_handle={v:{m['handle_address']:m for m in group} for v,group in modules.items()}
remaining=json.loads((ROOT/'docs/evidence/hc-up-host-reference-content-20261017/remaining.json').read_text())['references']
resolved=[];unresolved=[]
for r in remaining:
 a=by_handle['A'].get(r['A']['address']);b=by_handle['B'].get(r['B']['address'])
 if a and b:
  assert a['fatbin_sha256']==b['fatbin_sha256'] and a['functions']==b['functions']
  resolved.append({'function':r['function'],'index':r['index'],'A_handle':r['A']['address'],'B_handle':r['B']['address'],'same_fatbin_sha256':a['fatbin_sha256']})
 else:unresolved.append(r)
report={'modules':modules,'common_fatbins_byte_identical':22,'extra_HC_module':extra[0],
 'references_resolved_to_same_module_handle':len(resolved),'resolved':resolved,'remaining':unresolved,
 'limits':['Static constructor dataflow and full fatbin bytes identify module roles, not runtime handle pointer values or cache costs','Only matched handle base references, no inference for adjacent globals','No GPU run or root-cause proof']}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'common_identical_modules':22,'resolved_handles':len(resolved),'remaining':len(unresolved),'remaining_sections':dict(collections.Counter(r['A']['section'] for r in unresolved))},indent=2))
