"""Three owned model lifetimes, twelve chats; strict outer GPU lease."""
import copy
from dataclasses import asdict
import errno
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
ORIGINAL = Path(subprocess.check_output(
    ['git', 'rev-parse', '--git-common-dir'], cwd=ROOT, text=True).strip()).parent
BASE = ROOT/'.q4t-work/serve-closeout-business-20261107'
PLAN, OUT = BASE/'plan.json', BASE/'execution'
BINARY = ORIGINAL/'.q4t-work/serve-closeout-20261107/runtime/q4t'
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
PORT = 18147
LIMITS = dict(starts=3, chats=12, controls=160, seconds=1200)
MODE = sys.argv[1] if len(sys.argv) > 1 else ''
LABEL = sys.argv[2] if len(sys.argv) > 2 else ''
if MODE in ('controller', 'worker'):
    selected = json.loads((BASE/(LABEL+'-package.json')).read_text())['package']
    source = Path(selected)/'source'
else:
    source = ROOT
sys.path[:0] = [str(source), str(source/'tools/evalscope'), str(ROOT/'tools/examples')]
sys.dont_write_bytecode = True
from tools.serve_service import package as pkg, service, supervisor as sup
from tools.release import release as processes

SCHEMAS = dict(config='serve-config-v1', message='serve-message-profile-v2',
    preflight='serve-preflight-v1', reasoning='serve-reasoning-v1',
    response='serve-transport-v1', stop='serve-stop-v1', thinking='serve-thinking-v1')


def identity(path):
    return dict(path=str(path), sha256=pkg.sha(path), bytes=path.stat().st_size)


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit source before freeze')
    BASE.mkdir(parents=True, exist_ok=True)
    paths = subprocess.check_output(['git', 'ls-files', 'include', 'src', 'cmake',
        'CMakeLists.txt', 'tools/serve_service', 'tools/evalscope', 'tools/release',
        'tools/serve_ops', 'tools/serve_gateway', 'tools/examples', 'docs/STATUS.md',
        'docs/*schema.json'], cwd=ROOT, text=True).splitlines()
    protected = [ORIGINAL/'build/q4t', ORIGINAL/'build/q4t.release.json', BINARY,
        ORIGINAL/'.q4t-work/vision-attention-rowmax-20261107/engine/q4t',
        ORIGINAL/'.q4t-work/vision-forward-timing-20261107/engine/q4t']
    pkg.save(PLAN, dict(limits=LIMITS, candidate=identity(BINARY),
        cache=identity(BINARY.parent/'CMakeCache.txt'), model=str(MODEL),
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        sources={p: pkg.sha(ROOT/p) for p in paths},
        protected=[identity(p) for p in protected],
        fault_scope='validation controller/worker only; sealed production code unchanged',
        host_policy=dict(high_mib=114688, max_mib=120832, tasks=2048),
        default_replacement=False, performance_acceptance=False))
    print(json.dumps(dict(plan=str(PLAN), limits=LIMITS)))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS, 'budget changed')
    for path, digest in plan['sources'].items():
        pkg.require(pkg.sha(ROOT/path) == digest, 'source drift: '+path)
    for row in [*plan['protected'], plan['cache']]:
        pkg.require(pkg.sha(row['path']) == row['sha256'], 'artifact drift: '+row['path'])
    return plan


def fault_enabled(label):
    folder = OUT/label
    return (folder/'fault.request').exists() and not (folder/'recover.request').exists()


def controller(label):
    check()
    receipt = pkg.read(BASE/(label+'-package.json'))
    fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    sup.arm_parent_death(sup.process_identity(os.getppid()))
    guard = service.guard
    original_query, original_run = guard.nvml_process_query.query, guard.run_guard
    def query(*args, **kwargs):
        if fault_enabled(label):
            raise ValueError('synthetic collector transport fault; no driver fault')
        return original_query(*args, **kwargs)
    def run(_command, output, lock, **kwargs):
        command = ['/usr/bin/python3', '-I', '-B', str(Path(__file__).resolve()),
                   'worker', label]
        return original_run(command, output, lock, lease_fd=fd, **kwargs)
    guard.nvml_process_query.query, guard.run_guard = query, run
    pkg.save(OUT/label/'controller-source.json', dict(source=str(source),
        package=receipt, synthetic_fault=True, owner=sup.process_identity(os.getpid())))
    return service.run(receipt['package'], receipt['manifest_sha256'], OUT/label/'managed')


def worker(label):
    run_dir = OUT/label/'managed'
    control = service.verify_worker(run_dir)
    from tools.serve_service import resources
    write, get = sup.RotatingLog.write, sup.transport.get
    properties, read = resources.properties, resources.read
    counts = dict(log_errors=0, metrics_attempts=0, manager_attempts=0, statistics_errors=0)
    def log_write(logger, data):
        if fault_enabled(label) and logger.path.name == 'backend.log':
            counts['log_errors'] += 1
            raise OSError(errno.ENOSPC, 'synthetic log sink full')
        return write(logger, data)
    def http_get(port, path, **options):
        if fault_enabled(label) and path == '/metrics':
            counts['metrics_attempts'] += 1
            raise sup.contracts.InvalidObservation('synthetic_metrics_unavailable')
        return get(port, path, **options)
    def manager(unit):
        if fault_enabled(label):
            counts['manager_attempts'] += 1
            raise OSError('synthetic manager unavailable')
        return properties(unit)
    def statistic(path, *args, **kwargs):
        if fault_enabled(label) and path.name in (
                'memory.current', 'memory.peak', 'memory.swap.current', 'pids.current'):
            counts['statistics_errors'] += 1
            raise PermissionError('synthetic optional statistics unavailable')
        return read(path, *args, **kwargs)
    sup.RotatingLog.write, sup.transport.get = log_write, http_get
    resources.properties, resources.read = manager, statistic
    try:
        return sup.run(control['package'], control['manifest_sha256'], run_dir)
    finally:
        pkg.save(OUT/label/'fault-counts.json', counts)


def port_free():
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', PORT))


class Stage:
    def __init__(self):
        self.plan = check()
        OUT.mkdir(exist_ok=False)
        self.started = time.monotonic()
        self.counts = dict(starts=0, chats=0, controls=0)
        self.results = {}
        self.fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        with service.guard.borrowed_lock(self.fd, service.guard.common_lock()):
            pass

    def reserve(self, key):
        pkg.require(self.counts[key] < LIMITS[key], key+' budget exhausted')
        self.counts[key] += 1
        pkg.save(OUT/'counts.json', self.counts)

    def get(self, path):
        self.reserve('controls')
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=5)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            raw = response.read(4*1024*1024)
            return response.status, json.loads(raw)
        finally:
            connection.close()

    def wait(self, predicate, process, seconds):
        until = min(time.monotonic()+seconds, self.started+1080)
        while time.monotonic() < until:
            pkg.require(process.poll() is None, 'owned process exited before completion')
            value = predicate()
            if value:
                return value
            time.sleep(.25)
        raise TimeoutError('bounded phase deadline')

    def complete(self, folder, name, body):
        from tools.examples import chat_client as client
        self.reserve('chats')
        pkg.save(folder/(name+'-request.json'), body)
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=65)
        start = time.monotonic()
        try:
            connection.request('POST', '/v1/chat/completions', json.dumps(body).encode(),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            with (folder/(name+'-wire.bin')).open('xb') as stream:
                if response.status != 200:
                    stream.write(response.read(1024*1024))
                    raise RuntimeError('HTTP '+str(response.status))
                value = (client.read_sse if body['stream'] else client.read_json)(
                    response, sink=stream.write)
            pkg.require(value.id == body['request_id'] and value.model == body['model'],
                        'response identity')
            pkg.save(folder/(name+'-completion.json'), dict(seconds=time.monotonic()-start,
                                                          **asdict(value)))
            return value
        finally:
            connection.close()

    def business(self, folder, before_final=lambda: None):
        from tools.examples import chat_client as client
        from tools.examples.project_records import ProjectRecords, TOOLS, VALIDATORS
        for stream in (False, True):
            body = client.make_request([dict(role='user',
                content='Compute 17 + 25. Reply with the number only.')], [],
                stream=stream, max_tokens=128, timeout_ms=60000)
            answer = self.complete(folder, 'sse' if stream else 'json', body)
            pkg.require(answer.finish_reason == 'stop' and answer.message['content'].strip() == '42',
                        'JSON/SSE quality')
        tools = copy.deepcopy(TOOLS[:1])
        tools[0]['function']['strict'] = True
        body = client.make_request([dict(role='user', content=
            'Read the first 16 lines of docs/STATUS.md using read_project_record. '
            'Then reply only with the exact citation returned by the tool.')], tools,
            stream=True, max_tokens=256, timeout_ms=60000)
        body.update(tool_choice=dict(type='function', function=dict(name='read_project_record')),
                    parallel_tool_calls=False)
        answer = self.complete(folder, 'tool-call', body)
        calls = client.prepare_tool_calls(answer, VALIDATORS)
        pkg.require(len(calls) == 1 and calls[0].arguments ==
            dict(path='docs/STATUS.md', start_line=1, line_count=16), 'tool arguments')
        result = ProjectRecords(ROOT).execute(calls[0])
        pkg.require(result['ok'] and result['source']['sha256'] ==
                    self.plan['sources']['docs/STATUS.md'], 'actual project record')
        pkg.save(folder/'tool-result.json', result)
        before_final()
        messages = client.append_tool_results(body['messages'], answer,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow = client.make_request(messages, tools, stream=False,
                                     max_tokens=256, timeout_ms=60000)
        follow['tool_choice'] = 'none'
        answer = self.complete(folder, 'tool-final', follow)
        pkg.require(answer.finish_reason == 'stop' and result['source']['citation'] in
                    answer.message['content'], 'real tool final citation')

    def native(self):
        folder = OUT/'native'
        folder.mkdir()
        port_free()
        manifest = dict(settings=pkg.settings(PORT), model_dir=str(MODEL))
        command = pkg.command(BINARY.parent, manifest)
        self.reserve('starts')
        with (folder/'backend.log').open('xb') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        owner = sup.process_identity(process.pid)
        pkg.save(folder/'owner.json', owner)
        try:
            def ready():
                # Failed connects count too; do not spin through the control budget.
                time.sleep(2)
                try:
                    status, value = self.get('/readyz')
                    return value if status == 200 and value.get('ready') else None
                except (OSError, ValueError):
                    return None
            self.wait(ready, process, 240)
            caps_status, caps = self.get('/v1/capabilities')
            cfg_status, config = self.get('/v1/config')
            pkg.require(caps_status == cfg_status == 200, 'native declarations HTTP')
            pkg.save(folder/'capabilities.json', caps)
            pkg.save(folder/'configuration.json', config)
            self.business(folder)
        finally:
            processes.stop_children({'backend': process}, {}, grace=30, kill_grace=5)
            pkg.save(folder/'reaped.json', dict(owner=owner, returncode=process.returncode,
                remaining=processes.group_members(process.pid)))
        pkg.require(process.returncode == 0 and not processes.group_members(process.pid),
                    'native clean exit')
        port_free()
        self.results['native'] = dict(status='PASS_NATIVE_SERVE_SMOKE', chats=4,
            candidate=identity(BINARY), deployment_accepted=False, performance_acceptance=False)
        pkg.save(folder/'result.json', self.results['native'])
        self.metadata(folder)

    def metadata(self, native):
        declarations = BASE/'declarations'
        (declarations/'schemas').mkdir(parents=True)
        for name in ('capabilities.json', 'configuration.json'):
            (declarations/name).write_bytes((native/name).read_bytes())
        protocol = pkg.read(native/'capabilities.json')
        protocol['protocol_sha256'] = protocol['protocol_source_sha256']
        for name, stem in SCHEMAS.items():
            target = declarations/'schemas'/(name+'.json')
            target.write_bytes((ROOT/'docs'/(stem+'.schema.json')).read_bytes())
            protocol[name+'_schema'] = str(target)
            protocol[name+'_schema_sha256'] = pkg.sha(target)
        metadata = dict(version=1, binary_sha256=pkg.sha(BINARY),
            build_cache=str(BINARY.parent/'CMakeCache.txt'),
            build_cache_sha256=pkg.sha(BINARY.parent/'CMakeCache.txt'),
            source_commit=self.plan['commit'], acceptance='ACCEPT_NATIVE_SERVE_SMOKE',
            acceptance_decision=str(native/'result.json'),
            acceptance_decision_sha256=pkg.sha(native/'result.json'),
            scope='bounded native validation prerequisite; not deployment approval',
            deployment_accepted=False, chat_protocol=protocol,
            observed_configuration=pkg.read(native/'configuration.json'),
            release_declarations=dict(profile=pkg.DECLARATION_PROFILE,
                directory=str(declarations), binary_sha256=pkg.sha(BINARY),
                protocol_sha256=protocol['protocol_sha256'],
                files={name: pkg.sha(declarations/name) for name in pkg.DECLARATION_FILES}))
        pkg.verify_declarations(metadata, required=True)
        pkg.save(BASE/'candidate.release.json', metadata)

    def managed(self, label):
        from tools.serve_service import monitor_control, resources
        folder = OUT/label
        folder.mkdir()
        policy = resources.policy(**self.plan['host_policy']) if label == 'v10' else None
        receipt = pkg.prepare(BINARY, BASE/'candidate.release.json', MODEL,
            BASE/(label+'-package'), port=PORT, nvml_policy='degrade', host_resources=policy)
        pkg.save(BASE/(label+'-package.json'), receipt)
        run_dir = folder/'managed'
        port_free()
        self.reserve('starts')
        with (folder/'controller.log').open('xb') as log:
            process = subprocess.Popen(['/usr/bin/python3', '-I', '-B',
                str(Path(__file__).resolve()), 'controller', label], cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                pass_fds=(self.fd,), start_new_session=True)
        pkg.save(folder/'owner.json', sup.process_identity(process.pid))
        def state():
            if not (run_dir/'status.json').exists():
                return None
            value = service.status(run_dir)
            pkg.require(value['state'] not in ('FAILED', 'STOPPED'), 'managed service stopped')
            pkg.require(value['worker']['attempts'] <= 1 and value['worker']['restarts'] == 0,
                        'no model replacement')
            return value
        owner = None
        try:
            ready = self.wait(lambda: (v if (v := state()) and v['state'] == 'READY' else None),
                              process, 260)
            owner = ready['worker']['backend']
            pkg.save(folder/'ready.json', ready)
            (folder/'fault.request').touch(exist_ok=False)
            def gap_ready():
                v = state()
                monitor = v.get('gpu_monitor', {}) if v else {}
                resource = v['worker'].get('host_resources', {}) if v else {}
                if (monitor.get('suspended') and monitor.get('visibility') == 'UNKNOWN'
                        and (label != 'v10' or resource.get('statistics_gap_samples', 0) > 0)):
                    return v
                return None
            gap = self.wait(gap_ready, process, 20)
            pkg.save(folder/'gap.json', gap)
            pkg.require(gap['gpu_monitor']['query_category'] == 'QUERY_ERROR', 'collector classification')
            with service.guard.common_lock().open('r+') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise RuntimeError('lease released during observation gap')
            def recover():
                current = state()
                pkg.save(folder/'before-recovery.json', current)
                pkg.require(current['worker']['backend'] == owner and
                            current['gpu_monitor']['visibility'] == 'UNKNOWN', 'gap continuity')
                deadline = current['gpu_monitor']['resume_not_before_monotonic']
                self.wait(lambda: time.monotonic() >= deadline+.1, process, 65)
                (folder/'recover.request').touch(exist_ok=False)
                self.reserve('controls')
                pkg.save(folder/'resume.json', monitor_control.request(run_dir))
                restored = self.wait(lambda: (v if (v := state()) and
                    v.get('gpu_monitor', {}).get('visibility') == 'AVAILABLE' and
                    not v['gpu_monitor']['suspended'] else None), process, 20)
                pkg.require(restored['gpu_monitor']['resume_attempts'] == 1 and
                    restored['gpu_monitor']['failed_queries'] > 0, 'recovery history')
                pkg.save(folder/'recovered.json', restored)
            self.business(folder, recover)
            final = state()
            pkg.require(final['worker']['backend'] == owner, 'backend PID changed')
            pkg.save(folder/'business-final.json', final)
        finally:
            (folder/'recover.request').touch(exist_ok=True)
            if (run_dir/'controller.json').exists():
                pkg.save(folder/'stop.json', service.stop(run_dir))
            try:
                process.wait(45)
            except subprocess.TimeoutExpired:
                processes.stop_children({'controller': process}, {}, grace=5, kill_grace=5)
                raise
        terminal = service.status(run_dir)
        pkg.save(folder/'terminal.json', terminal)
        pkg.require(process.returncode == 0 and terminal['state'] == 'STOPPED' and
            terminal['cleanup_confirmed'] and terminal['guard']['status'] ==
            'COMPLETED_WITH_MONITOR_GAPS', 'managed terminal/cleanup')
        counts = pkg.read(folder/'fault-counts.json')
        pkg.require(counts['log_errors'] > 0 and counts['metrics_attempts'] == 0 and
                    counts['manager_attempts'] == 0, 'auxiliary fault coverage')
        if label == 'v10':
            pkg.require(counts['statistics_errors'] > 0 and
                terminal['host_resource_cleanup']['scope_removed'], 'real resource lifecycle')
        port_free()
        self.results[label] = dict(status='PASS_MANAGED_AUXILIARY_ISOLATION',
            chats=4, package=receipt, backend=owner, synthetic_faults=counts,
            cleanup_confirmed=True, model_restarts=0)
        pkg.save(folder/'result.json', self.results[label])

    def run(self):
        def expired(*_):
            raise TimeoutError('1100-second execution budget; cleanup only')
        old = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 1100)
        failure = None
        try:
            self.native()
            self.managed('v9')
            self.managed('v10')
            check()
        except BaseException:
            failure = traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
            result = dict(status='PASS_BOUNDED_SERVE_RESILIENCE' if failure is None else 'FAILED',
                failure=failure, counts=self.counts, seconds=time.monotonic()-self.started,
                stages=self.results, source_commit=self.plan['commit'],
                default_replaced=False, performance_acceptance=False)
            pkg.save(OUT/'result.json', result)
            print(json.dumps(result), flush=True)
        return 0 if failure is None else 1


if __name__ == '__main__':
    os.umask(0o077)
    if MODE == 'prepare':
        prepare()
    elif MODE == 'run':
        with processes.child_subreaper():
            raise SystemExit(Stage().run())
    elif MODE == 'controller':
        raise SystemExit(controller(LABEL))
    elif MODE == 'worker':
        raise SystemExit(worker(LABEL))
    else:
        raise SystemExit('usage: closeout_acceptance.py prepare|run|controller LABEL|worker LABEL')
