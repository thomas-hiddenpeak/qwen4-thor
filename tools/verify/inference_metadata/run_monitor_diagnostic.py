"""Proposed single-request diagnostic. Requires a newly authorized GPU budget.

Run only through seal_stage.py with output monitor-diagnostic-v1. No retries.
The observer reads /proc for guard-owned NVIDIA queries; it issues no GPU API.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/inference-metadata-20261020'
OUT = STAGE / 'monitor-diagnostic-v1'


def main():
    guard = os.getppid()
    binary = STAGE / 'candidate-before-capacity-q4t'
    expected = json.loads((STAGE / 'quality-v1/plan.json').read_text())[
        'candidate_sha256']
    if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
        raise ValueError('original candidate identity changed')
    fixtures = OUT / 'fixtures'
    fixtures.mkdir(exist_ok=False)
    source = STAGE / 'quality-v1/reference-on/inputs'
    manifest = json.loads((source / 'manifest.json').read_text())
    rows = (source / 'requests.jsonl').read_text().splitlines()
    selected = [i for i, row in enumerate(manifest) if row['length'] == 204800]
    if len(selected) != 1 or len(rows) != len(manifest):
        raise ValueError('ambiguous frozen fixture')
    index = selected[0]
    (fixtures / 'manifest.json').write_text(json.dumps([manifest[index]]) + '\n')
    (fixtures / 'requests.jsonl').write_text(rows[index] + '\n')
    stop = threading.Event()

    def observe():
        # Bound volume and lifetime even if the runner stops responding.
        with (OUT / 'query-processes.jsonl').open('w') as log:
            for _ in range(4500):
                if stop.is_set():
                    break
                for process in Path('/proc').iterdir():
                    if not process.name.isdigit():
                        continue
                    try:
                        if (process / 'comm').read_text().strip() != 'nvidia-smi':
                            continue
                        status = (process / 'status').read_text()
                        parent = next(line.split()[1] for line in
                                      status.splitlines() if line.startswith('PPid:'))
                        if int(parent) != guard:
                            continue
                        record = dict(time=time.time(), pid=int(process.name),
                                      status=status)
                        for field in ['wchan', 'syscall', 'stack']:
                            try:
                                record[field] = (process / field).read_text()[:8192]
                            except OSError as error:
                                record[field] = repr(error)
                        log.write(json.dumps(record) + '\n')
                        log.flush()
                    except (OSError, StopIteration):
                        continue
                stop.wait(0.2)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
               '--mode', 'quality', '--output', str(OUT / 'run'),
               '--binary', str(binary), '--fixtures', str(fixtures),
               '--model-dir', '/home/rm01/models/dev/llm/garnermccloud/'
               'Qwen3.8-Flash-Next-NVFP4-SSD-Stream', '--mtp',
               '--startup-timeout', '300', '--port', '8740',
               '--allow-unqualified-binary']
    state = dict(status='RUNNING', purpose='ENVIRONMENT_DIAGNOSTIC_ONLY',
                 requests=1, loads=1, retries=0, binary_sha256=expected,
                 guard_parent=guard, command=command)
    try:
        subprocess.run(command, cwd=ROOT, timeout=900, check=True)
        state['status'] = 'REQUEST_COMPLETED_NOT_FULL_ACCEPTANCE'
    except BaseException as error:
        state.update(status='INTERRUPTED_OR_FAILED', error=repr(error))
        raise
    finally:
        stop.set()
        observer.join(timeout=2)
        (OUT / 'diagnostic.json').write_text(json.dumps(state, indent=2) + '\n')


if __name__ == '__main__':
    main()
