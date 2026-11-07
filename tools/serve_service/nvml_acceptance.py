"""One managed model, four chats; synthetic inner outage, strict outer lease."""
import copy
from dataclasses import asdict
import fcntl
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-nvml-degradation-20261107'
PLAN, OUT = BASE/'plan.json', BASE/'execution-01'
PORT = 18147
LIMITS = dict(starts=1, chats=4, explicit_controls=1, seconds=300)
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
DEFAULT = 'ea1b474d7d69808696e697346a60ee09bb9ecaca9bfb99925b78dcdbda7a4550'
SIDECAR = '3eb463850cf07338406b811197a68a5dcb9474ac9c2cf039470aa4f654924724'

# The validation controller imports the immutable package code. Only this
# harness injects a query result and supplies the already-held outer lease.
if len(sys.argv) > 1 and sys.argv[1] == 'controller':
    selected = json.loads(PLAN.read_text())['package']['package']
    source = Path(selected)/'source'
    sys.path[:0] = [str(source), str(source/'tools/evalscope')]
else:
    sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import package as pkg, service, supervisor


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit before freezing')
    pkg.require(pkg.sha(ROOT/'build/q4t') == DEFAULT and
                pkg.sha(ROOT/'build/q4t.release.json') == SIDECAR, 'default pair changed')
    receipt = pkg.prepare(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
                          MODEL, BASE/'package', port=PORT, nvml_policy='degrade')
    paths = {*pkg.V7_SOURCES, 'tools/serve_service/nvml_acceptance.py',
        'tools/serve_service/test_telemetry.py', 'tools/examples/chat_client.py',
        'tools/examples/deadline_http.py', 'tools/examples/project_records.py',
        'tools/serve_backpressure/verify.py', 'docs/STATUS.md',
        'docs/SERVE_NVML_DEGRADATION_2026-11-07.md'}
    cpu = {str(p.relative_to(BASE)): pkg.sha(p) for p in (BASE/'cpu').glob('*.log')}
    pkg.require(cpu and all('\nOK\n' in (BASE/p).read_text() for p in cpu), 'CPU gate')
    pkg.save(PLAN, dict(version=1, limits=LIMITS, package=receipt, cpu=cpu,
        default_binary_sha256=DEFAULT, default_sidecar_sha256=SIDECAR,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                            cwd=ROOT, text=True).strip(),
        source_files={p: pkg.sha(ROOT/p) for p in sorted(paths)},
        fault='synthetic inner TIMEOUT until explicit recovery marker',
        outer_monitor='strict, real NVML, original shared lock',
        supervisor_control_upper_bound=244, performance_acceptance=False))
    return receipt


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS, 'phase limits changed')
    for path, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/path) == digest, 'source changed: '+path)
    for path, digest in plan['cpu'].items():
        pkg.require(pkg.sha(BASE/path) == digest, 'CPU receipt changed')
    pkg.require(pkg.sha(ROOT/'build/q4t') == DEFAULT and
                pkg.sha(ROOT/'build/q4t.release.json') == SIDECAR, 'default changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    return plan


def controller():
    plan = check()
    guard = service.guard
    fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    with guard.borrowed_lock(fd, guard.common_lock()):
        pass
    supervisor.arm_parent_death(supervisor.process_identity(os.getppid()))
    real_query, real_guard = guard.nvml_process_query.query, guard.run_guard
    def query(*args, **kwargs):
        if (OUT/'fault.request').exists() and not (OUT/'recover.request').exists():
            return dict(status='TIMEOUT', pids=None, seconds=0,
                worker_pid=None, returncode=None, synthetic=True,
                mechanism='validation harness; no worker or driver failure induced')
        return real_query(*args, **kwargs)
    def run(command, output, lock, **kwargs):
        return real_guard(command, output, lock, lease_fd=fd, **kwargs)
    guard.nvml_process_query.query, guard.run_guard = query, run
    pkg.save(OUT/'controller-source.json', dict(
        service=str(Path(service.__file__).resolve()),
        package=str(Path(pkg.__file__).resolve()), lease_inode=os.fstat(fd).st_ino,
        owner=supervisor.process_identity(os.getpid()), synthetic_fault=True))
    return service.run(plan['package']['package'], plan['package']['manifest_sha256'],
                       OUT/'managed-run')


def wait_for(predicate, process, deadline):
    while time.monotonic() < deadline:
        pkg.require(process.poll() is None, 'controller exited before scenario completed')
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('scenario deadline')


def require_port_available(port):
    # Match the server's SO_REUSEADDR policy: TIME_WAIT is not a listener.
    # listen() also rejects a competing listener rather than merely checking
    # that a reusable local address can be bound. This owns no external socket.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(1)


def run():
    plan = check()
    guard = service.guard
    fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    argv = Path(f'/proc/{os.getppid()}/cmdline').read_bytes().split(b'\0')
    pkg.require(str(ROOT/'tools/evalscope/gpu_exclusive.py').encode() in argv,
                'strict outer guard required')
    with guard.borrowed_lock(fd, guard.common_lock()):
        pass
    OUT.mkdir(exist_ok=False)
    started = time.monotonic()
    pkg.save(OUT/'begin.json', dict(started=started, limits=LIMITS,
        lease_inode=os.fstat(fd).st_ino, plan_sha256=pkg.sha(PLAN)))
    process = None
    failure, cleanup = None, []
    responses, states = [], []
    count = 0
    run_dir = OUT/'managed-run'

    def expired(*_):
        raise TimeoutError('220-second business budget exhausted; cleanup only')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 220)

    def state():
        if not (run_dir/'status.json').exists():
            return None
        value = service.status(run_dir)
        pkg.require(value['state'] not in ('FAILED', 'STOPPED'), 'service stopped unexpectedly')
        pkg.require(value['worker']['attempts'] <= 1, 'model start budget')
        return value

    def monitor(visibility):
        value = state()
        if value and value.get('gpu_monitor', {}).get('visibility') == visibility:
            return value
        return None

    def complete(label, body):
        nonlocal count
        from tools.examples import chat_client as client
        pkg.require(count < LIMITS['chats'], 'chat budget')
        count += 1
        pkg.save(OUT/'counts.json', dict(chats=count))
        pkg.save(OUT/(label+'-request.json'), body)
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=60)
        before = time.monotonic()
        try:
            connection.request('POST', '/v1/chat/completions',
                json.dumps(body).encode(), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            with (OUT/(label+'-wire.bin')).open('xb') as raw:
                pkg.require(response.status == 200, 'HTTP '+str(response.status))
                value = (client.read_sse if body['stream'] else client.read_json)(
                    response, sink=raw.write)
            pkg.require(value.id == body['request_id'] and value.model == body['model'],
                        'response identity differs')
            row = dict(label=label, seconds=time.monotonic()-before, **asdict(value))
            pkg.save(OUT/(label+'-completion.json'), row)
            responses.append(row)
            states.append(state())
            return value
        finally:
            connection.close()

    try:
        with (OUT/'controller.log').open('xb') as log:
            process = subprocess.Popen(['/usr/bin/python3', '-B', str(Path(__file__).resolve()),
                'controller'], cwd=ROOT, pass_fds=(fd,), start_new_session=True,
                stdout=log, stderr=subprocess.STDOUT)
        pkg.save(OUT/'controller-identity.json', supervisor.process_identity(process.pid))
        ready = wait_for(lambda: (v if (v := state()) and v['state'] == 'READY' else None),
                         process, started+150)
        owner = ready['worker']['backend']
        pkg.save(OUT/'ready.json', ready)
        (OUT/'fault.request').touch(exist_ok=False)
        gap = wait_for(lambda: monitor('UNKNOWN'), process, time.monotonic()+10)
        pkg.save(OUT/'gap.json', gap)
        with guard.common_lock().open('r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise RuntimeError('lease was released during outage')
        from tools.examples import chat_client as client
        sys.path.insert(0, str(ROOT/'tools/examples'))
        from tools.examples.project_records import ProjectRecords, TOOLS, VALIDATORS
        for stream in (False, True):
            body = client.make_request([dict(role='user',
                content='Compute 17 + 25. Reply with the number only.')], [],
                stream=stream, max_tokens=128, timeout_ms=60000)
            value = complete('sse' if stream else 'json', body)
            pkg.require(value.finish_reason == 'stop' and value.message['content'].strip() == '42',
                        'JSON/SSE answer')
        tools = copy.deepcopy(TOOLS[:1])
        tools[0]['function']['strict'] = True
        body = client.make_request([dict(role='user', content=
            'Read the first 16 lines of docs/STATUS.md using read_project_record. '
            'Then reply only with the exact citation returned by the tool.')], tools,
            stream=True, max_tokens=256, timeout_ms=60000)
        body.update(tool_choice=dict(type='function', function=dict(name='read_project_record')),
                    parallel_tool_calls=False)
        value = complete('tool-call', body)
        calls = client.prepare_tool_calls(value, VALIDATORS)
        pkg.require(len(calls) == 1 and calls[0].arguments ==
                    dict(path='docs/STATUS.md', start_line=1, line_count=16), 'tool range')
        result = ProjectRecords(ROOT).execute(calls[0])
        pkg.require(result['ok'] and result['source']['sha256'] ==
                    plan['source_files']['docs/STATUS.md'], 'real project source')
        pkg.save(OUT/'tool-result.json', result)
        pkg.require(all(v['gpu_monitor']['visibility'] == 'UNKNOWN' for v in states),
                    'expected three requests during monitor outage')
        (OUT/'recover.request').touch(exist_ok=False)
        recovered = wait_for(lambda: monitor('AVAILABLE'), process, time.monotonic()+65)
        pkg.save(OUT/'recovered.json', recovered)
        history = client.append_tool_results(body['messages'], value,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow = client.make_request(history, tools, stream=False, max_tokens=256,
                                     timeout_ms=60000)
        follow['tool_choice'] = 'none'
        value = complete('tool-final', follow)
        pkg.require(value.finish_reason == 'stop' and result['source']['citation'] in
                    value.message['content'], 'final tool citation')
        pkg.require(all(v['worker']['backend'] == owner and v['worker']['attempts'] == 1 and
                    v['worker']['restarts'] == 0 for v in [*states, recovered]),
                    'backend was replaced during outage')
        from tools.serve_backpressure.verify import metrics
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=5)
        try:
            connection.request('GET', '/metrics')
            response = connection.getresponse()
            raw = response.read(1024*1024)
            (OUT/'metrics.txt').write_bytes(raw)
            pkg.require(response.status == 200, 'metrics status')
            values = metrics(raw.decode())
        finally:
            connection.close()
        pkg.require(values['q4t_requests_total'] == values['q4t_requests_success_total'] == 4 and
                    values['q4t_gpu_healthy'] == 1 and values['q4t_active_chats'] == 0,
                    'requests/health/idle counters')
    except BaseException:
        failure = traceback.format_exc()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if process is not None:
            try:
                if (run_dir/'controller.json').exists():
                    pkg.save(OUT/'stop-result.json', service.stop(run_dir))
                process.wait(45)
                pkg.require(process.returncode == 0, 'controller nonzero exit')
                terminal = service.status(run_dir)
                pkg.save(OUT/'terminal.json', terminal)
                pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
                    terminal['guard']['status'] == 'COMPLETED_WITH_MONITOR_GAPS', 'terminal contract')
            except BaseException:
                cleanup.append(traceback.format_exc())
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)
        try:
            final = guard.nvml_process_query.query(timeout=5)
            pkg.save(OUT/'gpu-final.json', final)
            pkg.require(final['status'] == 'OK' and not final['pids'], 'final GPU unknown or occupied')
            require_port_available(PORT)
            check()
        except BaseException:
            cleanup.append(traceback.format_exc())
        pkg.save(OUT/'result.json', dict(status='PASS_MANAGED_NVML_DEGRADATION'
            if failure is None and not cleanup else 'FAILED', failure=failure, cleanup=cleanup,
            chats=count, seconds=time.monotonic()-started, states=states,
            responses=responses, performance_acceptance=False, synthetic_fault=True))
    return 0 if failure is None and not cleanup else 1


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1:] == ['prepare']:
        print(json.dumps(prepare()))
    elif sys.argv[1:] == ['controller']:
        raise SystemExit(controller())
    elif sys.argv[1:] == ['run']:
        raise SystemExit(run())
    else:
        raise SystemExit('usage: nvml_acceptance.py prepare|run|controller')
