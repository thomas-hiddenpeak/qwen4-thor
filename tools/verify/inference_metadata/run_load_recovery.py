"""One corrected sequential stage after v1 orchestration ENV_ABORT. No retries."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/megatron-reliability-20261020'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    state = dict(status='RUNNING', started=time.time(), retry=0)
    record = STAGE / 'recovery-v2.json'
    if record.exists():
        raise ValueError('preserve existing recovery')
    record.write_text(json.dumps(state, indent=2) + '\n')
    try:
        old = json.loads((STAGE / 'load-lease-v1/exit.json').read_text())
        if old['status'] != 'ENV_ABORT':
            raise ValueError('unexpected original phase status')
        subprocess.run(['python3', '-B',
                        'tools/verify/inference_metadata/run_load_acceptance.py',
                        '--output', str(STAGE / 'load-acceptance-v2')],
                       cwd=ROOT, check=True)
        vision = STAGE / 'vision-load-v1'
        plan = json.loads((vision / 'plan.json').read_text())
        for name, expected in plan['files'].items():
            if sha(ROOT / name) != expected:
                raise ValueError('vision input changed: ' + name)
        command = [str(vision / 'executable'),
                   '/home/rm01/models/dev/llm/garnermccloud/'
                   'Qwen3.8-Flash-Next-NVFP4-SSD-Stream']
        (vision / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
        with (vision / 'run.log').open('x') as log:
            subprocess.run(command, cwd=ROOT, stdout=log,
                           stderr=subprocess.STDOUT, timeout=180, check=True)
        for name, expected in plan['files'].items():
            if sha(ROOT / name) != expected:
                raise ValueError('vision input changed after run: ' + name)
        (vision / 'files.json').write_text(json.dumps({p.name: sha(p)
            for p in sorted(vision.iterdir()) if p.is_file()
            and p.name != 'files.json'}, indent=2) + '\n')
        state['status'] = 'PASS'
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        record.write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    main()
