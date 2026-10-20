"""One warm startup with diagnostic-only CUDA allocation wrappers."""
import argparse
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
BASE = ROOT / '.q4t-work/offload-foundation-20261020'
OUT = ROOT / '.q4t-work/warm-start-20261020/diagnostic-v1/run'
BINARY = ROOT / '.q4t-work/warm-start-20261020/q4t'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def read_text(path):
    try:
        return dict(value=path.read_text(), error=None)
    except OSError as error:
        return dict(value=None, error=str(error))


def observe(folder):
    """Root privileges only for /proc kernel stacks; no writes to processes."""
    deadline = time.monotonic() + 300
    with (folder / 'stacks.jsonl').open('x') as output:
        while time.monotonic() < deadline and not (folder / 'stop-observer').exists():
            targets = []
            for proc in Path('/proc').iterdir():
                if not proc.name.isdecimal():
                    continue
                cmd = read_text(proc / 'cmdline')['value']
                if not cmd:
                    continue
                args = cmd.split('\0')
                model = args[0] == str(BINARY)
                query = any(a.endswith('/nvml_process_query.py') for a in args) and '--worker' in args
                if not (model or query):
                    continue
                tasks = []
                try:
                    entries = list((proc / 'task').iterdir())
                except OSError:
                    entries = []
                for task in entries:
                    stat = read_text(task / 'stat')
                    state = stat['value'].rsplit(')', 1)[-1].split()[0] if stat['value'] else None
                    if task.name != proc.name and state != 'D':
                        continue
                    tasks.append(dict(tid=int(task.name), stat=stat,
                        wchan=read_text(task / 'wchan'), stack=read_text(task / 'stack')))
                    if len(tasks) >= 16:
                        break
                targets.append(dict(pid=int(proc.name), kind='model' if model else 'nvml',
                    command=args, tasks=tasks, cgroup=read_text(proc / 'cgroup')))
            output.write(json.dumps(dict(wall=time.time(), monotonic=time.monotonic(),
                targets=targets, meminfo=read_text(Path('/proc/meminfo')),
                vmstat=read_text(Path('/proc/vmstat')),
                buddyinfo=read_text(Path('/proc/buddyinfo')),
                pagetypeinfo=read_text(Path('/proc/pagetypeinfo')))) + '\n')
            output.flush()
            time.sleep(.2)
    return 0


def main(reclaim_before_load=False, reclaim_prefix=False):
    from file_cache import model_files, observe_files
    from run_budget_experiment import run_owned_runner
    OUT.mkdir(parents=True, exist_ok=False)
    sources = list((ROOT / 'tools/evalscope').glob('*.py')) + [Path(__file__), Path(__file__).with_name('warm_alloc_observer.cpp')]
    manifest = {}
    for path in sources:
        target = OUT / 'source' / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(OUT / 'sources.json', manifest)
    old = BASE / 'scope-integration-v5/run'
    for name in ('fixtures',):
        shutil.copytree(old / name, OUT / name)
    for name in ('hot.json', 'reference.json'):
        shutil.copy2(old / name, OUT / name)
    command = json.loads((old / 'normal-command.json').read_text())
    command = [a.replace(str(old), str(OUT)) for a in command if a != '--clear-model-cache']
    if reclaim_before_load:
        command.append('--clear-model-cache')
    command[command.index('--binary') + 1] = str(BINARY)
    command[command.index('--output') + 1] = str(OUT / 'normal')
    model = Path(command[command.index('--model-dir') + 1])
    binary = Path(command[command.index('--binary') + 1])
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != json.loads((BINARY.parent / 'build-identity.json').read_text())['observer_sha256']:
        raise RuntimeError('binary identity changed')
    save(OUT / 'plan.json', dict(loads=1, prewarm_max_bytes=64 << 30,
        prewarm_timeout_seconds=180, run_timeout_seconds=1200,
        scope_limit_bytes=64 << 30, binary_sha256=digest, command=command,
        observer_interval_seconds=.2, observer_max_seconds=300,
        performance_acceptance=False, reclaim_before_load=reclaim_before_load,
        controlled_prewarm_not_historical_replay=True,
        reclaim_prefix_max_bytes=(16 << 30) if reclaim_prefix else 0))
    started = time.time()
    observer = subprocess.Popen(['sudo', '-n', sys.executable, '-B', str(Path(__file__).resolve()),
                                 '--observe', str(OUT)])
    state = dict(status='RUNNING', started=started)
    save(OUT / 'state.json', state)
    try:
        paths = model_files(model)
        save(OUT / 'cache-before-prewarm.json', observe_files(paths))
        total = 0
        files = []
        deadline = time.monotonic() + 180
        buffer = bytearray(4 << 20)
        # Use only expert payloads, in a fixed order, with read-only descriptors.
        for path in sorted(model.glob('layer-*-experts-*.safetensors')):
            remaining = (64 << 30) - total
            if remaining <= 0:
                break
            consumed = 0
            with path.open('rb', buffering=0) as stream:
                while consumed < path.stat().st_size and remaining > 0:
                    if time.monotonic() > deadline:
                        raise TimeoutError('bounded prewarm deadline')
                    count = stream.readinto(memoryview(buffer)[:min(len(buffer), remaining)])
                    if not count:
                        break
                    total += count
                    consumed += count
                    remaining -= count
            files.append(dict(path=str(path), bytes=consumed))
        if not files:
            raise RuntimeError('no expert payload selected')
        save(OUT / 'prewarm.json', dict(bytes=total, files=files, finished=time.time()))
        save(OUT / 'cache-after-prewarm.json', observe_files(paths))
        if reclaim_prefix:
            from run_budget_experiment import prepare_target_cache, payload_residual
            selected = []
            selected_bytes = 0
            for item in files:
                path = Path(item['path'])
                if selected_bytes + path.stat().st_size > 16 << 30:
                    break
                selected.append(path)
                selected_bytes += path.stat().st_size
            preparation = prepare_target_cache(selected)
            save(OUT / 'partial-cache-preparation.json', dict(
                selected=[str(p) for p in selected], bytes=selected_bytes,
                preparation=preparation))
            remaining_cache = observe_files(paths)
            save(OUT / 'cache-after-partial-reclaim.json', remaining_cache)
            residual = payload_residual(remaining_cache)
            if (not selected or preparation[0] or
                    payload_residual(preparation[1]) != 0 or
                    residual is None or residual < 32 << 30):
                raise RuntimeError('partial reclaim evidence gate failed')

        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        with (OUT / 'normal.log').open('x') as log:
            rc = run_owned_runner(command, cwd=ROOT, env=env, log=log, timeout=1260,
                                  evidence=OUT / 'normal-process.json')
        state.update(status='COMPLETED' if rc == 0 else 'COMMAND_FAILED', returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        (OUT / 'stop-observer').touch()
        try:
            observer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            state['observer_cleanup'] = 'timeout; outer lease must drain'
        state.update(finished=time.time(), observer_returncode=observer.poll())
        save(OUT / 'state.json', state)
        with (OUT / 'kernel.jsonl').open('w') as log:
            subprocess.run(['sudo', '-n', 'journalctl', '-k', '--since', '@' + str(int(started)),
                            '-o', 'json', '--no-pager'], stdout=log, timeout=5, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe', type=Path)
    parser.add_argument('--reclaim-prefix', action='store_true')
    args = parser.parse_args()
    if args.reclaim_prefix:
        OUT = ROOT / '.q4t-work/warm-start-20261020/diagnostic-v2/run'
    sys.exit(observe(args.observe) if args.observe else main(reclaim_prefix=args.reclaim_prefix))
