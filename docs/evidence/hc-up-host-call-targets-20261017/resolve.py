"""Explain residual target differences with exact literals and target dependencies."""
import ast,collections,hashlib,importlib.util,json,re
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',ROOT/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
images={v:p.read_bytes() for v,p in elf.SOURCES.items()};sections={v:elf.sections(b) for v,b in images.items()}
for v,b in images.items():assert hashlib.sha256(b).hexdigest()==elf.EXPECTED[v]
rows={v:{r['address']:r for r in json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text())} for v in images}
def literal(v,address):
 s=sections[v]['.rodata'];assert s[3]<=address<s[3]+s[5]
 offset=s[4]+address-s[3];raw=images[v][offset:offset+min(4096,s[3]+s[5]-address)]
 end=raw.find(b'\0');assert 0<end<4096 and all(32<=c<127 for c in raw[:end])
 return raw[:end+1]
def constants(row):
 seq=row['instructions'];states={0:{}};queue=collections.deque([0])
 def key(reg):return 'x'+reg[1:] if re.fullmatch(r'[xw]\d+',reg) else reg
 while queue:
  i=queue.popleft();state=states[i];after=state.copy()
  text=seq[i].split('//',1)[0].strip();op=text.split()[0]
  operands=text.split(None,1)[1] if len(text.split(None,1))==2 else ''
  first=operands.split(',')[0].strip()
  if op in ('bl','blr'):
   for j in range(19):after.pop('x'+str(j),None)
  elif op not in ('b','br','ret','cmp','cmn','ccmp','tst','cbz','cbnz','tbz','tbnz','str','strb','strh','stur','stp','nop') and not op.startswith('b.'):
   after.pop(key(first),None)
   if op=='ldp':after.pop(key(operands.split(',')[1].strip()),None)
  if m:=re.match(r'adrp\s+(x\d+), ([0-9a-f]+)',text):after[m[1]]=(int(m[2],16),frozenset([i]))
  elif m:=re.match(r'add\s+(x\d+), (x\d+), #0x([0-9a-f]+)',text):
   if m[2] in state:
    value,origins=state[m[2]];after[m[1]]=(value+int(m[3],16),origins)
  elif m:=re.match(r'mov\s+(x\d+), (x\d+)$',text):
   if m[2] in state:after[m[1]]=state[m[2]]
  # Any GP base written back by a load/store becomes unknown.
  for m in re.finditer(r'\[(x\d+)[^\]]*\](?:!|,)',text):after.pop(m[1],None)
  targets=[]
  branch=op=='b' or op.startswith('b.') or op in ('cbz','cbnz','tbz','tbnz')
  if branch:
   m=re.search(r'([0-9a-f]+) <[^>]+>',text);assert m
   address=int(m[1],16)
   if row['address']<=address<row['address']+row['size']:targets.append((address-row['address'])//4)
  if op not in ('b','br','ret') and i+1<len(seq):targets.append(i+1)
  for j in targets:
   if j not in states:merged=after.copy()
   else:
    merged={k:(v[0],v[1]|after[k][1]) for k,v in states[j].items() if k in after and v[0]==after[k][0]}
   if j not in states or merged!=states[j]:states[j]=merged;queue.append(j)
 return states
state_cache={}
def pair(row,i):
 cachekey=(row['address'],row['name'],tuple(row['instructions']))
 # Addresses of A/B happen to differ here; callers additionally verify identity.
 if cachekey not in state_cache:state_cache[cachekey]=constants(row)
 b=re.match(r'add\s+(x\d+), (x\d+), #0x([0-9a-f]+)',row['instructions'][i]);assert b
 state=state_cache[cachekey].get(i,{})
 assert b[2] in state,(row['name'],i,'address not constant on all normal CFG paths')
 value,origins=state[b[2]]
 return (b[1],b[2]),value+int(b[3],16),origins
report=json.loads((OUT/'result.json').read_text());details={}
normalizer_path=ROOT/'.q4t-work/hc-up-host-operands-20261017/audit.py'
node=next(n for n in ast.parse(normalizer_path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='normalize')
ns={'re':re};exec(compile(ast.Module(body=[node],type_ignores=[]),str(normalizer_path),'exec'),ns)
groups={v:collections.defaultdict(list) for v in images}
for v in images:
 for r in json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text()):groups[v][r['name']].append(r)
queued={(int(p['A_address'],16),int(p['B_address'],16)) for p in report['pairs']}
def add_dependency(at,bt):
 if (at,bt) in queued:return
 a,b=rows['A'][at],rows['B'][bt];assert a['name']==b['name'] and a['mnemonic_sha256']==b['mnemonic_sha256']
 sa=[ns['normalize'](x,a,groups['A']) for x in a['instructions']]
 sb=[ns['normalize'](x,b,groups['B']) for x in b['instructions']]
 report['pairs'].append({'A_address':hex(at),'B_address':hex(bt),'name':a['name'],
  'aligned_differences':[{'index':i,'A':x,'B':y} for i,(x,y) in enumerate(zip(sa,sb)) if x!=y]})
 queued.add((at,bt))

for p in report['pairs']:
 key=(int(p['A_address'],16),int(p['B_address'],16));a,b=rows['A'][key[0]],rows['B'][key[1]]
 assert a['name']==b['name']==p['name'] and a['size']==b['size']
 changed={d['index']:d for d in p['aligned_differences']};covered=set();literals=[];deps=[]
 for i,d in changed.items():
  if d['A'].startswith('add '):
   ar,ap,aj=pair(a,i);br,bp,bj=pair(b,i);assert ar==br and aj==bj
   av,bv=literal('A',ap),literal('B',bp);assert av==bv
   covered.update(aj);covered.add(i);literals.append({'index':i,'A_address':hex(ap),'B_address':hex(bp),'text':av[:-1].decode(),'sha256_including_NUL':hashlib.sha256(av).hexdigest()})
  elif d['A'].startswith('bl UNRESOLVED:'):
   at=int(re.search(r'UNRESOLVED:([0-9a-f]+)',d['A'])[1],16)
   bt=int(re.search(r'UNRESOLVED:([0-9a-f]+)',d['B'])[1],16)
   deps.append((at,bt));covered.add(i);add_dependency(at,bt)
 assert not set(changed)-covered,(p['name'],set(changed)-covered)
 details[key]={'name':p['name'],'A':hex(key[0]),'B':hex(key[1]),'literal_references':literals,'dependencies':deps,'all_changed_operands_classified':True}
resolved=set(k for k,v in details.items() if not v['dependencies'])
while True:
 new={k for k,v in details.items() if all(tuple(d) in resolved for d in v['dependencies'])}
 if new<=resolved:break
 resolved|=new
assert resolved==details.keys(),[(k,v['dependencies']) for k,v in details.items() if k not in resolved]
result={'target_pairs':len(details),'direct_sites':report['direct_branch_sites'],'all_target_pair_differences_classified':True,'literal_references':sum(len(v['literal_references']) for v in details.values()),'nested_call_sites':sum(len(v['dependencies']) for v in details.values()),'details':list(details.values()),'limits':['Only static function strings and literal payload identity, not address or cache equivalence','Uses direct branch sites from mnemonic-aligned unique callers; excludes indirect calls and other instruction differences','Literal references may be in error branches; no dynamic branch reachability claimed'],'new_gpu_requests':0}
(OUT/'resolved.json').write_text(json.dumps(result,indent=2)+'\n')
print({k:v for k,v in result.items() if k not in ('details','limits')})
