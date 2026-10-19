"""One bounded lifetime diagnostic. Invoke the whole driver under GPU lease."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / '.q4t-work/inference-metadata-20261020/transfer-probe-v1/run'


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def main():
    OUTPUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        shutil.copyfile(OUTPUT.parent/'executable', OUTPUT/'executable')
        (OUTPUT/'executable').chmod(0o755)
        for name in ['transfer_probe.cpp', 'run_transfer_probe.py']:
            shutil.copyfile(Path(__file__).with_name(name), OUTPUT/name)
        plan = dict(cases=16, integers=[1, 4, 8192, 626688],
                    sources=['pageable', 'pinned'], reuse=['before_wait', 'after_wait'],
                    delay_ms=50, timeout_seconds=60, retries=0, model_loads=0,
                    binary_sha256=sha(OUTPUT/'executable'))
        (OUTPUT/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        with (OUTPUT/'raw.json').open('w') as out, (OUTPUT/'stderr.log').open('w') as err:
            subprocess.run([str(OUTPUT/'executable')], cwd=ROOT, stdout=out,
                           stderr=err, timeout=60, check=True)
        result = json.loads((OUTPUT/'raw.json').read_text())
        assert result['status'] == 'PASS' and len(result['cases']) == 16
        for case in result['cases']:
            if case['wait']:
                assert case['original'] == case['count']
        state.update(status='PASS', note='Diagnostic observations, not proof of model corruption or universal CUDA behavior.')
    except BaseException as error:
        state.update(status='FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUTPUT/'run.json').write_text(json.dumps(state, indent=2)+'\n')
        (OUTPUT/'files.json').write_text(json.dumps({p.name:sha(p) for p in OUTPUT.iterdir()
            if p.is_file() and p.name != 'files.json'}, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
