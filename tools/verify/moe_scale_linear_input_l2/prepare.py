"""Reuse sealed generic helpers; no production build or new observer."""
from pathlib import Path
import hashlib,json,shutil
root=Path(__file__).resolve().parents[3]
s=root/'tools/verify/moe_scale_linear_input_l2';p=root/'.q4t-work/prepared/moe-scale-linear-input-l2-20260924';old=root/'.q4t-work/e2e/moe-scale-linear-input-l1-20260924'
def sha(f):
 h=hashlib.sha256()
 with f.open('rb') as z:
  for b in iter(lambda:z.read(4*1024*1024),b''):h.update(b)
 return h.hexdigest()
assert not p.exists();p.mkdir();m=json.loads((old/'artifact-binding.json').read_text());files={}
for name in ['gemm.cpp','gemm','build.log']:
 f=old/name;h=sha(f);assert h==m[name];shutil.copy2(f,p/name);assert sha(p/name)==h;files[name]=h
assert (p/'build.log').stat().st_size==0
for f in s.glob('*.in'):(p/f.name[:-3]).write_bytes(f.read_bytes())
(p/'reused-build.json').write_text(json.dumps({'source':str(old.relative_to(root)),'manifest_sha256':sha(old/'artifact-binding.json'),'files':files,'new_compilation':False},indent=2)+'\n')
(root/'.q4t-work/e2e/moe-scale-linear-input-l2-20260924/reference').mkdir(parents=True)
print('Generic frozen helpers verified and copied; new layer2 operands will be bound by run.py.')
