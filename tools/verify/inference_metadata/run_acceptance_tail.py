"""Frozen cancellation plus four five-tier matrices, under one GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time
ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT/'.q4t-work/inference-metadata-20261020'
OUT = STAGE/'acceptance-tail-v1'
PRIOR = ROOT/'.q4t-work/b1b-20261019'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    quality = STAGE/'quality-v1'
    if json.loads((quality/'run.json').read_text())['status'] != 'PASS':
        raise ValueError('complete quality prerequisite missing')
    OUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING',started=time.time())
    try:
        binaries = {'parent':STAGE/'parent-build/q4t', 'candidate':STAGE/'build/q4t'}
        hashes = {label:sha(path) for label,path in binaries.items()}
        for label,path in binaries.items(): shutil.copyfile(path,OUT/(label+'-binary'))
        shutil.copyfile(Path(__file__),OUT/'driver.py')
        fixtures = OUT/'fixtures'
        fixtures.mkdir()
        for length in [1024,4096,8192,45056,204800]:
            target = fixtures/('context-'+str(length))
            target.mkdir()
            shutil.copyfile(PRIOR/'candidate-off'/target.name/'requests.jsonl',target/'requests.jsonl')
        order=['parent-off','candidate-off','candidate-on','parent-on']
        plan=dict(order=order,requests_per_tier=3,tiers=[1024,4096,8192,45056,204800],
                  performance_requests=60,cancellation_generation_requests=20,
                  cancellation_control_posts=9,model_loads=5,retries=0,
                  binary_sha256=hashes,resolution_band='NOT_ESTABLISHED',
                  acceptance='No performance acceptance inferred from point estimates.')
        (OUT/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
        def run(name,command,timeout):
            for label,path in binaries.items():
                if sha(path)!=hashes[label]:raise ValueError('binary changed')
            with (OUT/(name+'-driver.log')).open('w') as log:
                subprocess.run(command,cwd=ROOT,env=env,stdout=log,
                               stderr=subprocess.STDOUT,timeout=timeout,check=True)
        run('cancellation',['python3','-B','tools/evalscope/run_request_cancellation.py',
            '--binary',str(binaries['candidate']),'--output',str(OUT/'cancellation'),
            '--quality-run',str(quality/'off'),'--performance-run',str(PRIOR/'candidate-off')],1800)
        result=json.loads((OUT/'cancellation/summary.json').read_text())
        if result['failure'] or result['server_exit']!=0:raise ValueError('cancellation failed')
        for name in order:
            label,mode=name.split('-')
            command=['python3','-B','tools/evalscope/run_acceptance.py',
                     '--mode','performance','--output',str(OUT/name),
                     '--binary',str(binaries[label]),'--model-dir',MODEL,
                     '--fixtures',str(fixtures),'--startup-timeout','300',
                     '--port','8740','--allow-unqualified-binary']
            if mode=='on':command.append('--mtp')
            run(name,command,7200)
        observations={}
        for mode in ['off','on']:
            parent=json.loads((OUT/('parent-'+mode)/'results.json').read_text())
            candidate=json.loads((OUT/('candidate-'+mode)/'results.json').read_text())
            if len(parent)!=5 or len(candidate)!=5:raise ValueError('incomplete matrix')
            comparisons=[]
            for a,b in zip(parent,candidate):
                if a['length']!=b['length'] or a['prompt_sha256']!=b['prompt_sha256'] or a['outputs']!=b['outputs']:
                    raise ValueError('matrix identity/output mismatch')
                if len(a['metrics'])!=3 or len(b['metrics'])!=3:raise ValueError('incomplete repeats')
                row={'length':a['length'],'parent':a['metrics'],'candidate':b['metrics']}
                for metric in ['ttft','decode_tps']:
                    av=statistics.median(x[metric] for x in a['metrics'])
                    bv=statistics.median(x[metric] for x in b['metrics'])
                    row[metric+'_median_change_pct']=100*(bv/av-1)
                comparisons.append(row)
            observations[mode]=comparisons
        (OUT/'observations.json').write_text(json.dumps(observations,indent=2)+'\n')
        state.update(status='MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING',
                     statistical_performance='INSUFFICIENT_EVIDENCE_RESOLUTION_NOT_ESTABLISHED')
    except BaseException as error:
        state.update(status='FAIL',error=repr(error))
        raise
    finally:
        state['finished']=time.time()
        (OUT/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        files={str(p.relative_to(OUT)):sha(p) for p in OUT.rglob('*') if p.is_file()}
        (OUT/'files.json').write_text(json.dumps(files,indent=2)+'\n')
    print('Measurements complete; acceptance pending',flush=True)


if __name__=='__main__':main()
