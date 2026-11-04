"""One authorized idle-service window; same flock across stop/checks/restore."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/home/rm01/models/dev/qwen4-thor')
OUT = ROOT / '.q4t-work/chat-business-acceptance-20261102'
ATTEMPT = OUT / 'attempt-01'
PLAN = ROOT / '.q4t-work/chat-business-plan-20261102.json'
CID = '9fb0504fd908c42e0db4fb8e90cce7a037ecfe2c7bb2adbc256318144a715164'
STARTED = '2026-11-01T22:31:29.647037149Z'
sys.path[:0] = [str(ROOT / 'tools/response_protocol'), str(ROOT / 'tools/evalscope')]
import acceptance
import gpu_exclusive
import nvml_process_query

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
state = dict(status='PREFLIGHT', performance_acceptance=False, deployed=False)

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def emit(event, **kw):
    row = dict(utc=stamp(), event=event, **kw)
    with (OUT / 'lifecycle-events.jsonl').open('a') as stream:
        stream.write(json.dumps(row) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(row), flush=True)

def save(name, value):
    acceptance.save(OUT / name, value)

def require(value, message):
    if not value:
        raise RuntimeError(message)

def docker(*args, timeout=20):
    run = subprocess.run(['sudo', '-n', 'docker', *args], capture_output=True,
                         text=True, timeout=timeout)
    emit('docker', argv=list(args), returncode=run.returncode,
         stdout=run.stdout, stderr=run.stderr)
    require(run.returncode == 0, 'docker operation failed')
    return run.stdout

# Do not print/store container environment or unrelated configuration.
def inspect_container():
    raw = docker('inspect', '--format',
        '{{json .Id}}\n{{json .Name}}\n{{json .Image}}\n{{json .State}}', CID)
    return dict(zip(('Id', 'Name', 'Image', 'State'), map(json.loads, raw.splitlines())))

def idle_metrics():
    with opener.open('http://127.0.0.1:8000/metrics', timeout=5) as response:
        require(response.status == 200, 'metrics HTTP status')
        raw = response.read().decode()
    (OUT / 'preflight/vllm-metrics-under-lock.prom').write_text(raw)
    names = ('vllm:num_requests_running', 'vllm:num_requests_waiting',
             'vllm:request_success_total', 'vllm:prompt_tokens_total',
             'vllm:generation_tokens_total')
    values = {}
    for line in raw.splitlines():
        if line.startswith('#') or not line:
            continue
        name = line.split('{', 1)[0].split()[0]
        if name in names:
            values.setdefault(name, []).append(float(line.split()[-1]))
    require(set(values) == set(names), 'incomplete occupancy metrics')
    require(all(v == 0 for rows in values.values() for v in rows),
            'vLLM has business activity; do not stop')
    save('preflight/locked-idle.json', dict(utc=stamp(), values=values))
    emit('idle_confirmed', values=values)

def mark_lock(lock, phase):
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps(dict(utc=stamp(), pid=os.getpid(), state=phase,
                              output=str(OUT))) + '\n')
    lock.flush()
    os.fsync(lock.fileno())

def main():
    acceptance.install_interrupt_handlers()
    plan = json.loads(PLAN.read_text())
    acceptance.validate_plan(plan)
    save('lifecycle-identity.json', dict(script_sha256=acceptance.digest(__file__),
        plan_sha256=acceptance.digest(PLAN), container_id=CID, started_at=STARTED,
        limits=plan['limits'], restore_timeout_seconds=600))
    lock_path = gpu_exclusive.common_lock()
    recovery = lock_path.with_name(lock_path.name + '.recovery-required')
    with lock_path.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not os.path.lexists(recovery), 'unfinished recovery')
        mark_lock(lock, 'business_window_preflight')
        guard = None
        stop_attempted = False
        cleanup_ok = True
        try:
            before = inspect_container()
            save('container-before.json', before)
            require(before['Id'] == CID and before['State']['Running'] and
                    before['State']['StartedAt'] == STARTED, 'container identity changed')
            idle_metrics()
            stop_attempted = True
            docker('stop', '--time', '45', CID, timeout=60)
            stopped = inspect_container()
            save('container-stopped.json', stopped)
            require(not stopped['State']['Running'], 'container still running')
            emit('idle_service_stopped')
            ATTEMPT.mkdir(mode=0o700)
            acceptance.save(ATTEMPT / 'plan.json', plan)
            command = ['/usr/bin/python3', '-B', str(ROOT / 'tools/evalscope/gpu_exclusive.py'),
                '--output', str(ATTEMPT / 'lease'), '--lease-fd', str(lock.fileno()),
                '--', '/usr/bin/python3', '-B', str(ROOT / 'tools/response_protocol/acceptance.py'),
                'worker', '--plan', str(ATTEMPT / 'plan.json'), '--output', str(ATTEMPT)]
            acceptance.save(ATTEMPT / 'guard-command.json', dict(argv=command))
            env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_')
                   and k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME')}
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            with (ATTEMPT / 'guard.log').open('x') as log:
                guard = subprocess.Popen(command, cwd=ROOT, stdout=log,
                    stderr=subprocess.STDOUT, pass_fds=(lock.fileno(),),
                    start_new_session=True, env=env)
                emit('acceptance_started', guard_pid=guard.pid)
                guard.wait(timeout=plan['limits']['stage_seconds'] + 120)
            exit_record = json.loads((ATTEMPT / 'lease/exit.json').read_text())
            worker_path = ATTEMPT / 'worker-result.json'
            worker = json.loads(worker_path.read_text()) if worker_path.exists() else {}
            state['status'] = acceptance.combined_status(worker, exit_record, guard.returncode)
            if state['status'] == 'PASS':
                state['status'] = 'FAIL'
                acceptance.validate_plan(plan)
                state['status'] = 'PASS'
            emit('acceptance_finished', status=state['status'])
        except BaseException as error:
            state.update(status='ENV_ABORT', error=repr(error))
            emit('lifecycle_error', error=repr(error))
        finally:
            # Once cleanup starts, repeat signals cannot skip service recovery.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                if guard is not None:
                    if guard.poll() is None:
                        guard.terminate()
                        guard.wait(timeout=90)
                    record = json.loads((ATTEMPT / 'lease/exit.json').read_text())
                    state['guard_exit'] = record
                    require(record.get('finished') and not record.get('cleanup_error'),
                            'guard cleanup not established')
                if stop_attempted:
                    snapshot = gpu_exclusive.inspect(compute_query=lambda:
                        gpu_exclusive.nvml_pids(OUT / 'restore-nvml.jsonl'))
                    save('before-restore-processes.json', snapshot)
                    require(not snapshot['conflicts'], 'GPU conflict before restore')
                    mark_lock(lock, 'business_window_restoring')
                    now = inspect_container()
                    require(now['Id'] == CID, 'restore container identity changed')
                    if not now['State']['Running']:
                        docker('start', CID, timeout=45)
                    emit('original_service_restoring')
                    deadline = time.monotonic() + 600
                    last_error = None
                    while time.monotonic() < deadline:
                        try:
                            with opener.open('http://127.0.0.1:8000/health', timeout=3) as response:
                                require(response.status == 200, 'health not ready')
                                response.read()
                            break
                        except Exception as error:
                            last_error = repr(error)
                            time.sleep(3)
                    else:
                        raise RuntimeError('restore health timeout: ' + str(last_error))
                    after = inspect_container()
                    require(after['Id'] == CID and after['State']['Running'], 'restored state')
                    save('container-restored.json', after)
                    state['original_service_restored'] = True
                    emit('original_service_restored', container_id=CID)
            except BaseException as error:
                cleanup_ok = False
                state.update(status='ENV_ABORT', recovery_error=repr(error))
                if not os.path.lexists(recovery):
                    acceptance.save(recovery, dict(utc=stamp(), output=str(OUT),
                        container_id=CID, error=repr(error)))
                emit('recovery_required', error=repr(error))
            state['finished'] = stamp()
            save('result.json', state)
            mark_lock(lock, 'released' if cleanup_ok else 'recovery_required')
            emit('window_finished', status=state['status'])
    return 0 if state['status'] == 'PASS' else 1

if __name__ == '__main__':
    raise SystemExit(main())
