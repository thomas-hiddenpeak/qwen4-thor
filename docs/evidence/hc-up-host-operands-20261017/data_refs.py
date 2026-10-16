"""Resolve only adjacent ADRP-based references to uniquely named nm objects."""
import collections,json,re
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor');src=root/'.q4t-work/hc-up-host-instances-20261017';out=Path(__file__).resolve().parent
rows={v:json.loads((src/f'{v}-instances.json').read_text()) for v in ('A','B')}
objects={};names={}
for v in rows:
 objects[v]=[];names[v]=collections.Counter()
 for line in (src/f'{v}-symbols.txt').read_text().splitlines():
  f=line.split()
  if len(f)==4 and f[2] in 'bBdDrR':
   item=(int(f[0],16),int(f[1],16),f[3]);objects[v].append(item);names[v][f[3]]+=1

def reference(seq,i):
 if i==0:return None
 m=re.match(r'adrp\s+(x\d+), ([0-9a-f]+)',seq[i-1])
 if not m:return None
 reg,page=m[1],int(m[2],16)
 add=re.match(r'add\s+x\d+, '+reg+r', #0x([0-9a-f]+)',seq[i])
 mem=re.match(r'(?:ldr|str|ldrb|strb)\s+[wxqds]\d+, \['+reg+r'(?:, #(\d+))?\]',seq[i])
 if add:return page+int(add[1],16)
 if mem:return page+int(mem[1] or '0')
 return None

def object_at(v,address):
 matches=[(n,address-a,size) for a,size,n in objects[v] if a<=address<a+size and names[v][n]==1]
 return matches[0] if len(matches)==1 else None

groups={v:collections.defaultdict(list) for v in rows}
for v in rows:
 for r in rows[v]:groups[v][r['name']].append(r)
classified=[]
for name,a_group in groups['A'].items():
 b_group=groups['B'].get(name,[])
 if len(a_group)!=1 or len(b_group)!=1:continue
 a,b=a_group[0],b_group[0]
 if a['mnemonic_sha256']!=b['mnemonic_sha256']:continue
 for i in range(1,len(a['instructions'])):
  ar=reference(a['instructions'],i);br=reference(b['instructions'],i)
  if ar is None or br is None or ar==br:continue
  ao,bo=object_at('A',ar),object_at('B',br)
  classified.append({'function':name,'index':i,'A_target':hex(ar),'B_target':hex(br),'A_object':ao,'B_object':bo,'same_unique_object_offset_size':bool(ao is not None and ao==bo)})
report={'adjacent_shifted_references':len(classified),'same_unique_named_object_offset_size':sum(x['same_unique_object_offset_size'] for x in classified),'unresolved_or_different':sum(not x['same_unique_object_offset_size'] for x in classified),'limits':['Only adjacent ADRP + ADD/load/store; no backward register propagation','Matching object identity does not prove equal runtime contents, physical placement, or cost','Unresolved references are retained, not erased'],'references':classified}
(out/'data-references.json').write_text(json.dumps(report,indent=2)+'\n')
print({k:v for k,v in report.items() if k!='references'})
