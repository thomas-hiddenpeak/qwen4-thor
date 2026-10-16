"""Resolve direct calls to duplicate names by actual target address."""
import ast,collections,hashlib,json,re
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
SRC=ROOT/'.q4t-work/hc-up-host-instances-20261017'
manifest=json.loads((ROOT/'docs/evidence/hc-up-host-instances-20261017/retained-files.json').read_text())
rows={};groups={};addresses={}
for v in ('A','B'):
 p=SRC/f'{v}-instances.json';assert hashlib.sha256(p.read_bytes()).hexdigest()==manifest[str(p)]['sha256']
 rows[v]=json.loads(p.read_text());groups[v]=collections.defaultdict(list);addresses[v]=collections.defaultdict(list)
 for r in rows[v]:groups[v][r['name']].append(r);addresses[v][r['address']].append(r)
# Reuse only the checked normalizer definition, not its top-level audit side effects.
p=ROOT/'.q4t-work/hc-up-host-operands-20261017/audit.py'
tree=ast.parse(p.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='normalize')
namespace={'re':re};exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),namespace)
normalize=namespace['normalize']
def signature(v,r):return [normalize(s,r,groups[v]) for s in r['instructions']]
def target(v,text):
 m=re.fullmatch(r'(bl|b) UNRESOLVED:([0-9a-f]+) <([^>]+)>',text);assert m,text
 hits=[r for r in addresses[v][int(m[2],16)] if r['name']==m[3]]
 assert len(hits)==1,(v,text,hits)
 return hits[0]
changes=json.loads((ROOT/'.q4t-work/hc-up-host-operands-20261017/aligned-differences.json').read_text())
pairs={};sites=[]
for f in changes:
 for d in f['changes']:
  if not d['A'].startswith(('bl ','b ')):continue
  a,b=target('A',d['A']),target('B',d['B']);assert a['name']==b['name']
  key=(a['address'],b['address']);sites.append({'caller':f['name'],'index':d['index'],'A_target':hex(key[0]),'B_target':hex(key[1]),'name':a['name']})
  if key in pairs:continue
  sa,sb=signature('A',a),signature('B',b)
  diffs=[]
  if len(sa)==len(sb):
   diffs=[{'index':i,'A':x,'B':y} for i,(x,y) in enumerate(zip(sa,sb)) if x!=y]
  pairs[key]={'name':a['name'],'A_address':hex(a['address']),'B_address':hex(b['address']),
              'A_size':a['size'],'B_size':b['size'],'same_normalized_operands':sa==sb,
              'same_mnemonics':a['mnemonic_sha256']==b['mnemonic_sha256'],
              'aligned_differences':diffs if len(sa)==len(sb) else None}
counts=collections.Counter('same_normalized_operands' if p['same_normalized_operands'] else 'same_mnemonics_remaining_operands' if p['same_mnemonics'] else 'mnemonics_changed' for p in pairs.values())
resolved_sites=sum(pairs[(int(s['A_target'],16),int(s['B_target'],16))]['same_normalized_operands'] for s in sites)
report={'direct_branch_sites':len(sites),'distinct_target_pairs':len(pairs),'pair_categories':dict(counts),'sites_with_same_normalized_target_operands':resolved_sites,'pairs':list(pairs.values()),'sites':sites,'limits':['Same normalized function body is not same runtime address or cache behavior','Only direct duplicate-name branches in previously aligned caller functions','No guessed matching by name order; no indirect calls or whole-program equivalence claim'],'new_gpu_requests':0}
(OUT/'result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ('pairs','sites')},indent=2))
