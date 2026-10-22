"""One frozen stage. Always invoke through gpu_exclusive.py; no retries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import threading

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT.parent
OUT = ROOT / '.q4t-work/acceptance-v1'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def sample_memory(stop):
    with (OUT / 'unified-memory.jsonl').open('w') as out:
        while not stop.is_set():
            record = dict(time=time.time(), meminfo=Path('/proc/meminfo').read_text(),
                          processes={})
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    name = (entry / 'comm').read_text().strip()
                    if name.startswith('q4t'):
                        record['processes'][entry.name] = (entry / 'status').read_text()
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass
            out.write(json.dumps(record) + '\n')
            out.flush()
            stop.wait(1)


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    OUT = args.output.resolve()
    if not OUT.is_relative_to(ROOT / '.q4t-work'):
        raise ValueError('stage output must be in source/.q4t-work/')
    plan = json.loads(args.plan.read_text())
    OUT.mkdir(parents=True, exist_ok=False)
    state = dict(status='RUNNING', started=time.time(), completed=[])
    deadline = time.monotonic() + plan['stage_timeout_seconds']
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    stop = threading.Event()
    sampler = threading.Thread(target=sample_memory, args=(stop,), daemon=True)
    sampler.start()
    try:
        for path, expected in plan['files'].items():
            if sha(Path(path)) != expected:
                raise RuntimeError('frozen identity changed: ' + path)
        save('plan.json', plan)
        for step in plan['steps']:
            name = step['name']
            state['current'] = name
            save('state.json', state)
            step_env = env | step.get('env', {})
            save(name + '-command.json', step)
            with (OUT / (name + '-driver.log')).open('w') as log:
                subprocess.run(step['command'], cwd=ROOT, env=step_env,
                               stdout=log, stderr=subprocess.STDOUT,
                               timeout=min(step['timeout'], max(1, deadline-time.monotonic())),
                               check=True)
            server_log = OUT / name / 'server.log'
            if server_log.is_file():
                text = server_log.read_text()
                finalized = [json.loads(line.split('] ', 1)[1])
                             for line in text.splitlines()
                             if line.startswith('[startup-reservation] ')
                             and '"event":"finalized"' in line]
                if len(finalized) != 1 or finalized[0]['status'] != 'PASS':
                    raise RuntimeError('startup ownership finalization failed: ' + name)
                if 'retained after failed drain' in text or 'MTP init failed' in text:
                    raise RuntimeError('runtime drain or MTP fallback: ' + name)
            if not sampler.is_alive():
                raise RuntimeError('memory sampler stopped unexpectedly')
            state['completed'].append(name)
            save('state.json', state)
        for path, expected in plan['files'].items():
            if sha(Path(path)) != expected:
                raise RuntimeError('frozen identity changed at completion: ' + path)
        state['status'] = 'MEASURED_REQUIRES_REVIEW'
    except BaseException as error:
        state.update(status='STOPPED', error=repr(error))
        raise
    finally:
        stop.set()
        sampler.join()
        state['finished'] = time.time()
        save('state.json', state)
        save('files.json', {str(p.relative_to(OUT)): sha(p)
                           for p in OUT.rglob('*') if p.is_file() and p.name != 'files.json'})


if __name__ == '__main__':
    main()
