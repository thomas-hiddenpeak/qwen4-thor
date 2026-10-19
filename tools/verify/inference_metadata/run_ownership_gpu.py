"""Bounded owner validation; run this entire driver under gpu_exclusive."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT/'.q4t-work/inference-metadata-20261020/candidate/gpu-v1'


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def main():
    OUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        shutil.copyfile(OUT.parent/'gpu-test', OUT/'executable')
        (OUT/'executable').chmod(0o755)
        for source in ['src/model/decode_metadata_upload.cpp',
                       'include/q4t/model/decode_metadata_upload.h',
                       'tools/verify/inference_metadata/ownership_gpu_test.cpp',
                       'tools/verify/inference_metadata/run_ownership_gpu.py']:
            shutil.copyfile(ROOT/source, OUT/Path(source).name)
        plan = dict(uploads=4, rows=[2, 1, 2, 1], streams=[0, 0, 1, 1],
                    copies=30, invalid_calls=1, delay_ms=50, timeout_seconds=60,
                    model_loads=0, retries=0, binary_sha256=sha(OUT/'executable'))
        (OUT/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        with (OUT/'run.log').open('w') as log:
            subprocess.run([str(OUT/'executable')], stdout=log,
                           stderr=subprocess.STDOUT, cwd=ROOT, timeout=60, check=True)
        state['status'] = 'PASS'
    except BaseException as error:
        state.update(status='FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUT/'run.json').write_text(json.dumps(state, indent=2)+'\n')
        (OUT/'files.json').write_text(json.dumps({p.name:sha(p) for p in OUT.iterdir()
             if p.is_file() and p.name != 'files.json'}, indent=2)+'\n')
    print((OUT/'run.log').read_text())


if __name__ == '__main__':
    main()
