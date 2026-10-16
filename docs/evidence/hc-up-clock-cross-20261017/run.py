"""Fixed eight-request crossover; no retries or adaptation to observed timing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path('/home/rm01/models/dev/qwen4-thor')
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'tools/evalscope'))
from gpu_clock_io import GpcIO, snapshot
from gpu_clock_journal import ClockJournal
from gpu_exclusive import common_lock
p=json.loads((OUT/'protocol.json').read_text())
config=json.loads((OUT/'config.json').read_text())
for path,digest in p['sha256'].items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest,path
marker=common_lock().with_name(common_lock().name+'.recovery-required')
io=GpcIO(config['identity'],config['range'][1],marker)
env=os.environ.copy()
for key in list(env):
    if key.startswith('Q4T_') or key in ('LD_PRELOAD','CUDA_INJECTION64_PATH','NVTX_INJECTION64_PATH'):
        env.pop(key)
(OUT/'environment.json').write_text(json.dumps({k:v for k,v in env.items() if k.startswith(('Q4T_','CUDA_')) or k in ('LD_PRELOAD','LD_LIBRARY_PATH')},indent=2)+'\n')
rows=[]
for label in p['order']:
    variant=label[1]
    target=config['range'] if label[0]=='O' else config['target']
    dest=OUT/'http'/label
    assert not dest.exists(),'No overwrite/retry'
    c=ClockJournal(marker,io.read,io.write,config['identity'],io.available)
    c.prepare(config['range'],target)
    c.apply()
    before=snapshot()
    assert before['identity']==config['identity'] and before['range']==target
    (OUT/f'{label}-clock-before.json').write_text(json.dumps(before,indent=2)+'\n')
    command=[str(ROOT/'tools/evalscope/.venv/bin/python3'),str(ROOT/'tools/evalscope/run_acceptance.py'),'--binary',p['binaries'][variant],'--mode','performance','--perf-lengths','204800','--perf-repeats','1','--fixtures',p['fixture'],'--reference',p['reference'],'--output',str(dest),'--model-dir',p['model'],'--startup-timeout','300','--allow-unqualified-binary','--port','8730','--oc-observer']
    print('START',label,flush=True)
    with (OUT/f'{label}-driver.log').open('w') as log:
        rc=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
    (OUT/f'{label}-exit.json').write_text(json.dumps({'command':command,'returncode':rc},indent=2)+'\n')
    after=snapshot()
    (OUT/f'{label}-clock-after.json').write_text(json.dumps(after,indent=2)+'\n')
    # Normal restore between cases; independent ExecStopPost covers exceptions.
    c.restore()
    assert after['identity']==config['identity'] and after['range']==target
    if rc:raise SystemExit(rc)
    results=json.loads((dest/'results.json').read_text())
    ex=json.loads((dest/'exit.json').read_text())
    assert ex['server']==0 and ex['completed']==1 and ex['http_output_checks_passed'] and ex['diagnostic_oc']
    assert len(results)==1 and results[0]['outputs']==[p['expected_output']]
    assert results[0]['prompt_sha256']==p['expected_prompt']
    counter=json.loads((dest/'context-204800/run-1/oc/counter.json').read_text())
    assert counter['complete'] and not counter['error']
    bounds=counter['boundary']
    assert bounds['leading_slack_ns']<=p['max_each_slack_ns'] and bounds['trailing_slack_ns']<=p['max_each_slack_ns']
    rows.append({'label':label,'variant':variant,'frequency':target,'delta':counter['delta'],'boundary':bounds,'output':results[0]['outputs'][0],'prompt':results[0]['prompt_sha256'],'metrics_diagnostic_only':results[0]['metrics']})
    (OUT/'observations.json').write_text(json.dumps(rows,indent=2)+'\n')
    print('COMPLETE',label,'OC',counter['delta'],flush=True)
