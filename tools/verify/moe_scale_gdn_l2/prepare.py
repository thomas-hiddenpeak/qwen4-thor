"""Reuse sealed norm/recurrence, build layer-selectable parameter transform."""
from pathlib import Path
import hashlib,json,shutil,subprocess
root=Path(__file__).resolve().parents[3];s=root/'tools/verify/moe_scale_gdn_l2';p=root/'.q4t-work/prepared/moe-scale-gdn-l2-20260924';old=root/'.q4t-work/e2e/moe-scale-gdn-l1-20260924'
def sha(f):
 h=hashlib.sha256()
 with f.open('rb') as z:
  for b in iter(lambda:z.read(4*1024*1024),b''):h.update(b)
 return h.hexdigest()
assert not p.exists();p.mkdir();m=json.loads((old/'artifact-binding.json').read_text());files={}
for n in ['norm.cpp','math.cu','replay.cpp','norm','math','replay']:
 f=old/n;h=sha(f);assert h==m[n];shutil.copy2(f,p/n);files[n]=h
assert sha(old/'build.log')==m['build.log'] and (old/'build.log').stat().st_size==0
shutil.copy2(old/'build.log',p/'reused-build.log')
(p/'reused-build.json').write_text(json.dumps({'source':str(old.relative_to(root)),'manifest_sha256':sha(old/'artifact-binding.json'),'files':files,'old_build_log_sha256':m['build.log'],'new_compilation':'parameters only'},indent=2)+'\n')
for f in s.glob('*.in'):(p/f.name[:-3]).write_bytes(f.read_bytes())
subprocess.run(['clang-format','-i',str(p/'parameters.cu')],check=True);(s/'parameters.cu.in').write_bytes((p/'parameters.cu').read_bytes())
with (p/'build.log').open('w') as log:subprocess.run(['nvcc','-std=c++20','-O2','-arch=sm_110a','-ccbin=g++-14','-Xcompiler=-Wall,-Wextra,-ffp-contract=off',str(p/'parameters.cu'),'-o',str(p/'parameters')],stdout=log,stderr=subprocess.STDOUT,check=True)
assert (p/'build.log').stat().st_size==0
(root/'.q4t-work/e2e/moe-scale-gdn-l2-20260924/reference').mkdir(parents=True)
print('Frozen generic helpers verified; layer-selectable parameter helper built without warnings.')
