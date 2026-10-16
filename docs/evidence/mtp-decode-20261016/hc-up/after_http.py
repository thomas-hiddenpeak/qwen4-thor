import hashlib,json,os,select,subprocess
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
r=Path(__file__).resolve().parent
fd=os.pidfd_open(989823)
select.select([fd],[],[])
os.close(fd)
for name in ['quality-on','on','off','quality-off']:
    p=r/(name+'-exit.json')
    if not p.exists() or json.loads(p.read_text())['returncode']:
        (r/'after-http-stop.json').write_text(json.dumps({'reason':'HTTP gates not all passed','missing_or_failed':name})+'\n')
        raise SystemExit(2)
if (r/'gate-stop.json').exists(): raise SystemExit(2)
required=['hc_up_four_rows_exact','smallm_gemm_wiring','model_verify_ckpt','model_verify_multi_ckpt','model_verify_ckpt_samepath','mtp_spec_accept_states']
(r/'affected.required').write_text('\n'.join(required)+'\n')
def run(name,cmd,env=None):
    with (r/(name+'.log')).open('w') as log:
        rc=subprocess.run(cmd,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
    (r/(name+'-exit.json')).write_text(json.dumps({'returncode':rc,'command':cmd},indent=2)+'\n')
    if rc: raise SystemExit(rc)
run('build-tests',['cmake','--build','build','--target','q4t_tests','--parallel','4'])
if any(w in (r/'build-tests.log').read_text().lower() for w in ['warning:', 'error:']): raise SystemExit('build warnings/errors')
(r/'tests.sha256').write_text(hashlib.sha256((root/'build/q4t_tests').read_bytes()).hexdigest()+'\n')
run('affected',['build/q4t_tests','--required-list',str(r/'affected.required')])
run('numerics',['build/q4t_tests','mtp_verify_numerics'])
env=os.environ.copy();env['Q4T_LAYER_SUBTIMING']='1';env['Q4T_MTP_CYCLE_TIMING']='1'
run('diagnostic',[str(root/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--mode','performance','--perf-lengths','4096','--perf-repeats','1','--fixtures',str(root/'.q4t-work/mtp-numerical-closure-20261016/http/on'),'--mtp-default','--startup-timeout','300','--allow-unqualified-binary','--port','8731','--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--output',str(r/'diagnostic')],env)
