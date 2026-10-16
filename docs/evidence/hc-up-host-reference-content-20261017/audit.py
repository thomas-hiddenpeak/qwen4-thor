"""Resolve retained adjacent address references without erasing content changes."""
import collections,hashlib,importlib.util,json,re,struct
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('elf',ROOT/'docs/evidence/hc-up-thunk-cause-20261016/prepare.py');elf=importlib.util.module_from_spec(spec);spec.loader.exec_module(elf)
sources=elf.SOURCES
images={v:p.read_bytes() for v,p in sources.items()}
for v,b in images.items():assert hashlib.sha256(b).hexdigest()==elf.EXPECTED[v]
sections={v:elf.sections(b) for v,b in images.items()}
def section(v,address):
 matches=[(n,s) for n,s in sections[v].items() if s[2]&2 and s[3]<=address<s[3]+s[5]]
 assert len(matches)<=1
 return matches[0] if matches else (None,None)
relocs={}
for v,b in images.items():
 symbols=sections[v]['.dynsym'];strings=sections[v]['.dynstr']
 names=b[strings[4]:strings[4]+strings[5]];dyn=[]
 for i in range(symbols[4],symbols[4]+symbols[5],24):
  name,info,other,shndx,value,size=struct.unpack_from('<IBBHQQ',b,i)
  dyn.append(names[name:].split(b'\0',1)[0].decode())
 relocs[v]={}
 for n,s in sections[v].items():
  if s[1]!=4:continue
  for i in range(s[4],s[4]+s[5],24):
   address,info,addend=struct.unpack_from('<QQq',b,i)
   index=info>>32;kind=info&0xffffffff
   relocs[v][address]={'type':kind,'symbol':dyn[index] if index else None,'addend':addend}

def describe(v,address):
 name,s=section(v,address);result={'section':name,'address':hex(address)}
 if address in relocs[v]:result['relocation']=relocs[v][address]
 if s and s[1]!=8:
  offset=s[4]+address-s[3];available=s[3]+s[5]-address
  data=images[v][offset:offset+min(available,4096)]
  # This classification records bytes through NUL, not an inferred object size.
  end=data.find(b'\0')
  if name=='.rodata' and 1<=end<4096 and all(32<=c<127 or c in (9,10,13) for c in data[:end]):
   result['nul_terminated_ascii']=data[:end].decode('ascii')
   result['literal_sha256']=hashlib.sha256(data[:end+1]).hexdigest()
 return result

# GNU unique (u) objects and function aliases were outside the earlier
# bBdDrR-only mapper. Retain all aliases, and reject globally ambiguous names.
all_symbols={};name_counts={}
for v in images:
 all_symbols[v]=[];name_counts[v]=collections.Counter()
 for line in (ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-symbols.txt').read_text().splitlines():
  f=line.split()
  if len(f)==4 and f[2] in 'bBdDrRuUvVwWtT':
   a,size=int(f[0],16),int(f[1],16)
   all_symbols[v].append((a,size,f[3],f[2]));name_counts[v][f[3]]+=1

def identities(v,address):
 found=[(n,address-a,size,t) for a,size,n,t in all_symbols[v] if a<=address<a+size]
 return sorted(found) if found and all(name_counts[v][x[0]]==1 for x in found) else None

functions={v:{r['name']:r for r in json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text())} for v in images}
# Only names selected by the preceding unique-function mapper are used here.
def readonly_load(v,ref,address):
 ins=functions[v][ref['function']]['instructions'][ref['index']]
 m=re.match(r'(ldr|ldrb)\s+([wxqds])\d+,',ins)
 name,sec=section(v,address)
 if not m or name!='.rodata':return None
 width=1 if m[1]=='ldrb' else {'w':4,'x':8,'q':16,'d':8,'s':4}[m[2]]
 if address+width>sec[3]+sec[5]:return None
 offset=sec[4]+address-sec[3]
 return {'width':width,'hex':images[v][offset:offset+width].hex()}

references=json.loads((ROOT/'.q4t-work/hc-up-host-operands-20261017/data-references.json').read_text())['references']
for v in images:
 counts_by_name=collections.Counter(r['name'] for r in json.loads((ROOT/f'.q4t-work/hc-up-host-instances-20261017/{v}-instances.json').read_text()))
 assert all(counts_by_name[r['function']]==1 for r in references)
result=[];counts=collections.Counter();section_pairs=collections.Counter()
for r in references:
 if r['same_unique_object_offset_size']:continue
 a=describe('A',int(r['A_target'],16));b=describe('B',int(r['B_target'],16))
 section_pairs[str((a['section'],b['section']))]+=1
 category='unresolved'
 ai,bi=identities('A',int(r['A_target'],16)),identities('B',int(r['B_target'],16))
 if ai is not None and ai==bi:
  category='same_unique_symbol_or_alias_set'
  a['symbol_identities']=ai;b['symbol_identities']=bi
 elif 'literal_sha256' in a and 'literal_sha256' in b:
  category='same_ascii_bytes_through_NUL' if a['literal_sha256']==b['literal_sha256'] else 'different_ascii_bytes'
 elif 'relocation' in a and 'relocation' in b:
  ar,br=a['relocation'],b['relocation']
  if ar['symbol'] and ar==br:category='same_dynamic_symbol_relocation'
  elif ar['type']==br['type']==1027:category='relative_relocation_needs_target_resolution'
  else:category='different_relocation'
 if category=='unresolved':
  al=readonly_load('A',r,int(r['A_target'],16));bl=readonly_load('B',r,int(r['B_target'],16))
  if al is not None and bl is not None:
   category='same_exact_readonly_load_bytes' if al==bl else 'different_readonly_load_bytes'
   a['loaded_bytes']=al;b['loaded_bytes']=bl
 counts[category]+=1
 result.append({'function':r['function'],'index':r['index'],'category':category,'A':a,'B':b})
# These named functions are on source forward call chains; not every branch or
# literal in them is necessarily reached by the fixture.
selected=('10MoEForward','20FullAttentionForward','19DecoderLayerForward','9RunLayers',
          'L22HyperConnectionMixImpl','15PleLayerForward')
focused=[r for r in result if any(x in r['function'] for x in selected)]
summary={'unresolved_input_references':len(result),'categories':dict(counts),'section_pairs':dict(section_pairs),
 'forward_function_reference_categories':dict(collections.Counter(r['category'] for r in focused)),
 'different_content_or_relocation':[r for r in result if r['category'] in ('different_ascii_bytes','different_relocation','different_readonly_load_bytes')],
 'limits':['ASCII equality is exact only through first NUL, not a proof of load width or branch reachability','Dynamic relocation equality identifies imported symbol/addend, not runtime pointer or cache equality','Only previously extracted adjacent ADRP references; not all memory references or host functions'],
 'new_gpu_requests':0}
(OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(OUT/'references.json').write_text(json.dumps(result,indent=2)+'\n')
(OUT/'forward-references.json').write_text(json.dumps(focused,indent=2)+'\n')
print(json.dumps(summary,indent=2)[:5500])
