"""Reuse sealed layer-independent HC FMA helper, without new compilation."""
from pathlib import Path
import hashlib,json,shutil
root=Path(__file__).resolve().parents[3];s=Path(__file__).resolve().parent
p=root/'.q4t-work/prepared/moe-scale-hc-attn-write-l2-20260924'
old=root/'.q4t-work/e2e/moe-scale-hc-attn-write-l1-20260924'
def sha(f):return hashlib.sha256(f.read_bytes()).hexdigest()
assert not p.exists();p.mkdir();manifest=json.loads((old/'artifact-binding.json').read_text());files={}
for n in ['write.cpp','common.h','write','build.log']:
 f=old/n;h=sha(f);assert h==manifest[n];shutil.copy2(f,p/n);files[n]=h
assert (p/'build.log').stat().st_size==0
(p/'reused-build.json').write_text(json.dumps({'source':str(old.relative_to(root)),'manifest_sha256':sha(old/'artifact-binding.json'),'files':files,'new_compilation':False},indent=2)+'\n')
for f in s.glob('*.in'):(p/f.name[:-3]).write_bytes(f.read_bytes())
(root/'.q4t-work/e2e/moe-scale-hc-attn-write-l2-20260924/reference').mkdir(parents=True)
print('Sealed generic FMA helper and old zero-warning build log verified; no new compilation.')
