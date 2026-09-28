"""Check actual ModelOwner allocations, failures and repeated destruction."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[3]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--build', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--model', type=Path, required=True)
a = p.parse_args()
out = a.output.resolve()
assert any(out.is_relative_to(ROOT / x) for x in ['build', '.q4t-work'])
out.mkdir(parents=True, exist_ok=False)
shutil.copy2(__file__, out / 'run.py')
shutil.copy2(Path(__file__).with_name('check.cu'), out / 'check.cu')
for name in ['include/q4t/model/model_owner.h', 'src/model/model_owner.cpp', 'include/q4t/model/model.h', 'src/model/model.cu']:
    dest = out / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((ROOT / name).read_bytes())
libs = [a.build.resolve() / f'libq4t_{x}.a' for x in ['model','ple','quant','text','io','runtime']]
command = ['nvcc','-std=c++23','--expt-relaxed-constexpr','-O3','-arch=sm_110a','-ccbin=g++-14','-Xcompiler=-Wall,-Wextra','-I'+str(ROOT/'include'),str(out/'check.cu'),'-Xlinker=--wrap=cudaMalloc', '-Xlinker=--wrap=cudaFree', '-Xlinker=--wrap=cudaStreamSynchronize', '-Xlinker=--start-group']
command += [str(x) for x in libs]
command += ['-Xlinker=--end-group','-lcublasLt','-luring','-licui18n','-licuuc','-licudata','-ldl','-lpthread','-lrt','-o',str(out/'check')]
(out/'binding.json').write_text(json.dumps({'libraries':{str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in libs},'command':command},indent=2))
with (out/'build.log').open('w') as f: subprocess.run(command,stdout=f,stderr=subprocess.STDOUT,check=True)
assert 'warning' not in (out/'build.log').read_text().lower()
(out/'empty-index.json').write_text('{"weight_map":{}}')
with (out/'run.log').open('w') as f: result=subprocess.run([str(out/'check'),str(a.model.resolve()),str(out/'empty-index.json')],stdout=f,stderr=subprocess.STDOUT)
(out/'exit.json').write_text(json.dumps({'returncode':result.returncode}))
print((out/'run.log').read_text()[-3000:])
raise SystemExit(result.returncode)
