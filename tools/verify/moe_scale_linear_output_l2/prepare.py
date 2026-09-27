"""Reuse sealed norm; build layer-selectable nonlinear and projection helpers."""
from pathlib import Path
import hashlib,json,shutil,subprocess
root=Path(__file__).resolve().parents[3];s=root/'tools/verify/moe_scale_linear_output_l2';p=root/'.q4t-work/prepared/moe-scale-linear-output-l2-20260924';old=root/'.q4t-work/e2e/moe-scale-linear-output-l1-20260924'
def sha(f):
 h=hashlib.sha256()
 with f.open('rb') as z:
  for b in iter(lambda:z.read(4*1024*1024),b''):h.update(b)
 return h.hexdigest()
assert not p.exists();p.mkdir();m=json.loads((old/'artifact-binding.json').read_text());files={}
for n in ['norm.cpp','norm']:
 f=old/n;h=sha(f);assert h==m[n];shutil.copy2(f,p/n);files[n]=h
assert sha(old/'build.log')==m['build.log'] and (old/'build.log').stat().st_size==0
shutil.copy2(old/'build.log',p/'reused-build.log')
(p/'reused-build.json').write_text(json.dumps({'source':str(old.relative_to(root)),'manifest_sha256':sha(old/'artifact-binding.json'),'files':files,'old_build_log_sha256':m['build.log'],'new_compilation':'nonlinear and replay only'},indent=2)+'\n')
for f in s.glob('*.in'):(p/f.name[:-3]).write_bytes(f.read_bytes())
for name in ['nonlinear.cu','replay.cpp']:
 subprocess.run(['clang-format','-i',str(p/name)],check=True);(s/(name+'.in')).write_bytes((p/name).read_bytes())
with (p/'build.log').open('w') as log:
 subprocess.run(['nvcc','-std=c++20','-O2','-arch=sm_110a','-ccbin=g++-14','-Xcompiler=-Wall,-Wextra,-ffp-contract=off',str(p/'nonlinear.cu'),'-o',str(p/'nonlinear')],stdout=log,stderr=subprocess.STDOUT,check=True)
 subprocess.run(['g++-14','-std=c++20','-O3','-Wall','-Wextra','-ffp-contract=off','-I/usr/local/cuda/include',str(p/'replay.cpp'),'-L/usr/local/cuda/lib64','-Wl,-rpath,/usr/local/cuda/lib64','-lcudart','-lcublasLt','-o',str(p/'replay')],stdout=log,stderr=subprocess.STDOUT,check=True)
assert (p/'build.log').stat().st_size==0
(root/'.q4t-work/e2e/moe-scale-linear-output-l2-20260924/reference').mkdir(parents=True)
print('Frozen norm verified; nonlinear/replay built without warnings.')
