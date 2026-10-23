"""Run a frozen suffix stage under one outer gpu_exclusive.py lease.

Custom HTTP groups use the same evalscope collector as the standard matrix.
Only lifecycle cancellation uses direct HTTP; it is not a performance sample.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
import gpu_exclusive
import run_acceptance as http_runner
from request_cancellation_contract import parse_sse, require_cancelled, response_fields


class EnvironmentAbort(RuntimeError):
    pass


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def require_guard():
    """Require our guard ancestor and the actual shared exclusive lock."""
    pid, found = os.getppid(), False
    for _ in range(64):
        if pid <= 1:
            break
        proc = Path('/proc') / str(pid)
        arguments = proc.joinpath('cmdline').read_bytes().split(b'\0')
        if any(Path(a.decode(errors='replace')).name == 'gpu_exclusive.py'
               for a in arguments if a):
            found = True
            break
        fields = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    if not found:
        raise EnvironmentAbort('must run beneath gpu_exclusive.py')
    with gpu_exclusive.common_lock().open('r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    raise EnvironmentAbort('outer shared GPU lease is not held')


def verify_files(plan):
    for name, expected in plan['files'].items():
        if sha(Path(name)) != expected:
            raise RuntimeError('frozen identity changed: ' + name)
    for name, expected in plan['model_file_metadata'].items():
        stat = Path(name).stat()
        actual = dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                      device=stat.st_dev, inode=stat.st_ino)
        if actual != expected:
            raise RuntimeError('model file metadata changed: ' + name)


def sample_memory(output, stop, failures, heartbeat):
    try:
        with output.open('x') as stream:
            while not stop.is_set():
                processes = gpu_exclusive.processes()
                owned = {os.getpid()}
                for _ in range(64):
                    previous = len(owned)
                    owned |= {pid for pid, row in processes.items()
                              if row.get('ppid') in owned}
                    if len(owned) == previous:
                        break
                row = dict(unix_seconds=time.time(), monotonic=time.monotonic(),
                           meminfo=Path('/proc/meminfo').read_text(), processes={})
                for pid in sorted(owned):
                    try:
                        row['processes'][pid] = dict(
                            status=Path(f'/proc/{pid}/status').read_text(),
                            identity=processes.get(pid))
                    except (FileNotFoundError, ProcessLookupError):
                        pass
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                heartbeat['last_sample'] = time.monotonic()
                stop.wait(1)
    except BaseException as error:
        failures.append(repr(error))
        stop.set()


def clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}


def require_monitor(sampler, failures, heartbeat, maximum_age):
    age = time.monotonic() - heartbeat['last_sample']
    if failures or not sampler.is_alive() or age > maximum_age:
        raise EnvironmentAbort('resource monitor failed/stale: ' +
                               repr(dict(failures=failures, age_seconds=age)))


def stop_group(child):
    # Each step owns a new session; never signal a global q4t name or conflict PID.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    remaining = [pid for pid, row in gpu_exclusive.processes().items()
                 if row.get('pgid') == child.pid and row.get('state') != 'Z']
    if remaining:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def audit_server(output, expected_requests):
    log = output / 'server.log'
    if not log.is_file():
        raise RuntimeError('missing server evidence: ' + str(output))
    text = log.read_text()
    final = [json.loads(line.split('] ', 1)[1]) for line in text.splitlines()
             if line.startswith('[startup-reservation] ')
             and '"event":"finalized"' in line]
    if (len(final) != 1 or final[0]['status'] != 'PASS' or
            final[0].get('release_unknown', 0) != 0):
        raise RuntimeError('startup ownership finalization failed: ' + str(output))
    if any(value in text for value in ['MTP init failed', 'retained after failed drain']):
        raise RuntimeError('MTP fallback or drain failure: ' + str(output))
    requests = len(re.findall(r'event=admitted ', text))
    if requests != expected_requests:
        raise RuntimeError(f'admitted {requests}, expected {expected_requests}: {output}')
    result = json.loads((output / 'exit.json').read_text())
    if result['server'] != 0 or not result['http_output_checks_passed']:
        raise RuntimeError('HTTP/clean server exit failed: ' + str(output))
    argv = json.loads((output / 'server-command.json').read_text())['argv']
    suffix_lines = [line for line in text.splitlines() if line.startswith('[suffix] ')]
    suffix_rows = []
    for line in suffix_lines:
        if ' disabled=' in line:
            raise RuntimeError('suffix silently disabled: ' + line)
        row = dict(re.findall(r'(\w+)=(\S+)', line))
        if int(row.get('invalid', '-1')) != 0 or int(row.get('exhausted', '-1')) != 0:
            raise RuntimeError('suffix invalid/capacity fallback: ' + line)
        suffix_rows.append(row)
    if '--mtp-suffix' in argv:
        if len(suffix_rows) != expected_requests:
            raise RuntimeError('missing per-request suffix instrumentation: ' + str(output))
    elif suffix_rows:
        raise RuntimeError('suffix ran in a suffix-off arm: ' + str(output))
    save(output / 'server-audit.json', dict(
        admitted_requests=requests, finalization=final[0], suffix=suffix_rows,
        mtp_records=[line for line in text.splitlines()
                     if line.startswith(('[mtp-', '[suffix]', '[suffix-', '[mtp-suffix'))]))


def run_stage(plan, plan_path):
    require_guard()
    output = Path(plan['output'])
    if not output.is_relative_to(ROOT / '.q4t-work'):
        raise ValueError('output must be within this checkout .q4t-work')
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + plan['stage_timeout_seconds']
    state = dict(status='RUNNING', started=time.time(), completed=[],
                 requests_completed=0, loads_started=0, contract_seconds=0,
                 plan_sha256=sha(plan_path))
    stop, failures = threading.Event(), []
    heartbeat = {'last_sample': time.monotonic()}
    sampler = threading.Thread(target=sample_memory,
        args=(output / 'unified-memory.jsonl', stop, failures, heartbeat), daemon=True)
    sampler.start()
    active = None
    try:
        verify_files(plan)
        shutil.copy2(plan_path, output / 'plan.json')
        for step in plan['steps']:
            if sha(plan_path) != state['plan_sha256']:
                raise RuntimeError('plan changed during execution')
            if time.monotonic() >= deadline:
                raise TimeoutError('frozen stage wall-clock budget exhausted')
            require_monitor(sampler, failures, heartbeat,
                            plan['resource_sample_max_age_seconds'])
            state['current'] = step['name']
            state['loads_started'] += step['http_loads']
            save(output / 'state.json', state)
            save(output / (step['name'] + '-command.json'), step)
            step_start = time.monotonic()
            limit = min(step_start + step['timeout'], deadline)
            if step['kind'] == 'contract':
                limit = min(limit, step_start +
                            plan['direct_contract_timeout_seconds'] - state['contract_seconds'])
            with (output / (step['name'] + '-driver.log')).open('x') as log:
                active = subprocess.Popen(step['command'], cwd=ROOT,
                    env=clean_env() | step.get('env', {}), stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                while active.poll() is None:
                    if time.monotonic() >= limit:
                        raise TimeoutError('frozen step/stage budget: ' + step['name'])
                    require_monitor(sampler, failures, heartbeat,
                                    plan['resource_sample_max_age_seconds'])
                    time.sleep(.2)
                if active.returncode:
                    raise RuntimeError(f"{step['name']} failed with {active.returncode}")
                residual = [pid for pid, row in gpu_exclusive.processes().items()
                            if row.get('pgid') == active.pid and row.get('state') != 'Z']
                if residual:
                    raise RuntimeError('step left owned processes: ' + repr(residual))
            active = None
            if step['kind'] == 'contract':
                state['contract_seconds'] += time.monotonic() - step_start
            if step['http_loads']:
                audit_server(output / step['name'], step['http_requests'])
            state['requests_completed'] += step['http_requests']
            state['completed'].append(step['name'])
            save(output / 'state.json', state)
        verify_files(plan)
        if sha(plan_path) != state['plan_sha256']:
            raise RuntimeError('plan changed at completion')
        require_monitor(sampler, failures, heartbeat,
                        plan['resource_sample_max_age_seconds'])
        if time.monotonic() >= deadline:
            raise TimeoutError('frozen stage wall-clock budget exhausted')
        if state['requests_completed'] != plan['http_requests']:
            raise RuntimeError('completed request budget mismatch')
        state['status'] = 'MEASURED_REQUIRES_REVIEW'
    except BaseException as error:
        state.update(status='ENV_ABORT' if isinstance(error, EnvironmentAbort)
                     else 'STOPPED', error=repr(error))
        raise
    finally:
        cleanup_errors = []
        if active is not None:
            try:
                stop_group(active)
            except BaseException as error:
                cleanup_errors.append('owned step cleanup: ' + repr(error))
        stop.set()
        sampler.join(timeout=5)
        if sampler.is_alive():
            cleanup_errors.append('resource monitor did not stop within five seconds')
        if cleanup_errors:
            state.update(status='ENV_ABORT', cleanup_errors=cleanup_errors)
        state.update(finished=time.time(), elapsed_seconds=time.monotonic() - started,
                     monitor_failures=failures)
        save(output / 'state.json', state)
        save(output / 'files.json', {str(p.relative_to(output)): sha(p)
             for p in output.rglob('*') if p.is_file() and p.name != 'files.json'})
        if cleanup_errors:
            raise EnvironmentAbort('; '.join(cleanup_errors))


def lifecycle(plan, step, output):
    """One same-mode control, one explicit decode cancel, one fresh recovery."""
    scenario = next(s for s in plan['scenarios'] if s['id'] == 'repeat-code')
    prompt = json.loads(Path(scenario['path']).read_text().splitlines()[0])['prompt']
    port = plan['port']

    def request(path, payload=None, first=None, label=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
        if label:
            save(output / (label + '-request.json'), payload)
        try:
            connection.request('GET' if payload is None else 'POST', path,
                None if payload is None else json.dumps(payload),
                {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = bytearray()
            while True:
                line = response.readline()
                if not line:
                    break
                raw.extend(line)
                if first and line.startswith(b'data: {'):
                    value = json.loads(line[6:])
                    if any(c.get('delta', {}).get('content')
                           for c in value.get('choices', [])):
                        first.set()
            result = dict(status=response.status, body=raw.decode())
            if label:
                save(output / (label + '-response.json'), result)
            return result
        finally:
            connection.close()

    def payload(label):
        return dict(model='qwen3.8-flash-next', prompt=prompt,
                    temperature=0, max_tokens=256, stream=True,
                    stream_options={'include_usage': True}, request_id=label,
                    cancel_token=hashlib.sha256(label.encode()).hexdigest())

    def fields(response, label):
        return response_fields(parse_sse(response, label))

    def complete(response, label):
        value = fields(response, label)
        usage = dict(prompt_tokens=scenario['input_tokens'], completion_tokens=256,
                     total_tokens=scenario['input_tokens'] + 256)
        if (value['errors'] or not value['text'] or value['finish'] != ['length']
                or value['usage'] != [usage]):
            raise RuntimeError('lifecycle normal response failed: ' + label)
        return value

    def idle():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = request('/healthz')
            if result['status'] != 200:
                raise RuntimeError('health failed after lifecycle request')
            value = json.loads(result['body'])
            if value['seq_slots_free'] == 1:
                if not value['gpu_healthy'] or value['seq_slots_total'] != 1:
                    raise RuntimeError('lifecycle GPU health or capacity failed')
                return value
            time.sleep(.05)
        raise RuntimeError('lifecycle slot failed to drain')

    label = 'suffix-' + step['head'] + '-control'
    control = complete(request('/v1/chat/completions', payload(label), label=label), label)
    idle()
    label = 'suffix-' + step['head'] + '-cancel'
    body, first = payload(label), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request, '/v1/chat/completions', body, first, label)
        if not first.wait(90):
            raise RuntimeError('no decode content before cancellation')
        cancelled = request('/v1/requests/cancel',
            {key: body[key] for key in ['request_id', 'cancel_token']})
        save(output / 'cancel-control-response.json', cancelled)
        if cancelled['status'] != 202:
            raise RuntimeError('explicit cancellation was not accepted')
        require_cancelled(future.result(timeout=120), label)
    save(output / 'after-cancel-health.json', idle())
    label = 'suffix-' + step['head'] + '-recovery'
    recovery = complete(request('/v1/chat/completions', payload(label), label=label), label)
    if recovery != control:
        raise RuntimeError('fresh same-mode recovery differs from control')
    save(output / 'after-recovery-health.json', idle())
    return dict(requests=3, control=control, recovery=recovery,
                scope='single slot; explicit decode cancellation and subsequent fresh request')


def custom_step(plan, name):
    require_guard()
    step = next(s for s in plan['steps'] if s['name'] == name)
    output = Path(plan['output']) / name
    output.mkdir(parents=True, exist_ok=False)
    env = clean_env()
    if step['kind'] == 'timing':
        env['Q4T_MTP_TIMING'] = '1'
    command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
               '--port', str(plan['port']), '--max-seq', '1', '--max-prefill',
               '8192', '--max-len', '208896', '--max-tokens', '256', '--mtp', '--mtp-k', '3']
    if step['head'] == 'subset':
        command += ['--mtp-subset-head', plan['artifact']]
    if step['suffix']:
        command += ['--mtp-suffix']
    save(output / 'server-command.json', dict(argv=command,
         environment={k: v for k, v in env.items() if k.startswith('Q4T_')},
         diagnostic_only=step['kind'] == 'timing',
         performance_acceptance_sample=step['kind'] == 'scenarios',
         measurement_role={'scenarios': 'acceptance', 'timing': 'diagnostic',
                           'lifecycle': 'contract'}[step['kind']],
         performance_acceptance=False))
    save(output / 'step.json', step)
    results, passed, failure = [], False, None
    server = None
    with (output / 'server.log').open('x') as log:
        try:
            http_runner.event(output, 'server_before_start')
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            (output / 'server.pid').write_text(str(server.pid) + '\n')
            http_runner.event(output, 'server_started', pid=server.pid)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError('custom server exited during startup')
                if 'serving on port' in (output / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise TimeoutError('custom service startup exceeded 900 seconds')
            capacity = http_runner.capacity_evidence((output / 'server.log').read_text(), 208896)
            save(output / 'capacity.json', capacity)
            http_runner.event(output, 'server_ready', pid=server.pid, capacity=capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('custom service effective capacity mismatch')
            if step['kind'] == 'lifecycle':
                results = lifecycle(plan, step, output)
            else:
                for scenario in plan['scenarios']:
                    case = output / scenario['id']
                    case.mkdir()
                    count = 1 if step['kind'] == 'timing' else 3
                    source = Path(scenario['path'])
                    inputs = case / 'requests.jsonl'
                    inputs.write_text('\n'.join(source.read_text().splitlines()[:count]) + '\n')
                    parsed = http_runner.run_case(output, case, inputs, count,
                        scenario['input_tokens'], scenario['input_tokens'], 256,
                        True, ROOT / 'tools/evalscope/.venv/bin/evalscope',
                        Path(plan['model']), plan['port'], env, server, False,
                        capacity, 'performance')
                    rows = dict(id=scenario['id'], responses=parsed,
                        prompt_sha256=scenario['prompt_sha256'],
                        outputs=[hashlib.sha256(r['text'].encode()).hexdigest() for r in parsed],
                        metrics=[dict(ttft=r['ttft'], latency=r['latency'],
                            decode_tps=(r['actual_output'] - 1) / (r['latency'] - r['ttft']),
                            total_tps=r['actual_output'] / r['latency']) for r in parsed])
                    if any(r['prompt_sha256'] != scenario['prompt_sha256'] for r in parsed):
                        raise RuntimeError('scenario prompt identity changed')
                    results.append(rows)
                    save(output / 'results.json', results)
                    http_runner.event(output, 'case_completed', case=scenario['id'], requests=count)
            save(output / 'results.json', results)
            passed = True
        except BaseException as error:
            failure = repr(error)
            raise
        finally:
            if server is not None:
                http_runner.event(output, 'server_before_shutdown', pid=server.pid)
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                http_runner.event(output, 'server_after_shutdown', pid=server.pid,
                                  server_returncode=server.returncode)
            clean = passed and server is not None and server.returncode == 0
            save(output / 'exit.json', dict(server=server.returncode if server else None,
                http_output_checks_passed=clean, failure=failure,
                diagnostic_only=step['kind'] == 'timing',
                performance_acceptance_sample=step['kind'] == 'scenarios',
                performance_acceptance=False))
    if not clean:
        raise RuntimeError('custom HTTP group did not complete cleanly')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.custom_step:
        custom_step(plan, args.custom_step)
    else:
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt('stage interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        run_stage(plan, args.plan.resolve())


if __name__ == '__main__':
    main()
