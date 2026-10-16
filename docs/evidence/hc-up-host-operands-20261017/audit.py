"""Conservative operand comparison; data addresses deliberately NOT erased."""
import collections,hashlib,json,re
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
SRC=ROOT/'.q4t-work/hc-up-host-instances-20261017'
records={v:json.loads((SRC/f'{v}-instances.json').read_text()) for v in ('A','B')}
# Check the retained source hashes, not merely filenames.
manifest=json.loads((ROOT/'docs/evidence/hc-up-host-instances-20261017/retained-files.json').read_text())
for v in records:
 p=SRC/f'{v}-instances.json';assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest[str(p)]['sha256']
groups={v:collections.defaultdict(list) for v in records}
for v,rows in records.items():
 for r in rows:groups[v][r['name']].append(r)

def normalize(text,row,all_groups):
 # Remove only disassembler comments. Keep every arithmetic/address operand.
 text=text.split('//',1)[0].strip()
 mnemonic=text.split()[0]
 if mnemonic=='b' or mnemonic=='bl' or mnemonic.startswith('b.') or mnemonic in ('cbz','cbnz','tbz','tbnz'):
  match=re.search(r'\b([0-9a-f]+) <([^>]+)>$',text)
  if match:
   target=int(match[1],16);name=match[2]
   if row['address']<=target<row['address']+row['size']:
    label='SELF+'+hex(target-row['address'])
   elif name.endswith('@plt'):
    label='PLT:'+name
   elif name in all_groups and len(all_groups[name])==1:
    label='FUNCTION:'+name
   else:
    label='UNRESOLVED:'+match[0]
   text=text[:match.start()]+label
 else:
  # Angle-bracket labels are objdump annotations, not encoded operands.
  text=re.sub(r'\s*<[^>]*>', '', text)
 return re.sub(r'\s+',' ',text)

def signature(row,by_name):return tuple(normalize(s,row,by_name) for s in row['instructions'])
# The normalization must retain non-control-flow immediates and register changes.
r={'address':0x1000,'size':32}
assert normalize('b.eq\t100c <local+0xc>',r,{})=='b.eq SELF+0xc'
assert normalize('bl\t2220 <free@plt>',r,{})=='bl PLT:free@plt'
assert normalize('add\tx0, x1, #0x10',r,{})!=normalize('add\tx0, x1, #0x20',r,{})
assert normalize('adrp\tx0, 12000 <x>',r,{})!=normalize('adrp\tx0, 13000 <x>',r,{})
assert normalize('adrp x0, 12000 <x>',r,{})==normalize('adrp x0, 12000 <y>',r,{})
assert 'UNRESOLVED:' in normalize('bl\t2220 <duplicate>',r,{'duplicate':[{},{}]})
matched=0;changed=[];aligned=[];counts=collections.Counter()
for name in sorted(set(groups['A'])|set(groups['B'])):
 ga=groups['A'].get(name,[]);gb=groups['B'].get(name,[])
 ca=collections.Counter(signature(r,groups['A']) for r in ga);cb=collections.Counter(signature(r,groups['B']) for r in gb)
 matched+=sum((ca&cb).values())
 if ca!=cb:changed.append({'name':name,'A_count':len(ga),'B_count':len(gb)})
 if len(ga)!=1 or len(gb)!=1 or ga[0]['mnemonic_sha256']!=gb[0]['mnemonic_sha256']:continue
 a,b=ga[0],gb[0];sa=signature(a,groups['A']);sb=signature(b,groups['B']);assert len(sa)==len(sb)
 diffs=[]
 for i,(x,y) in enumerate(zip(sa,sb)):
  if x==y:continue
  opcode=x.split()[0];counts[opcode]+=1
  diffs.append({'index':i,'A':x,'B':y,'A_pc':hex(a['address']+i*4),'B_pc':hex(b['address']+i*4)})
 if diffs:aligned.append({'name':name,'changes':diffs})
result={'matched_operand_signature_instances':matched,'changed_name_groups':len(changed),'changed_groups':changed,
        'aligned_same_mnemonic_unique_functions_with_operand_changes':len(aligned),
        'aligned_changed_instruction_counts_by_opcode':dict(counts),
        'limits':['Branch normalization covers only intra-function offsets, PLT labels and unique exact function names','Data addresses/ADRP+ADD/GOT offsets remain raw; no address-only difference is assumed semantically irrelevant','Ambiguous duplicates and size/mnemonic changes are retained, not guessed as equal','Identical normalized instruction strings do not imply equal instruction cache or address behavior'],
        'new_gpu_requests':0,'normalizer_checks_passed':6}
(OUT/'result.json').write_text(json.dumps(result,indent=2)+'\n')
(OUT/'aligned-differences.json').write_text(json.dumps(aligned,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='changed_groups'},indent=2))
