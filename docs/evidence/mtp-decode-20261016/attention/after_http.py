import hashlib,json,os,re,select,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
r=Path(__file__).resolve().parent
fd=os.pidfd_open(1003078);select.select([fd],[],[]);os.close(fd)
for name in ['quality-on','on','off','quality-off']:
    p=r/(name+'-exit.json')
    if not p.exists() or json.loads(p.read_text())['returncode']:
        (r/'after-http-stop.json').write_text(json.dumps({'reason':'HTTP gates not all passed','missing_or_failed':name})+'\n')
        raise SystemExit(2)
if (r/'gate-stop.json').exists(): raise SystemExit(2)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
for name in ['quality-on','on','off','quality-off']:
    assert (r/name/'binary.sha256').read_text().strip()==sha(root/'build/q4t')
def run(name,cmd,env=None):
    with (r/(name+'.log')).open('w') as log:
        rc=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
    (r/(name+'-exit.json')).write_text(json.dumps({'returncode':rc,'command':cmd},indent=2)+'\n')
    if rc:raise SystemExit(rc)
source='src/model/qsa_decode.cu'
old=subprocess.check_output(['git','show','28a3783:'+source],cwd=root,text=True)
new=(root/source).read_text()
assert new==subprocess.check_output(['git','show','481cd94:'+source],cwd=root,text=True)
(r/'baseline-qsa.cu.txt').write_text(old);(r/'candidate-qsa.cu.txt').write_text(new)
for header in ['include/q4t/model/full_attention.h','include/q4t/model/qsa_decode.h','include/q4t/status.h']:
    assert (root/header).read_bytes()==subprocess.check_output(['git','show','28a3783:'+header],cwd=root)
unit='#include <array>\n#include <vector>\n#include <cstdio>\n'+old.replace('namespace q4t::model {','namespace q4t::model::ordered_softmax_baseline {')+'\n'+new+'\n'+(r/'ordered-softmax.cu.in').read_text()
(r/'comparison.cu').write_text(unit)
cache=(root/'build/CMakeCache.txt').read_text()
compiler=re.search(r'^CMAKE_CUDA_COMPILER:[^=]+=([^\n]+)',cache,re.M)[1]
host=re.search(r'^CMAKE_CUDA_HOST_COMPILER:[^=]+=([^\n]+)',cache,re.M)[1]
cmd=[compiler,'-std=c++23','--expt-relaxed-constexpr','-O3','-DNDEBUG','-arch=sm_110a','-ccbin',host,'-Xcompiler=-Wall,-Wextra','-I'+str(root/'include'),str(r/'comparison.cu'),'-o',str(r/'comparison')]
(r/'numerical-manifest.json').write_text(json.dumps({'baseline':'28a3783','candidate':'481cd94','binary_sha256':sha(root/'build/q4t'),'old_source_sha256':sha(r/'baseline-qsa.cu.txt'),'new_source_sha256':sha(r/'candidate-qsa.cu.txt'),'harness_sha256':sha(r/'ordered-softmax.cu.in'),'command':cmd},indent=2)+'\n')
run('build-comparison',cmd)
if any(w in (r/'build-comparison.log').read_text().lower() for w in ['warning:','error:']):raise SystemExit('comparison build warnings/errors')
run('comparison',[str(r/'comparison')])
run('build-tests',['cmake','--build','build','--target','q4t_tests','--parallel','4'])
if any(w in (r/'build-tests.log').read_text().lower() for w in ['warning:','error:']):raise SystemExit('test build warnings/errors')
(r/'tests.sha256').write_text(sha(root/'build/q4t_tests')+'\n')
run('numerics',['build/q4t_tests','mtp_verify_numerics'])
env=os.environ.copy();env['Q4T_LAYER_SUBTIMING']='1';env['Q4T_MTP_CYCLE_TIMING']='1'
run('diagnostic',[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--mode','performance','--perf-lengths','4096','--perf-repeats','1','--fixtures',str(root/'.q4t-work/mtp-numerical-closure-20261016/http/on'),'--mtp-default','--startup-timeout','300','--allow-unqualified-binary','--port','8731','--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--output',str(r/'diagnostic')],env)
