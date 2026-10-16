"""Normal-CFG address audit for six explicitly selected forward functions."""
import ast,collections,hashlib,importlib.util,json,re
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',ROOT/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
images={v:p.read_bytes() for v,p in elf.SOURCES.items()};sections={v:elf.sections(b) for v,b in images.items()}
for v,b in images.items():assert hashlib.sha256(b).hexdigest()==elf.EXPECTED[v]
rows={v:json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text()) for v in images}
p=OUT/'cfg.py';node=next(n for n in ast.parse(p.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='constants');ns={'re':re,'collections':collections};exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),ns)
objects={};counts={}
for v in images:
 objects[v]=[];counts[v]=collections.Counter()
 for line in (ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-symbols.txt').read_text().splitlines():
  f=line.split()
  if len(f)==4 and f[2] in 'bBdDrRuUvVwWtT':
   objects[v].append((int(f[0],16),int(f[1],16),f[3],f[2]));counts[v][f[3]]+=1

def describe(v,address,width):
 matches=[(n,address-a,size,t) for a,size,n,t in objects[v] if a<=address<a+size]
 result={'address':hex(address)}
 if matches and all(counts[v][x[0]]==1 for x in matches):result['symbols']=sorted(matches)
 ss=[(name,s) for name,s in sections[v].items() if s[2]&2 and s[3]<=address<s[3]+s[5]]
 assert len(ss)<=1
 if not ss:return result
 name,s=ss[0];result['section']=name
 if name=='.rodata':
  offset=s[4]+address-s[3];available=s[3]+s[5]-address;raw=images[v][offset:offset+min(4096,available)]
  if width and width<=available:result['load_bytes']=raw[:width].hex()
  end=raw.find(b'\0')
  if end>=0 and all(32<=c<127 or c in (9,10,13) for c in raw[:end]):result['NUL_bytes']=raw[:end+1].hex()
 return result

def ref(row,state,i):
 text=row['instructions'][i]
 a=re.match(r'add\s+(x\d+), (x\d+), #0x([0-9a-f]+)',text)
 m=re.match(r'(ldr|ldrb|str|strb)\s+([wxqds])\d+, \[(x\d+)(?:, #(\d+))?\]$',text)
 if a and a[2] in state:
  val,orig=state[a[2]];return val+int(a[3],16),None,orig
 if m and m[3] in state:
  val,orig=state[m[3]];width=1 if m[1].endswith('b') else {'w':4,'x':8,'q':16,'d':8,'s':4}[m[2]]
  return val+int(m[4] or '0'),width if m[1].startswith('ldr') else None,orig
 return None

selected=('10MoEForward','20FullAttentionForward','19DecoderLayerForward','9RunLayers','L22HyperConnectionMixImpl','15PleLayerForward')
changes={r['name']:r['changes'] for r in json.loads((ROOT/'.q4t-work/hc-up-host-operands-20261017/aligned-differences.json').read_text())}
results=[]
for a in rows['A']:
 if not a['name'].startswith('_ZN3q4t5model') or not any(n in a['name'] for n in selected):continue
 bs=[b for b in rows['B'] if b['name']==a['name']];assert len(bs)==1;b=bs[0]
 assert a['mnemonic_sha256']==b['mnemonic_sha256']
 assert not any(s.split()[0]=='br' for r in (a,b) for s in r['instructions'])
 sa,sb=ns['constants'](a),ns['constants'](b);refs=[];covered=set()
 for i in range(len(a['instructions'])):
  ar,br=ref(a,sa.get(i,{}),i),ref(b,sb.get(i,{}),i)
  if ar is None or br is None or ar[0]==br[0]:continue
  ap,aw,ao=ar;bp,bw,bo=br;assert aw==bw
  ad,bd=describe('A',ap,aw),describe('B',bp,bw)
  category='unresolved'
  if 'symbols' in ad and ad['symbols']==bd.get('symbols'):category='same_symbol_identity'
  elif aw and ad.get('load_bytes') is not None and ad['load_bytes']==bd.get('load_bytes'):category='same_load_bytes'
  elif not aw and ad.get('NUL_bytes') is not None and ad['NUL_bytes']==bd.get('NUL_bytes'):category='same_NUL_bytes'
  if category!='unresolved':covered.update(ao|bo);covered.add(i)
  refs.append({'index':i,'A':ad,'B':bd,'category':category,'A_definitions':sorted(ao),'B_definitions':sorted(bo)})
 relevant=[d for d in changes[a['name']] if d['A'].split()[0] in ('adrp','add','ldr')]
 unresolved=[d for d in relevant if d['index'] not in covered]
 results.append({'function':a['name'],'changed_address_instructions':len(relevant),'associated_changed_address_instructions':len(relevant)-len(unresolved),'remaining_changed_instructions':unresolved,'references':refs})
assert len(results)==6
report={'functions':results,'summary':{'functions':6,'changed_address_instructions':sum(r['changed_address_instructions'] for r in results),'associated_changed_address_instructions':sum(r['associated_changed_address_instructions'] for r in results),'remaining_changed_address_instructions':sum(len(r['remaining_changed_instructions']) for r in results),'reference_categories':dict(collections.Counter(ref['category'] for r in results for ref in r['references']))},'limits':['Normal direct CFG only, no exception-unwind propagation','No indirect tail branches in selected functions; no indirect call target equivalence','Matching content/identity is not timing or layout equivalence','No inferred load width for ADD-produced pointers; only NUL prefix or named-object identity','Association means at least one classified use, not all uses of a definition','Only exact eight-byte SP slots modeled; calls, unknown stores and SP changes clear slots; exception landing pads not modeled'], 'new_gpu_requests':0}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report['summary'],indent=2))
