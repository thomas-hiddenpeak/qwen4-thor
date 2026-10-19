"""Only the interrupted MTP quality group; no ordinary-decode resampling."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
ROOT=Path(__file__).resolve().parents[3]
STAGE=ROOT/'.q4t-work/inference-metadata-20261020'
OUT=STAGE/'quality-on-v2'


def sha(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    source=STAGE/'quality-v1'
    binary=STAGE/'build/q4t'
    identity=json.loads((source/'plan.json').read_text())['candidate_sha256']
    if sha(binary)!=identity:raise ValueError('candidate changed')
    shutil.copytree(source/'reference-on',OUT/'reference')
    shutil.copyfile(Path(__file__),OUT/'stage-driver.py')
    shutil.copyfile(binary,OUT/'candidate-binary')
    command=['python3','-B','tools/evalscope/run_acceptance.py','--mode','quality',
             '--output',str(OUT/'run'),'--binary',str(binary),'--model-dir',
             '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream',
             '--fixtures',str(OUT/'reference/inputs'),'--reference',str(OUT/'reference/results.json'),
             '--startup-timeout','300','--port','8740','--allow-unqualified-binary','--mtp']
    plan=dict(requests=11,model_loads=1,retries=0,binary_sha256=identity,
              prior_env_abort=str(source),off_repeated=False,command=command)
    (OUT/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    state=dict(status='RUNNING',started=time.time())
    try:
        subprocess.run(command,cwd=ROOT,timeout=1800,check=True)
        rows=json.loads((OUT/'run/results.json').read_text())
        if len(rows)!=11 or not all(r['success']==1 for r in rows):raise ValueError('incomplete quality')
        if sha(binary)!=identity:raise ValueError('candidate changed')
        state['status']='PASS'
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error,KeyboardInterrupt) else 'FAIL',error=repr(error))
        raise
    finally:
        state['finished']=time.time()
        (OUT/'run-state.json').write_text(json.dumps(state,indent=2)+'\n')


if __name__=='__main__':main()
