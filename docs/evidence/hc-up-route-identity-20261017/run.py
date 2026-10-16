import hashlib,json,os,subprocess
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor');OUT=Path(__file__).resolve().parent
p=json.loads((OUT/'protocol.json').read_text())
for path,digest in p['sha256'].items():assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,path
env=os.environ.copy()
for key in list(env):
 if key.startswith('Q4T_') or key in ('LD_PRELOAD','CUDA_INJECTION64_PATH','NVTX_INJECTION64_PATH'):env.pop(key)
(OUT/'environment.json').write_text(json.dumps({k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k in ('LD_PRELOAD','LD_LIBRARY_PATH')},indent=2)+'\n')
for index,v in enumerate(p['order']):
 dest=OUT/'http'/v;trace=OUT/'routes'/v
 assert not dest.exists() and not trace.exists(),'No overwrite/retry'
 command=[str(ROOT/'tools/evalscope/.venv/bin/python3'),str(ROOT/'tools/evalscope/run_acceptance.py'),'--binary',p['binaries'][v],'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',p['fixture'],'--reference',p['reference'],'--output',str(dest),'--model-dir',p['model'],'--startup-timeout','300','--allow-unqualified-binary','--port','8730','--moe-trace-dir',str(trace),'--moe-trace-workload',p['workload'],'--moe-trace-max-mib','1024']
 print('START',v,flush=True)
 with (OUT/f'{v}-driver.log').open('w') as log:rc=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
 (OUT/f'{v}-exit.json').write_text(json.dumps({'command':command,'returncode':rc},indent=2)+'\n')
 if rc:raise SystemExit(rc)
 manifest=json.loads((trace/'manifest.json').read_text());assert manifest['complete'] and manifest['failure']=='none' and manifest['requests_started']==manifest['requests_published']==1
 row=json.loads((dest/'results.json').read_text());assert len(row)==1
 assert row[0]['outputs']==[p['expected_output']]
 assert row[0]['prompt_sha256']==p['expected_prompt']
 print('COMPLETE',v,'output and manifest valid',flush=True)
