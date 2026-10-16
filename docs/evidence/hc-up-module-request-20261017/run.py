import hashlib,json,os,subprocess
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
p=json.loads((OUT/'protocol.json').read_text())
for path,sha in p['sha256'].items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,path
assert not (OUT/'http').exists()
env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
env.update(LD_PRELOAD=p['probe'],Q4T_DIAG_MODULE_OBSERVER='1')
(OUT/'environment.json').write_text(json.dumps({k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k=='LD_PRELOAD'},indent=2)+'\n')
base=ROOT/'.q4t-work/mtp-numerical-closure-20261016/http/off'
cmd=[str(ROOT/'tools/evalscope/.venv/bin/python3'),'tools/evalscope/run_acceptance.py','--binary',p['binary'],'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',str(base),'--reference',str(base/'results.json'),'--output',str(OUT/'http'),'--model-dir','/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream','--startup-timeout','300','--allow-unqualified-binary','--port','8730']
with (OUT/'runner.log').open('x') as log:rc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
(OUT/'runner-exit.json').write_text(json.dumps({'command':cmd,'returncode':rc},indent=2)+'\n')
raise SystemExit(rc)
