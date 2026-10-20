"""One bounded warm-cache allocation replay under the outer GPU lease."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT/'tools/evalscope'), str(ROOT/'tools/verify/inference_metadata')]
import diagnose_warm_start as observer
from file_cache import model_files, observe_files
from isolated_scope import IsolatedScope

BASE = ROOT/'.q4t-work/vmm-prototype-20261020'
OUT = BASE/'replay-v1/run'
MODEL = Path.home()/'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def save(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2)+'\n')


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    for path in (BASE/'replay', BASE/'allocations.txt', BASE/'replay-plan.json'):
        shutil.copy2(path, OUT/path.name)
    source = list((ROOT/'tools/verify/vmm').glob('*')) + list((ROOT/'tools/evalscope').glob('*.py')) + [Path(observer.__file__)]
    manifest = {}
    for path in source:
        if not path.is_file(): continue
        dest = OUT/'source'/path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    save('sources.json', manifest)
    save('plan.json', dict(prewarm_max_bytes=64<<30, prewarm_seconds=180,
        replay_seconds=180, memory_max=64<<30, device_chunk=2<<20,
        vmm_threshold=32<<20, startup_cache_eviction=False))
    state = {'status': 'RUNNING'}
    scope = monitor = None
    paths = model_files(MODEL)
    try:
        save('cache-before.json', observe_files(paths))
        total = 0
        deadline = time.monotonic()+180
        buffer = bytearray(4<<20)
        files = []
        for path in sorted(MODEL.glob('layer-*-experts-*.safetensors')):
            count = 0
            with path.open('rb', buffering=0) as stream:
                while total < 64<<30:
                    if time.monotonic()>deadline: raise TimeoutError('prewarm')
                    n = stream.readinto(memoryview(buffer)[:min(len(buffer),(64<<30)-total)])
                    if not n: break
                    total += n
                    count += n
            files.append(dict(path=str(path), bytes=count))
            if total >= 64<<30: break
        save('prewarm.json', dict(bytes=total, files=files))
        cache = observe_files(paths)
        save('cache-after-prewarm.json', cache)
        if not cache['complete_file_set_observed'] or sum(f['resident_bytes'] for f in cache['files']) < 48<<30:
            raise RuntimeError('warm cache evidence gate')
        monitor = subprocess.Popen(['sudo','-n',sys.executable,'-B',str(Path(__file__).resolve()),'--observe'])
        scope = IsolatedScope([str(BASE/'replay'),str(BASE/'allocations.txt')],
            cwd=ROOT, env={k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'},
            log_path=OUT/'replay.log', unit=f'q4t-vmm-{time.time_ns()}.scope',
            memory_max=64<<30, evidence=OUT/'isolation')
        rc = scope.wait(timeout=180)
        scope.snapshot('completed')
        state.update(status='COMPLETED' if rc==0 else 'COMMAND_FAILED', returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        if scope is not None: scope.close()
        (OUT/'stop-observer').touch()
        if monitor is not None: monitor.wait(timeout=5)
        save('cache-after.json', observe_files(paths))
        save('state.json', state)


if __name__=='__main__':
    if sys.argv[1:]==['--observe']:
        observer.BINARY=BASE/'replay'
        sys.exit(observer.observe(OUT))
    sys.exit(main())
