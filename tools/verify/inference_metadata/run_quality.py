"""Fixed two-mode HTTP quality stage, held under one external GPU lease."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT/'.q4t-work/inference-metadata-20261020'
OUT = STAGE/'quality-v1'
PRIOR = ROOT/'.q4t-work/b1b-20261019'
BINARY = STAGE/'build/q4t'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main():
    OUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        identity = sha(BINARY)
        shutil.copyfile(BINARY, OUT/'candidate-binary')
        shutil.copyfile(Path(__file__), OUT/'driver.py')
        references = {}
        for mode in ['off','on']:
            source = PRIOR/('quality-'+mode)
            rows = json.loads((source/'results.json').read_text())
            if len(rows) != 11 or not all(row['success'] == 1 for row in rows):
                raise ValueError('reference quality incomplete')
            destination = OUT/('reference-'+mode)
            destination.mkdir()
            shutil.copyfile(source/'results.json', destination/'results.json')
            shutil.copytree(source/'inputs', destination/'inputs')
            references[mode] = sha(destination/'results.json')
        plan = dict(modes=['off','on'], requests_per_mode=11, total_requests=22,
                    model_loads=2, retries=0, candidate_sha256=identity,
                    reference_sha256=references, timeout_per_mode_seconds=1800)
        (OUT/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
        for mode in ['off','on']:
            if sha(BINARY) != identity: raise ValueError('candidate changed')
            reference = OUT/('reference-'+mode)
            command = ['python3','-B','tools/evalscope/run_acceptance.py',
                       '--mode','quality','--output',str(OUT/mode),
                       '--binary',str(BINARY),'--model-dir',MODEL,
                       '--fixtures',str(reference/'inputs'),
                       '--reference',str(reference/'results.json'),
                       '--startup-timeout','300','--port','8740',
                       '--allow-unqualified-binary']
            if mode == 'on': command.append('--mtp')
            with (OUT/(mode+'-driver.log')).open('w') as log:
                subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                               timeout=1800,check=True)
            rows = json.loads((OUT/mode/'results.json').read_text())
            if len(rows) != 11 or not all(row['success']==1 for row in rows):
                raise ValueError('quality incomplete '+mode)
        if sha(BINARY) != identity: raise ValueError('candidate changed')
        state['status'] = 'PASS'
    except BaseException as error:
        state.update(status='FAIL',error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUT/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        files={str(p.relative_to(OUT)):sha(p) for p in OUT.rglob('*') if p.is_file()}
        (OUT/'files.json').write_text(json.dumps(files,indent=2)+'\n')
    print('PASS HTTP quality off/on 11+11',flush=True)


if __name__ == '__main__':
    main()
