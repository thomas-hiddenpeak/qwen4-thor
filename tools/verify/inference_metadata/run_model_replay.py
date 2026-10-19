"""Exact parent/candidate four-layer replay. Whole stage requires GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT/'.q4t-work/inference-metadata-20261020'
OUT = STAGE/'model-replay-v1'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
PRIOR = ROOT/'.q4t-work/upstream-first-fork-20261019/gpu-v1/model-files.json'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def check_model(records):
    for row in records:
        st = (MODEL/row['path']).stat()
        for field, value in [('bytes', st.st_size), ('mtime_ns', st.st_mtime_ns),
                             ('ctime_ns', st.st_ctime_ns), ('inode', st.st_ino), ('device', st.st_dev)]:
            if row[field] != value:
                raise ValueError('model metadata changed: '+row['path'])


def main():
    OUT.mkdir(exist_ok=False)
    state = dict(status='PREPARING', started=time.time())
    try:
        records = json.loads(PRIOR.read_text())
        check_model(records)
        shutil.copyfile(PRIOR, OUT/'model-files.json')
        shutil.copyfile(Path(__file__), OUT/'driver.py')
        shutil.copyfile(Path(__file__).with_name('model_replay.cpp'), OUT/'fixture.cpp')
        binaries = {}
        for label, build in [('parent', 'parent-build'), ('candidate', 'build')]:
            destination = OUT/(label+'-executable')
            shutil.copyfile(STAGE/build/'q4t_metadata_replay', destination)
            destination.chmod(0o755)
            binaries[label] = sha(destination)
        plan = dict(parent_commit='73aab36', candidate_commit='f01e8fa',
                    layers=4, model_loads=2, prefills_per_binary=5,
                    packed_decode_rows=[4,2,1,1], retries=0,
                    binaries=binaries, model_identity='prior full hashes plus unchanged stat identity before/after',
                    timeout_per_binary_seconds=600)
        (OUT/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
        (OUT/'worktree.patch').write_bytes(subprocess.check_output(['git','diff'],cwd=ROOT))
        env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        for label in binaries:
            with (OUT/(label+'.log')).open('w') as log:
                subprocess.run([str(OUT/(label+'-executable')),str(OUT/label)],
                               stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env,
                               timeout=600,check=True)
        names = sorted(str(p.relative_to(OUT/'parent')) for p in (OUT/'parent').rglob('*') if p.is_file())
        other = sorted(str(p.relative_to(OUT/'candidate')) for p in (OUT/'candidate').rglob('*') if p.is_file())
        if len(names) != 28 or names != other:
            raise ValueError('missing replay files')
        checks = {name:sha(OUT/'parent'/name)==sha(OUT/'candidate'/name) for name in names}
        (OUT/'comparison.json').write_text(json.dumps(checks,indent=2)+'\n')
        if not all(checks.values()):
            raise ValueError('parent/candidate differs')
        check_model(records)
        state.update(status='PASS', bitexact_files=len(checks))
    except BaseException as error:
        state.update(status='FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        (OUT/'run.json').write_text(json.dumps(state,indent=2)+'\n')
        files={str(p.relative_to(OUT)):sha(p) for p in OUT.rglob('*') if p.is_file() and p.name != 'files.json'}
        (OUT/'files.json').write_text(json.dumps(files,indent=2)+'\n')
    print('PASS parent/candidate 28 bitexact files',flush=True)


if __name__ == '__main__':
    main()
