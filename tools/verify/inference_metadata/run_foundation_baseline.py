"""Frozen, bounded five-tier descriptive baseline; invoke under GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
from run_budget_experiment import run_owned_runner

STAGE = ROOT / '.q4t-work/offload-foundation-20261020'
OUT = STAGE / 'baseline-v1/run'
PACKAGE = ROOT / '.q4t-work/releases/offload-foundation-20261020'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
FIXTURES = ROOT / '.q4t-work/offload-autonomous-20261003/matrix-off/http'
LENGTHS = [1024, 4096, 8192, 45056, 204800]
EXPECTED = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    if sha(PACKAGE / 'q4t') != EXPECTED:
        raise ValueError('release identity changed')
    inputs = OUT / 'fixtures'
    for length in LENGTHS:
        target = inputs / f'context-{length}/requests.jsonl'
        target.parent.mkdir(parents=True)
        shutil.copy2(FIXTURES / f'context-{length}/requests.jsonl', target)
    paths = [p for folder in ('src', 'include', 'tools/evalscope')
             for p in (ROOT / folder).rglob('*')
             if p.is_file() and (folder != 'tools/evalscope' or
                 p.suffix == '.py' and '.venv' not in p.parts)]
    paths += [Path(__file__), ROOT / 'CMakeLists.txt']
    identities = {}
    for path in paths:
        name = str(path.relative_to(ROOT))
        target = OUT / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        identities[name] = sha(path)
    save(OUT / 'sources.json', identities)
    save(OUT / 'plan.json', dict(binary_sha256=EXPECTED, lengths=LENGTHS,
        repeats=3, output_tokens=256, loads=2, requests=30, max_len=208896,
        modes=['resident', 'offload'], mtp=False, max_seq=1, max_prefill=8192,
        offload=dict(C=256, L2=16, mirror=8, workers=16, streams=1,
                     chunk_order=0, partition=0, hot_list=None),
        timeout_seconds_per_mode=5400, automatic_retries=0,
        cache='inherited; no eviction; fixed resident then offload order',
        memory='no dedicated cgroup or host/cache cap; counters overlap',
        io='process read_bytes and device/cgroup observations, no expert-only physical attribution',
        purpose='descriptive baseline, no optimization or strict RAM acceptance',
        fixtures={str(p.relative_to(inputs)): sha(p) for p in inputs.rglob('*.jsonl')},
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()))
    state = dict(status='RUNNING', started=time.time(), completed=[])
    save(OUT / 'state.json', state)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env.update(Q4T_MOE_L2_SLOTS='16', Q4T_MOE_MIRROR_K='8',
               Q4T_MOE_LOAD_THREADS='16', Q4T_MOE_STREAMS='1',
               Q4T_MOE_CHUNK_ORDER='0', Q4T_MOE_PARTITION='0')
    try:
        for mode, capacity in [('resident', 0), ('offload', 256)]:
            for name, digest in identities.items():
                if sha(ROOT / name) != digest:
                    raise ValueError('source changed: ' + name)
            if sha(PACKAGE / 'q4t') != EXPECTED:
                raise ValueError('binary changed')
            folder = OUT / mode
            folder.mkdir()
            http, memory = folder / 'http', folder / 'memory'
            ready, stop = folder / 'monitor.ready', folder / 'monitor.stop'
            monitor_command = [sys.executable, '-B', 'tools/evalscope/monitor_memory.py',
                '--model-dir', str(MODEL), '--pid-file', str(http / 'server.pid'),
                '--phase-file', str(http / 'memory-phase.txt'), '--out', str(memory),
                '--interval', '1', '--gpu-interval', '10', '--file-cache-mode', 'endpoints',
                '--ready-file', str(ready), '--stop-file', str(stop)]
            command = [sys.executable, '-B', 'tools/evalscope/run_acceptance.py',
                '--mode', 'performance', '--output', str(http), '--binary', str(PACKAGE / 'q4t'),
                '--model-dir', str(MODEL), '--fixtures', str(inputs), '--port', '8740',
                '--startup-timeout', '600', '--request-deadline-ms', '1800000',
                '--max-len', '208896', '--moe-resident-slots', str(capacity),
                '--perf-lengths', ','.join(map(str, LENGTHS)), '--perf-repeats', '3']
            save(folder / 'commands.json', dict(monitor=monitor_command, runner=command))
            state['current'] = mode
            save(OUT / 'state.json', state)
            with (folder / 'monitor.log').open('w') as log:
                monitor = subprocess.Popen(monitor_command, cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 90
                    while not ready.exists():
                        if monitor.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('monitor failed to become ready')
                        time.sleep(.2)
                    with (folder / 'runner.log').open('w') as runner_log:
                        rc = run_owned_runner(command, cwd=ROOT, env=env, log=runner_log,
                            timeout=5400, evidence=folder / 'runner-process.json')
                    if rc:
                        raise RuntimeError('runner failed: ' + str(rc))
                finally:
                    stop.touch()
                    try:
                        monitor.wait(timeout=90)
                    except subprocess.TimeoutExpired:
                        monitor.terminate()
                        try:
                            monitor.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            monitor.kill()
                            monitor.wait(timeout=5)
                        raise
                if monitor.returncode:
                    raise RuntimeError('monitor failed: ' + str(monitor.returncode))
            end = json.loads((http / 'exit.json').read_text())
            if end['server'] != 0 or not end['http_output_checks_passed']:
                raise RuntimeError('HTTP completion checks failed')
            state['completed'].append(mode)
            save(OUT / 'state.json', state)
        for name, digest in identities.items():
            if sha(ROOT / name) != digest:
                raise ValueError('source changed during baseline: ' + name)
        state['status'] = 'MEASURED_NOT_OPTIMIZATION_ACCEPTANCE'
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        save(OUT / 'state.json', state)


if __name__ == '__main__':
    main()
