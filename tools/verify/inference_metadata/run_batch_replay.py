"""Frozen multipath differential; invoke via seal_stage for one GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/metadata-followup-20261020'
OUT = STAGE / 'batch-replay-v1'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
PRIOR = ROOT / '.q4t-work/upstream-first-fork-20261019/gpu-v1/model-files.json'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def stat_identity(path):
    info = path.stat()
    return dict(bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                ctime_ns=info.st_ctime_ns, inode=info.st_ino, device=info.st_dev)


def check_model(records):
    for row in records:
        if any(row[key] != value for key, value in
               stat_identity(MODEL / row['path']).items()):
            raise ValueError('model identity changed: ' + row['path'])


def main():
    state = dict(status='PREPARING', started=time.time(), completed=[])
    try:
        if subprocess.check_output(['git', 'diff', 'b6870c0', '--', 'src', 'include'], cwd=ROOT):
            raise ValueError('candidate runtime changed')
        parent = STAGE / 'batch-parent-source'
        for name in subprocess.check_output(
                ['git', 'ls-tree', '-r', '--name-only', '48185ed', 'src', 'include'],
                cwd=ROOT, text=True).splitlines():
            if (parent / name).read_bytes() != subprocess.check_output(
                    ['git', 'show', '48185ed:' + name], cwd=ROOT):
                raise ValueError('parent runtime changed: ' + name)
        fixture = ROOT / 'tools/verify/inference_metadata/batch_replay.cpp'
        if fixture.read_bytes() != (parent / fixture.relative_to(ROOT)).read_bytes():
            raise ValueError('fixtures differ')
        shutil.copy2(fixture, OUT / 'fixture.cpp')
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        records = json.loads(PRIOR.read_text())
        check_model(records)
        # The prior full-model manifest did not include the MTP subdirectory.
        index = json.loads((MODEL / 'mtp/model.safetensors.index.json').read_text())
        files = {'config.json', 'model.safetensors.index.json',
                 *index['weight_map'].values()}
        for name in sorted(files):
            path = MODEL / 'mtp' / name
            before = stat_identity(path)
            digest = sha(path)
            if before != stat_identity(path):
                raise ValueError('MTP file changed while hashing')
            records.append(dict(path=str(path.relative_to(MODEL)),
                                sha256=digest, **before))
        save('model-files.json', records)
        binaries = {}
        for label, folder in [('parent', 'batch-parent-build'), ('candidate', 'build')]:
            source = STAGE / folder
            target = OUT / (label + '-executable')
            shutil.copy2(source / 'q4t_metadata_batch_replay', target)
            shutil.copy2(source / 'CMakeCache.txt', OUT / (label + '-CMakeCache.txt'))
            binaries[label] = sha(target)
        save('plan.json', dict(parent='48185ed', candidate='b6870c0',
             binaries=binaries, layers=4, main_loads=2, draft_loads=2,
             successful_main_forwards_per_side=9, expected_rejections_per_side=1,
             draft_extends_per_side=2, draft_chunks_per_side=6,
             expected_exact_files=78, timeout_per_side=900, retries=0,
             main_model_identity='prior payload hashes plus checked stat identity',
             mtp_model_identity='new payload hashes and stat identity',
             fixture_sha256=sha(fixture)))
        (OUT / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff'], cwd=ROOT))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        for label in ['parent', 'candidate']:
            check_model(records)
            state.update(status='RUNNING', current=label)
            save('run.json', state)
            with (OUT / (label + '.log')).open('w') as log:
                subprocess.run([str(OUT / (label + '-executable')), str(OUT / label)],
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=900)
            if sha(OUT / (label + '-executable')) != binaries[label]:
                raise ValueError('executable changed during run')
            state['completed'].append(label)
            save('run.json', state)
        names = sorted(str(p.relative_to(OUT / 'parent'))
                       for p in (OUT / 'parent').rglob('*') if p.is_file())
        other = sorted(str(p.relative_to(OUT / 'candidate'))
                       for p in (OUT / 'candidate').rglob('*') if p.is_file())
        if len(names) != 78 or names != other:
            raise ValueError('incomplete comparison file set')
        checks = {name: sha(OUT / 'parent' / name) == sha(OUT / 'candidate' / name)
                  for name in names}
        save('comparison.json', checks)
        if not all(checks.values()):
            raise ValueError('parent/candidate differ')
        check_model(records)
        state.update(status='PASS', bitexact_files=len(checks))
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save('run.json', state)


if __name__ == '__main__':
    main()
