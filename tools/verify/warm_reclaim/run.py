"""Bounded warm-start diagnosis; one lease includes tracing and owned cleanup."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'tools/evalscope'),
               str(ROOT / 'tools/verify/vmm_compare')]
from gpu_exclusive import common_lock
from isolated_scope import scope_properties, stop_owned_scope
from run_budget_experiment import run_owned_runner
from cache_start import prepare, check_vector
from file_cache import model_files, observe_files

BASE = ROOT / '.q4t-work/warm-reclaim-20261020'
PRIOR = ROOT / '.q4t-work/vmm-compare-20261020'
OLD = ROOT / '.q4t-work/offload-foundation-20261020/scope-integration-v5/run'
BINARY = PRIOR / 'runtime/q4t'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
DEFAULT_SHA = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
OBSERVER_SHA = '2a66ee2f174b4375961ab2d37a9be2dfc38f233d26cb55c7d2521afe38b5a8f7'


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def stamp():
    return dict(monotonic=time.monotonic(), wall=time.time())


def worker(out):
    run = out / 'run'
    run.mkdir(exist_ok=False)
    state = dict(status='RUNNING', **stamp())
    save(run / 'state.json', state)
    shutil.copytree(OLD / 'fixtures', run / 'fixtures')
    for name in ('hot.json', 'reference.json'):
        shutil.copy2(OLD / name, run / name)
    command = [a.replace(str(OLD), str(run)) for a in
               json.loads((OLD / 'normal-command.json').read_text())
               if a != '--clear-model-cache']
    command[command.index('--binary') + 1] = str(BINARY)
    command[command.index('--output') + 1] = str(run / 'normal')
    # The unchanged runner keeps its original timeout; the supervisor enforces
    # this diagnosis's tighter total wall budget and owns cleanup.
    save(run / 'command.json', command)
    def mark(name):
        with (run / 'milestones.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(event=name, **stamp())) + '\n')
    try:
        mark('prepare_begin')
        gate = prepare(MODEL, run, True, repair=False, limit_gib=64)
        save(run / 'cache-gate.json', gate)
        mark('prepare_end')
        if out.name == 'precompact-v1':
            mark('node_compaction_begin')
            subprocess.run(['sudo', '-n', sys.executable, '-B',
                str(HERE / 'compact_once.py'), '--output', str(run / 'compaction')],
                check=True)
            mark('node_compaction_end')
        final = observe_files(model_files(MODEL))
        save(run / 'cache-launch.json', final)
        check_vector(final, json.loads((run / 'cache-expected.json').read_text()))
        mark('runner_begin')
        with (run / 'normal.log').open('x') as log:
            rc = run_owned_runner(command, cwd=ROOT,
                env={k: v for k, v in os.environ.items()
                     if not k.startswith('Q4T_') and k != 'LD_PRELOAD'},
                log=log, timeout=300, evidence=run / 'normal-process.json')
        mark('runner_end')
        state.update(status='COMPLETED' if rc == 0 else 'COMMAND_FAILED',
                     returncode=rc)
        return rc
    except BaseException as error:
        state.update(status='FAILED_OR_INTERRUPTED', error=repr(error))
        raise
    finally:
        state['finished'] = stamp()
        save(run / 'state.json', state)


def wait_ready(proc, folder):
    deadline = time.monotonic() + 15
    while not (folder / 'ready.json').exists():
        if proc.poll() is not None:
            raise RuntimeError(f'collector exited before ready: {folder}')
        if time.monotonic() >= deadline:
            raise TimeoutError(f'collector ready deadline: {folder}')
        time.sleep(.05)


def cleanup_scope(out):
    spec = out / 'run/normal/http/isolation/spec.json'
    if not spec.exists():
        return dict(scope_created=False)
    owned = json.loads(spec.read_text())
    launch = json.loads(spec.with_name('launch.json').read_text())
    unit = launch['unit']
    result = dict(unit=unit, before=scope_properties(unit))
    if result['before'].get('LoadState') != 'not-found':
        result['cleanup'] = stop_owned_scope(unit, owned['token'])
    result['after'] = scope_properties(unit)
    save(out / 'supervisor-cleanup.json', result)
    if result['after'].get('LoadState') != 'not-found':
        raise RuntimeError('owned scope remains')
    return result


def supervise(stage):
    out = BASE / stage
    out.mkdir(parents=True, exist_ok=False)
    if sha(ROOT / 'build/q4t') != DEFAULT_SHA or sha(BINARY) != OBSERVER_SHA:
        raise RuntimeError('default or diagnostic binary identity changed')
    shutil.copy2(PRIOR / 'build.json', out / 'build.json')
    sources = list(HERE.glob('*.py')) + list((ROOT / 'tools/evalscope').glob('*.py'))
    sources += list((ROOT / 'tools/verify/vmm_compare').glob('*.py'))
    sources.append(ROOT / 'tools/verify/vmm_compare/memory_trace.cpp')
    source_hashes = {}
    for source in sources:
        target = out / 'source' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        source_hashes[str(source.relative_to(ROOT))] = sha(source)
    save(out / 'sources.json', source_hashes)
    state = dict(status='RUNNING', started=stamp(), model_load_budget=1,
                 http_budget=1, guarded_work_seconds=240,
                 default_sha=DEFAULT_SHA, observer_sha=OBSERVER_SHA)
    save(out / 'state.json', state)
    guard = None
    collectors = []
    logs = []
    lock_path = common_lock()
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state['lease_acquired'] = stamp()
        recovery = lock_path.with_name(lock_path.name + '.recovery-required')
        if recovery.exists():
            raise RuntimeError('unfinished recovery: ' + str(recovery))
        recovery_errors = []
        try:
            subprocess.run([sys.executable, '-B', str(HERE / 'driver_identity.py'),
                            '--output', str(out / 'driver-identity')], check=True)
            configs = [
                ('kernel', 'kernel_capture.py', ['--max-bytes', '536870912']),
                ('resources', 'resources.py', ['--binary', str(BINARY),
                                              '--model-dir', str(MODEL)])]
            for name, script, extra in configs:
                folder = out / name
                stop = out / ('stop-' + name)
                log = (out / (name + '-driver.log')).open('x')
                logs.append(log)
                proc = subprocess.Popen(['sudo', '-n', sys.executable, '-B',
                    str(HERE / script), '--output', str(folder),
                    '--stop-file', str(stop), '--max-seconds', '360', *extra],
                    stdout=log, stderr=subprocess.STDOUT)
                collectors.append((name, proc, folder, stop))
                wait_ready(proc, folder)
            command = [sys.executable, '-B', str(ROOT / 'tools/evalscope/gpu_exclusive.py'),
                '--output', str(out / 'lease'), '--lease-fd', str(lock.fileno()),
                '--', sys.executable, '-B', str(Path(__file__).resolve()),
                '--stage', stage, '--worker']
            save(out / 'guard-command.json', command)
            log = (out / 'guard.log').open('x')
            logs.append(log)
            guard = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     pass_fds=(lock.fileno(),), start_new_session=True)
            deadline = time.monotonic() + 240
            while guard.poll() is None:
                if any(proc.poll() is not None for _, proc, _, _ in collectors):
                    raise RuntimeError('collector exited during guarded stage')
                if time.monotonic() >= deadline:
                    raise TimeoutError('bounded diagnosis wall limit')
                time.sleep(.1)
            state['guard_returncode'] = guard.returncode
            state['guard'] = json.loads((out / 'lease/exit.json').read_text())
            if 'cleanup_error' in state['guard']:
                raise RuntimeError('guard cleanup error')
            state['scope'] = cleanup_scope(out)
            # Observe physical return after process teardown, without probing GPU.
            time.sleep(2)
            state['status'] = 'DIAGNOSIS_CAPTURED'
        except BaseException as error:
            state.update(status='FAILED', error=repr(error))
        finally:
            try:
                if guard is not None and guard.poll() is None:
                    guard.send_signal(signal.SIGTERM)
                    try:
                        guard.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        # Killing a hung guard cannot prove all descendants
                        # drained. Block subsequent leases until recovery.
                        guard.kill()
                        guard.wait(timeout=5)
                        recovery_errors.append('guard exceeded cleanup grace')
                state['scope_final'] = cleanup_scope(out)
            except BaseException as error:
                state.update(status='FAILED', cleanup_error=repr(error))
                recovery_errors.append(repr(error))
            for name, proc, folder, stop in collectors:
                stop.touch()
                try:
                    rc = proc.wait(timeout=15)
                    result = json.loads((folder / 'exit.json').read_text())
                    state[name] = dict(returncode=rc, result=result)
                    if rc or result['status'] != 'COMPLETED':
                        state['status'] = 'FAILED'
                    if name == 'kernel' and not result.get('instance_removed'):
                        state['status'] = 'FAILED'
                except BaseException as error:
                    state.update(status='FAILED', **{name + '_cleanup_error': repr(error)})
                    recovery_errors.append(name + ': ' + repr(error))
                if proc.poll() is None or (name == 'kernel' and
                        not state.get(name, {}).get('result', {}).get('instance_removed')):
                    recovery_errors.append(name + ': cleanup unconfirmed')
            if recovery_errors:
                # Same marker checked by gpu_exclusive before any GPU work.
                # Do not silently expose an unprotected unfinished stage.
                state['status'] = 'RECOVERY_REQUIRED'
                state['recovery_errors'] = recovery_errors
                save(recovery, dict(output=str(out), errors=recovery_errors,
                                    guard_pid=guard.pid if guard else None,
                                    collectors=[dict(name=n, pid=p.pid)
                                                for n, p, _, _ in collectors]))
            for log in logs:
                log.close()
            state['default_unchanged'] = sha(ROOT / 'build/q4t') == DEFAULT_SHA
            if not state['default_unchanged']:
                state['status'] = 'FAILED'
            state['finished'] = stamp()
            state['full_hot_gate_passed'] = (out / 'run/cache-launch.json').exists() and (out / 'run/cache-gate.json').exists()
            server_log = out / 'run/normal/http/server.log'
            state['model_started'] = server_log.exists() and '[memory-trace]' in server_log.read_text()
            cache_launch = out / 'run/cache-launch.json'
            if state['full_hot_gate_passed']:
                try:
                    check_vector(json.loads(cache_launch.read_text()),
                                 json.loads((out / 'run/cache-expected.json').read_text()))
                except RuntimeError:
                    state['full_hot_gate_passed'] = False
            save(out / 'state.json', state)
            lock.seek(0)
            lock.truncate()
            lock.write(json.dumps(dict(state='recovery_required' if recovery_errors
                                       else 'released', output=str(out))) + '\n')
            lock.flush()
    save(out / 'sealed-files.json', {str(p.relative_to(out)): sha(p)
         for p in out.rglob('*') if p.is_file()})
    print(json.dumps(dict(status=state['status'], output=str(out),
                         guard_status=state.get('guard', {}).get('status'))))
    return 0 if state['status'] == 'DIAGNOSIS_CAPTURED' else 1


if __name__ == '__main__':
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('diagnosis interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['baseline-v1', 'precompact-v1'], required=True)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    raise SystemExit(worker(BASE / args.stage) if args.worker else supervise(args.stage))
