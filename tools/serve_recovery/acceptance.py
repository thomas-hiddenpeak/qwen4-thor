"""Bounded A/B business acceptance; only run inside the strict inherited lease.

Controller/launcher modes load unchanged sealed production modules. Explicit
validation wrappers inject auxiliary errors and count work before dispatch;
they are never dependencies of the deployable service bundle.
"""
import argparse
import copy
from dataclasses import asdict
import errno
import fcntl
import http.client
import importlib.machinery
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time
import traceback
import types

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LIMITS = dict(starts=4, chats=20, probes=1, controls=10000, seconds=1800)
COMPLETION_LIMITS = dict(LIMITS, starts=5, chats=21)
OBSERVATION_LIMITS = dict(starts=1, chats=4, probes=0, controls=2000, seconds=600)
OBSERVATION_COMPLETION_LIMITS = dict(OBSERVATION_LIMITS, starts=2)
NATIVE_ACCESS_LIMITS = dict(starts=2, chats=8, probes=0, controls=2000, seconds=900)


def limits_for(scenario):
    return {'full_recovery': LIMITS,
        'controller_gateway_completion': COMPLETION_LIMITS,
        'observation_isolation': OBSERVATION_LIMITS,
        'observation_isolation_completion': OBSERVATION_COMPLETION_LIMITS,
        'native_access': NATIVE_ACCESS_LIMITS}[scenario]


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    with temporary.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


class Budget:
    def __init__(self, directory, *, initialize=False, baseline=None, limits=None):
        self.directory, self.limits = Path(directory), limits or LIMITS
        if initialize:
            if baseline is None:
                baseline = {name: 0 for name in self.limits if name != 'seconds'}
            if (baseline.keys() != {name for name in self.limits if name != 'seconds'} or
                    any(type(value) is not int or not 0 <= value <= self.limits[name]
                        for name,value in baseline.items())):
                raise ValueError('invalid prior acceptance consumption')
            with (self.directory/'budget.lock').open('x'):
                pass
            save(self.directory/'counts.json', dict(counts=baseline, events=[]))

    def reserve(self, kind, label):
        if kind not in self.limits or kind == 'seconds':
            raise ValueError('unknown acceptance budget')
        with (self.directory/'budget.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = json.loads((self.directory/'counts.json').read_text())
            if value['counts'][kind] >= self.limits[kind]:
                raise RuntimeError(kind+' acceptance budget exhausted')
            value['counts'][kind] += 1
            if kind != 'controls':
                value['events'].append(dict(kind=kind, label=label,
                    ordinal=value['counts'][kind], monotonic_ns=time.monotonic_ns()))
            save(self.directory/'counts.json', value)
            return value['counts'][kind]


def await_drain(observe, expected, *, clock=time.monotonic, sleep=time.sleep, seconds=5):
    """A response terminal precedes destruction/reap of request-local workers."""
    end=clock()+seconds
    while clock()<end:
        values=observe(min(1,end-clock()))
        if clock()>=end:
            break
        if values['q4t_requests_total']>expected:
            raise RuntimeError('new generation replayed an unexpected request')
        if (values['q4t_requests_total']==values['q4t_requests_success_total']==expected and
            values['q4t_seq_slots_free']==values['q4t_seq_slots_total']==1 and
            values['q4t_structured_workers_started_total']==values['q4t_structured_workers_reaped_total']):
            return values
        sleep(min(.05,max(0,end-clock())))
    raise TimeoutError('request/grammar ownership did not drain within 5 seconds')


def pin_packaged_modules(source):
    # The sealed inventory uses namespace packages, while the checkout has a
    # regular serve_recovery package. A sys.path prepend alone lets that regular
    # checkout package win. Pin production namespaces before any core import.
    source = Path(source).resolve(strict=True)
    prefixes = ('tools.serve_recovery', 'tools.serve_service',
                'tools.serve_gateway', 'tools.serve_ops')
    for name, module in list(sys.modules.items()):
        if any(name == p or name.startswith(p+'.') for p in prefixes):
            path = getattr(module, '__file__', None)
            if path and not Path(path).resolve().is_relative_to(source):
                raise RuntimeError('production module already loaded outside sealed source: '+name)
    import tools
    for name in prefixes:
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__package__ = name
        module.__path__ = [str(source/Path(*name.split('.')))]
        module.__spec__ = importlib.machinery.ModuleSpec(name, None, is_package=True)
        module.__spec__.submodule_search_locations = module.__path__
        sys.modules[name] = module
        setattr(tools, name.rsplit('.', 1)[1], module)


def load_modules(plan=None):
    source = Path(plan['package']['package'])/'source' if plan else ROOT
    sys.path[:0] = [str(source), str(source/'tools/evalscope'), str(ROOT/'tools/examples')]
    if plan:
        pin_packaged_modules(source)
    from tools.serve_recovery import bundle, child, control, journal, service
    from tools.serve_service import package as engine
    return bundle, child, control, journal, service, engine


def verify(plan):
    bundle, _, _, _, _, engine = load_modules(plan)
    engine.require(plan['limits'] == limits_for(plan.get('scenario', 'full_recovery')),
                   'acceptance budget changed')
    for name, digest in plan['sources'].items():
        engine.require(engine.sha(ROOT/name) == digest, 'acceptance source changed: '+name)
    for row in plan['protected']:
        engine.require(engine.sha(row['path']) == row['sha256'], 'protected artifact changed')
    for row in plan.get('prior_evidence',[]):
        engine.require(engine.sha(row['path']) == row['sha256'],'prior failure evidence changed')
    for row in plan.get('tls_inputs', {}).values():
        engine.require(engine.sha(row['path']) == row['sha256'], 'TLS acceptance input changed')
    return bundle.verify(plan['package']['package'], plan['package']['manifest_sha256'])


def prepare(args):
    bundle, _, _, _, _, engine = load_modules()
    engine.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                   'commit reviewed source before freezing acceptance')
    receipt = json.loads(args.receipt.read_text())
    manifest = bundle.verify(receipt['package'], receipt['manifest_sha256'])
    policy = json.loads((Path(receipt['package'])/'gateway-policy.json').read_text())
    backend = json.loads((Path(receipt['package'])/'engine/client-policy.json').read_text())
    engine.require([c['id'] for c in backend['clients']] == ['alpha', 'beta'] and
        backend['clients'][0]['requests_per_minute'] >= 20 and
        backend['clients'][1]['requests_per_minute'] == 1 and manifest['quota_enforced'],
        'acceptance requires alpha ordinary limits and beta RPM=1')
    native_access = args.native_access
    if native_access:
        engine.require(manifest['version'] in (4, 5, 6, 7, 8) and manifest['ingress_mode'] == 'native'
            and manifest['tls'] and policy['version'] == 3 and policy['cors'] and
            policy['server_name'] == policy['listen_host'] and
            not manifest['tls_configuration']['require_client_certificate'] and
            args.tls_ca is not None and args.next_certificate is not None and
            args.credential_directory is not None, 'native TLS fixture configuration required')
        credential_directory = args.credential_directory.resolve(strict=True)
        certificate = Path(manifest['tls_configuration']['certificate']).resolve(strict=True)
        engine.require(credential_directory.is_relative_to(ROOT/'.q4t-work') and
            certificate.parent == credential_directory and
            not args.next_certificate.resolve(strict=True).is_relative_to(credential_directory),
            'certificate mutation must be confined to a dedicated fixture directory')
        from tools.serve_recovery import tls_control
        tls_control.check(Path(receipt['package'])/'bin/q4t-ingress', policy,
            dict(manifest['tls_configuration'], certificate=str(args.next_certificate.resolve())),
            credential_directory)
    else:
        engine.require(policy['listen_host'] == '127.0.0.1' and not manifest['tls'],
                       'this bounded acceptance is loopback only')
    prior, prior_evidence, baseline = None, [], None
    supplemental=args.controller_completion
    observation=args.observation_isolation or args.observation_completion
    engine.require(not native_access or not (observation or supplemental or args.prior_plan),
                   'native TLS scenario cannot silently reset or mix acceptance phases')
    engine.require(not observation or not supplemental and
        bool(args.prior_plan) == args.observation_completion and
        not (args.observation_isolation and args.observation_completion),
        'observation completion requires the recorded fixture failure')
    scenario=('native_access' if native_access else
        'observation_isolation_completion' if args.observation_completion else
        'observation_isolation' if observation else
        'controller_gateway_completion' if supplemental else 'full_recovery')
    engine.require(not supplemental or args.prior_plan is not None,
                   'completion requires the prior measured failure')
    if args.prior_plan is not None:
        prior_plan=json.loads(args.prior_plan.read_text())
        prior_dir=Path(prior_plan['output'])
        prior=json.loads((prior_dir/'execution/result.json').read_text())
        baseline=prior['budget']['counts']
        expected_counts=(1,1,0) if observation else (3,15,1) if supplemental else (1,5,0)
        engine.require(prior['status']=='FAILED' and prior['cleanup_failure'] is None and
            (baseline['starts'],baseline['chats'],baseline['probes'])==expected_counts and
            prior['package']==receipt,'only the recorded fixture failure can be continued')
        names=['plan.json','execution/result.json','execution/counts.json','lease/exit.json',
            'execution/stop.json']
        names += (['execution/ready-1.json','execution/chat-01-self-check-response.json']
            if observation else ['execution/generation-2-metrics.json','execution/ready-2.json',
            'execution/interrupted-sse-result.json','execution/generation-2-tool-final-response.json']
            if supplemental else ['execution/generation-1-json-response.json',
            'execution/generation-1-sse-response.json','execution/beta-minute-quota-response.json',
            'execution/generation-1-tool-call-response.json','execution/auxiliary-gap.json'])
        if observation:
            engine.require(prior_plan['scenario'] == 'observation_isolation' and
                'observation fault target is not owned writer' in prior['failure'],
                'only the source-loading fixture failure may use this completion')
            names.append('lease/events.jsonl')
        prior_evidence.extend(prior_plan.get('prior_evidence',[]))
        for name in names:
            path=prior_dir/name
            prior_evidence.append(dict(path=str(path.resolve()),sha256=engine.sha(path)))
        engine.require(json.loads((prior_dir/'lease/exit.json').read_text())['status']=='COMMAND_FAILED',
                       'cannot continue an environmental abort as a fixture failure')
    args.output.mkdir(mode=0o700, exist_ok=False)
    paths = subprocess.check_output(['git', 'ls-files', 'tools/serve_recovery',
        'tools/serve_service', 'tools/serve_gateway', 'tools/serve_ops', 'tools/evalscope',
        'tools/examples', 'tools/release', 'src/service', 'include/q4t/service',
        'cmake/GuardianProcess.cmake', 'cmake/RecoveryProbe.cmake', 'docs/MODEL.md'],
        cwd=ROOT, text=True).splitlines()
    paths = sorted(set(paths) | bundle.NATIVE_SOURCES)
    plan = dict(version=1, limits=limits_for(scenario),
        ingress_mode=manifest.get('ingress_mode', 'nginx'),
        scenario=scenario,
        package=receipt, output=str(args.output.resolve()),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],cwd=ROOT,text=True).strip(),
        sources={name: engine.sha(ROOT/name) for name in paths},
        protected=[dict(path=str((ROOT/name).resolve()), sha256=engine.sha(ROOT/name))
                   for name in ('build/q4t', 'build/q4t.release.json')],
        validation_wrappers=['launcher argv selects counted controller',
            'inner NVML timeout and model-log ENOSPC; outer monitoring unchanged',
            'pre-dispatch Chat/model/probe budget counters'],
        sequence=['initial self-check', 'auxiliary gap JSON/SSE/tool + RPM denial',
            'kill owned in-flight model; 503; single CUDA probe; quota wait',
            'second self-check and JSON/SSE/tool', 'kill owned controller; 503; quota wait',
            'third self-check; JSON/SSE; gateway replacement; tool closure',
            'durable stop; no resurrection; owned tree/ports release'],
        planned_model_starts=2 if supplemental else 3,
        planned_chats=6 if supplemental else 15 if prior else 19, performance_acceptance=False,
        prior_counts=baseline, prior_evidence=prior_evidence,
        prior_seconds=prior['seconds']+prior_plan.get('prior_seconds',0) if prior else 0,
        reuse_initial_ordinary_and_quota=prior is not None,
        automatic_request_or_tool_replay=False, default_replacement=False)
    if observation:
        plan.update(planned_model_starts=1, planned_chats=3,
            validation_wrappers=['count model starts and self-check before dispatch',
                'SIGSTOP only the identity-pinned observation writer',
                'one ordinary log marker through the unchanged production Writer',
                'strict outer and inner NVML unchanged'],
            sequence=['self-check and ready', 'writer SIGSTOP and one log marker',
                'same-PID JSON during sink stall', 'writer deadline and bounded loss',
                'same-PID SSE after sink disabled', 'durable stop and full cleanup'])
        plan['protected'].extend(dict(path=str((ROOT/name).resolve()), sha256=engine.sha(ROOT/name))
            for name in ('build/service.release.json', 'build/service/manifest.json'))
        if prior is not None:
            events = [json.loads(line) for line in (prior_dir/'lease/events.jsonl').read_text().splitlines()]
            lease_started = next(e['monotonic'] for e in events if e['event'] == 'lease_acquired')
            lease_finished = next(e['monotonic'] for e in events if e['event'] == 'lease_released')
            plan['prior_seconds'] = max(plan['prior_seconds'], lease_finished-lease_started)
    if native_access:
        plan.update(planned_model_starts=1, planned_chats=6,
            credential_directory=str(credential_directory),
            tls_inputs={name: dict(path=str(path.resolve(strict=True)), sha256=engine.sha(path))
                for name, path in (('ca', args.tls_ca), ('next_certificate', args.next_certificate))},
            origin=policy['cors']['allowed_origins'][0],
            validation_wrappers=['count starts and self-check before dispatch',
                'bounded mutation of dedicated fixture certificate only',
                'normal TLS/Guardian/ingress/controller; strict NVML unchanged'],
            sequence=['self-check and ready', 'TLS1.2 JSON', 'valid certificate update',
                'TLS1.3 SSE', 'invalid update retains context; restore valid disk credential',
                'real document tool and citation', 'structured SSE',
                'same model and control identities', 'durable stop and full cleanup'])
        plan['protected'].extend(dict(path=str((ROOT/name).resolve()), sha256=engine.sha(ROOT/name))
            for name in ('build/service.release.json', 'build/service/manifest.json'))
    save(args.output/'plan.json', plan)
    print(json.dumps(dict(plan=str(args.output/'plan.json'), limits=plan['limits'])))


def launcher(plan, plan_path):
    _, _, _, _, service, engine = load_modules(plan)
    verify(plan)
    output = Path(plan['output'])/'execution'
    original = subprocess.Popen
    guardian = str(Path(plan['package']['package'])/'bin/q4t-guardian')
    def spawn(command, **options):
        if command[0] == guardian:
            path = Path(command[command.index('--config')+1])
            value = engine.read(path)
            original_command = value['controller']
            value['controller'] = [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                'controller', '--plan', str(plan_path), '--config', original_command[-1]]
            engine.save(path, value)
            save(output/'controller-wrapper.json', dict(original_command=original_command,
                validation_command=value['controller'], production_package=plan['package']))
        return original(command, **options)
    subprocess.Popen = spawn
    receipt = plan['package']
    result = service.launch(receipt['package'], receipt['manifest_sha256'], output/'state')
    save(output/'launcher-result.json', result)
    return int(result['guardian_exit_code'] != 0)


def controller_actor(plan, config):
    load_modules(plan)
    verify(plan)
    from tools.serve_recovery import controller, core_http, jobs, monitor
    from tools.serve_service import supervisor
    output = Path(plan['output'])/'execution'
    budget = Budget(output,limits=plan['limits'])
    generation = int(os.environ['Q4T_CONTROLLER_GENERATION'])
    counts = dict(nvml_injected=0, log_errors=0, metrics_attempts=0)
    def record():
        save(output/f'fault-counts-{generation}.json', counts)
    original_job, original_write = jobs.Job, supervisor.RotatingLog.write
    original_get, original_post = supervisor.transport.get, core_http.request
    original_spawn = subprocess.Popen
    def spawn(command, **options):
        if str(controller.ROOT/'tools/serve_recovery/child.py') in command:
            budget.reserve('starts', 'controller-'+str(generation))
        elif str(Path(plan['package']['package'])/'bin/q4t-cuda-probe') in command:
            budget.reserve('probes', 'controller-'+str(generation))
        return original_spawn(command, **options)
    def query(command, **options):
        if (output/'auxiliary-fault').exists():
            counts['nvml_injected'] += 1
            record()
            value = dict(event='result', request_id=command[-1], status='ERROR',
                error=dict(type='NVMLError', message='acceptance synthetic timeout', nvml_code=10))
            code = 'import sys; print('+repr(json.dumps(value))+',flush=True); sys.exit(1)'
            command = [sys.executable, '-B', '-c', code]
        return original_job(command, **options)
    def write(logger, data):
        if (output/'auxiliary-fault').exists() and logger.path.name == 'model.log':
            counts['log_errors'] += 1
            record()
            raise OSError(errno.ENOSPC, 'acceptance synthetic log sink full')
        return original_write(logger, data)
    def get(port, path, **options):
        if path == '/metrics':
            counts['metrics_attempts'] += 1
            record()
            raise ValueError('metrics must not gate this recovery controller')
        return original_get(port, path, **options)
    def post(port, method, path, **options):
        if method == 'POST':
            ordinal = budget.reserve('chats', 'generation-self-check-'+str(generation))
            save(output/f'chat-{ordinal:02d}-self-check-request.json', options['body'])
            result = original_post(port, method, path, **options)
            save(output/f'chat-{ordinal:02d}-self-check-response.json', result)
            return result
        return original_post(port, method, path, **options)
    original_monitor = monitor.Monitor
    monitor.Monitor = lambda fd: original_monitor(fd, factory=query)
    supervisor.RotatingLog.write, supervisor.transport.get = write, get
    core_http.request, subprocess.Popen = post, spawn
    if plan.get('scenario', '').startswith('observation_isolation'):
        original_tick = controller.Controller.tick
        def tick(actor, **kwargs):
            result = original_tick(actor, **kwargs)
            if (output/'writer-stalled').exists() and not counts.get('marker_submitted'):
                if actor.logger is not None:
                    from tools.serve_recovery.observation_io import LOG
                    accepted = actor.logger.submit(LOG, b'bounded acceptance log marker\n')
                    counts['marker_submitted'] = int(accepted)
                    record()
            return result
        controller.Controller.tick = tick
    record()
    sys.argv = [sys.argv[0], '--config', str(config)]
    controller.main()
    record()


class Stage:
    def __init__(self, plan, plan_path):
        self.plan, self.plan_path = plan, plan_path
        self.bundle, self.child, self.control, self.journal, self.service, self.engine = load_modules(plan)
        manifest = verify(plan)
        self.package = Path(plan['package']['package'])
        self.output = Path(plan['output'])/'execution'
        self.output.mkdir(mode=0o700, exist_ok=False)
        self.budget = Budget(self.output, initialize=True,baseline=plan.get('prior_counts'),limits=plan['limits'])
        self.lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
        self.lock_path = manifest['lock_path']
        self.child.verify_lease(self.lease, self.lock_path)
        self.policy = json.loads((self.package/'gateway-policy.json').read_text())
        self.port = self.policy['listen_port']
        self.keys = {c['id']: c['api_key'] for c in self.policy['clients']}
        self.keys['invalid'] = 'invalid-key-for-bounded-acceptance'
        self.started = time.monotonic()
        self.process = None
        self.models = {}
        self.quota_deadlines = {}
        self.service_id = None

    def sample(self):
        self.budget.reserve('controls', 'status')
        value = self.control.request(self.service_id, 'status', timeout=2)
        now = time.monotonic()
        generation, pid = value['model_generation'], value['model_pid']
        if pid is not None:
            for other, owner in self.models.items():
                if other != generation:
                    try:
                        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(')',1)[1].split()
                        self.engine.require(int(fields[19]) != owner['start_ticks'] or fields[0] == 'Z',
                                            'overlapping live model generations')
                    except FileNotFoundError:
                        pass
            owner = dict(pid=pid, start_ticks=value['model_start_ticks'])
            self.engine.require(generation not in self.models or self.models[generation] == owner,
                                'same model generation changed identity')
            self.models[generation] = owner
        # The gate is set when this model generation is granted, before spawn.
        if value['quota_wait_ms']:
            self.quota_deadlines[generation] = now+value['quota_wait_ms']/1000
        with (self.output/'status.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(monotonic=now, **value))+'\n')
        self.engine.require(not value['recovery_frozen'] and value['state'] != 'QUARANTINED',
                            'service entered protective state')
        self.child.verify_lease(self.lease, self.lock_path)
        return value

    def wait(self, predicate, seconds=360):
        end = min(time.monotonic()+seconds,
            self.started+self.plan['limits']['seconds']-70-self.plan.get('prior_seconds',0))
        last = None
        while time.monotonic() < end:
            self.engine.require(self.process.poll() is None, 'launcher exited unexpectedly')
            try:
                last = self.sample()
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(.25)
                continue
            if predicate(last):
                return last
            time.sleep(.25)
        save(self.output/'wait-timeout.json', last)
        raise TimeoutError('bounded business recovery deadline')

    def ready(self, generation):
        result = self.wait(lambda s: s['model_generation'] == generation and s['business_ready'])
        deadline = self.quota_deadlines.get(generation)
        self.engine.require(generation == 1 or deadline is not None and time.monotonic() >= deadline-.05,
                            'missing or early quota recovery window')
        save(self.output/f'ready-{generation}.json', result)
        save(self.output/f'ledger-{generation}.json', self.engine.read(self.output/'state/ledger.json'))
        return result

    def request(self, label, body=None, *, client='alpha', path='/v1/models', expected=200):
        from tools.examples import chat_client
        if body is not None:
            self.budget.reserve('chats', label)
            save(self.output/(label+'-request.json'), body)
            path = '/v1/chat/completions'
        else:
            self.budget.reserve('controls', label)
        headers = {'Content-Type': 'application/json',
                   'Authorization': 'Bearer '+self.keys[client]}
        if self.plan.get('scenario') == 'native_access':
            context = ssl.create_default_context(cafile=self.plan['tls_inputs']['ca']['path'])
            version = ssl.TLSVersion.TLSv1_2 if label.endswith('-json') else ssl.TLSVersion.TLSv1_3
            context.minimum_version = context.maximum_version = version
            connection = http.client.HTTPSConnection(self.policy['listen_host'], self.port,
                                                      timeout=65, context=context)
            headers['Origin'] = self.plan['origin']
        else:
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=65)
        start = time.monotonic()
        try:
            connection.request('POST' if body is not None else 'GET', path,
                json.dumps(body).encode() if body is not None else None,
                headers)
            response = connection.getresponse()
            if self.plan.get('scenario') == 'native_access':
                self.engine.require(response.getheader('Access-Control-Allow-Origin') ==
                    self.plan['origin'], 'native CORS response missing')
            with (self.output/(label+'-wire.bin')).open('xb') as wire:
                if body is not None and response.status == expected == 200:
                    result = (chat_client.read_sse if body['stream'] else chat_client.read_json)(response,sink=wire.write)
                    self.engine.require(result.id == body['request_id'] and result.model == body['model'],
                                        'completion identity changed')
                    value = asdict(result)
                else:
                    raw = response.read(1024*1024)
                    wire.write(raw)
                    value, result = json.loads(raw), None
            save(self.output/(label+'-response.json'), dict(status=response.status,
                headers=dict(response.getheaders()), seconds=time.monotonic()-start, value=value))
            self.engine.require(response.status == expected, 'unexpected HTTP status for '+label)
            return result or value
        finally:
            connection.close()

    def business(self, label, *, first=False, before_tools=lambda: None,
                 before_stream=lambda: None):
        from tools.examples import chat_client as client
        from project_records import ProjectRecords, TOOLS, VALIDATORS
        streams=() if first and self.plan.get('reuse_initial_ordinary_and_quota') else (False,True)
        for stream in streams:
            if stream:
                before_stream()
            body = client.make_request([dict(role='user', content='Compute 17 + 25. Reply with the number only.')],
                [], stream=stream, max_tokens=128, timeout_ms=60000)
            answer = self.request(label+('-sse' if stream else '-json'), body,
                                  client='beta' if not stream else 'alpha')
            self.engine.require(answer.finish_reason == 'stop' and answer.message['content'].strip() == '42',
                                'JSON/SSE quality')
            if first and not stream:
                denied = copy.deepcopy(body)
                denied.update(request_id='agent-'+secrets.token_hex(16),cancel_token=secrets.token_hex(32))
                self.request('beta-minute-quota', denied, client='beta', expected=429)
        before_tools()
        tools = copy.deepcopy(TOOLS[:1])
        tools[0]['function']['strict'] = True
        parameters = tools[0]['function']['parameters']['properties']
        parameters['path'] = dict(type='string',enum=['docs/MODEL.md'])
        body = client.make_request([dict(role='user',content='Read the first 16 lines of docs/MODEL.md with '
            'read_project_record. Then reply only with the exact citation returned by the tool.')],
            tools,stream=True,max_tokens=256,timeout_ms=60000)
        body.update(tool_choice=dict(type='function',function=dict(name='read_project_record')),
                    parallel_tool_calls=False)
        answer = self.request(label+'-tool-call',body)
        calls = client.prepare_tool_calls(answer,VALIDATORS)
        self.engine.require(len(calls)==1 and calls[0].arguments ==
            dict(path='docs/MODEL.md',start_line=1,line_count=16),'tool argument contract')
        result = ProjectRecords(ROOT).execute(calls[0])
        self.engine.require(result['ok'] and result['source']['sha256']==self.plan['sources']['docs/MODEL.md'],
                            'real project record identity')
        save(self.output/(label+'-tool-result.json'),dict(call_id=calls[0].call_id,executions=1,result=result))
        messages = client.append_tool_results(body['messages'],answer,
            {calls[0].call_id:json.dumps(result,ensure_ascii=False)})
        follow = client.make_request(messages,tools,stream=False,max_tokens=256,timeout_ms=60000)
        follow['tool_choice']='none'
        final = self.request(label+'-tool-final',follow)
        self.engine.require(final.finish_reason=='stop' and
            result['source']['citation'] in final.message['content'] and not final.message.get('tool_calls'),
            'tool closure/citation quality')

    def metrics(self,label,expected):
        from tools.serve_ops import contracts,transport
        observations=[]
        def observe(timeout):
            self.budget.reserve('controls',label+'-metrics')
            response=transport.get(self.policy['upstream_operations_port'],'/metrics',timeout=timeout)
            self.engine.require(response['http_status']==200,'acceptance metrics unavailable')
            values,_=contracts.parse_metrics(response['body'],version=5)
            observations.append(dict(monotonic=time.monotonic(),response=response,values=values))
            save(self.output/(label+'-metrics.json'),dict(observations=observations))
            return values
        return await_drain(observe,expected)

    def recover_controller(self,current,generation):
        self.kill(current,'controller','controller-exit')
        self.unavailable('controller-recovery-503',post=False)
        replacement=self.ready(generation)
        self.engine.require(replacement['controller_generation']==2 and
            replacement['gateway_pid']==current['gateway_pid'],'controller recovery changed wrong role')
        def gateway_recovery():
            self.kill(replacement,'gateway','gateway-exit')
            rebound=self.wait(lambda s:s['business_ready'] and s['gateway_generation']==2,seconds=25)
            self.engine.require(rebound['model_pid']==replacement['model_pid'] and
                rebound['controller_pid']==replacement['controller_pid'],'gateway recovery replaced model/controller')
            save(self.output/'gateway-recovered.json',rebound)
        self.business('generation-'+str(generation),before_tools=gateway_recovery)
        self.metrics('generation-'+str(generation),5)
        final=self.sample()
        self.engine.require(final['model_generation']==generation and final['controller_generation']==2 and
            final['guardian_pid']==current['guardian_pid'],'unexpected replacement count')
        save(self.output/'business-final.json',final)

    def exercise(self):
        first=self.ready(1)
        if self.plan.get('scenario') == 'native_access':
            return self.native_access(first)
        if self.plan.get('scenario', '').startswith('observation_isolation'):
            return self.observation_isolation(first)
        if self.plan.get('scenario')=='controller_gateway_completion':
            self.metrics('initial-self-check',1)
            self.recover_controller(first,2)
            return
        (self.output/'auxiliary-fault').touch(exist_ok=False)
        gap=self.wait(lambda s:s['business_ready'] and s['controller_health'] and
            s['controller_health']['observation']=='UNKNOWN' and
            s['controller_health']['reason']=='nvml_timeout',seconds=15)
        self.engine.require(gap['model_pid']==first['model_pid'],'auxiliary error replaced healthy model')
        save(self.output/'auxiliary-gap.json',gap)
        self.business('generation-1',first=True)
        self.engine.require(self.sample()['model_pid']==first['model_pid'],'auxiliary business changed model PID')
        self.interrupted_stream(first)
        self.unavailable('model-recovery-503')
        second=self.ready(2)
        self.engine.require(second['controller_pid']==first['controller_pid'] and
            second['gateway_pid']==first['gateway_pid'] and second['probe_used_for_generation']==1,
            'model recovery ownership/probe mismatch')
        self.business('generation-2')
        self.metrics('generation-2',5)
        (self.output/'auxiliary-fault').unlink()
        self.recover_controller(second,3)
        counts=self.engine.read(self.output/'fault-counts-1.json')
        self.engine.require(counts['nvml_injected']>0 and counts['log_errors']>0 and
            counts['metrics_attempts']==0,'auxiliary coverage incomplete')

    def native_access(self, first):
        from tools.examples import chat_client
        from tools.serve_recovery import tls_control
        manifest = self.bundle.verify(self.package, self.plan['package']['manifest_sha256'])
        descriptor = self.engine.read(self.output/'state/service.json')
        certificate = Path(manifest['tls_configuration']['certificate'])
        self.engine.require(certificate.resolve().parent ==
            Path(self.plan['credential_directory']), 'unexpected mutable credential path')
        original = certificate.read_bytes()
        replacement = Path(self.plan['tls_inputs']['next_certificate']['path']).read_bytes()
        def install(data):
            pending = certificate.with_name('acceptance-certificate-'+secrets.token_hex(8))
            with pending.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(certificate)
        def update(label, *, reload=False):
            self.budget.reserve('controls', label)
            state = self.sample()
            for field in ('model_pid', 'model_start_ticks', 'model_generation',
                          'controller_pid', 'controller_start_ticks', 'gateway_pid',
                          'gateway_start_ticks', 'guardian_pid'):
                self.engine.require(state[field] == first[field], 'TLS update changed '+field)
            value = tls_control.execute(descriptor, state, manifest, reload=reload)
            save(self.output/(label+'.json'), value)
            return value
        initial = update('tls-initial')
        self.engine.require(initial['ready'] and initial['generation'] == 1, 'TLS not initially ready')
        current = initial
        def valid_update():
            nonlocal current
            install(replacement)
            current = update('tls-valid-update', reload=True)
            self.engine.require(current.get('reloaded') and
                current['fingerprint_sha256'] != initial['fingerprint_sha256'] and
                current['entrypoint_pid'] == initial['entrypoint_pid'], 'valid update not applied')
        def invalid_update():
            install(b'bounded acceptance invalid certificate\n')
            try:
                rejected = update('tls-invalid-update', reload=True)
                self.engine.require(not rejected.get('reloaded') and rejected['ready'] and
                    rejected['error'] == 'tls_certificate_invalid' and
                    rejected['generation'] == current['generation'] and
                    rejected['fingerprint_sha256'] == current['fingerprint_sha256'],
                    'bad certificate replaced healthy TLS context')
            finally:
                install(replacement)
            restored = update('tls-restored', reload=True)
            self.engine.require(restored.get('reloaded'), 'valid credential restoration failed')
        try:
            self.request('tls-unauthorized', client='invalid', expected=401)
            self.business('tls', before_stream=valid_update, before_tools=invalid_update)
            body = chat_client.make_request([dict(role='user', content='Return JSON with ok true.')],
                [], stream=True, max_tokens=64, timeout_ms=60000)
            body['response_format'] = dict(type='json_schema', json_schema=dict(
                name='native_access', strict=True, schema=dict(type='object',
                    properties=dict(ok=dict(type='boolean', enum=[True])),
                    required=['ok'], additionalProperties=False)))
            answer = self.request('tls-structured-sse', body)
            self.engine.require(answer.finish_reason == 'stop' and
                json.loads(answer.message['content']) == {'ok': True}, 'structured TLS quality')
            self.metrics('tls-final', 6)
            update('tls-final')
            save(self.output/'business-final.json', self.sample())
        finally:
            install(original)
            # Restoration is local and explicit; it never dispatches a model request.
            update('tls-original-restored', reload=True)

    def observation_isolation(self, first):
        from tools.examples import chat_client
        first = self.wait(lambda s: s['business_ready'] and
            s['controller_health']['auxiliary_io']['worker_state'] == 'AVAILABLE', seconds=10)
        pid = first['controller_health']['auxiliary_io']['worker_pid']
        fd = os.pidfd_open(pid)
        try:
            ticks = self.journal.process_start(pid)
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            self.engine.require(int(fields[1]) == first['controller_pid'] and
                str(self.package/'source/tools/serve_recovery/observation_io.py').encode() in argv and
                self.journal.process_start(pid) == ticks, 'observation fault target is not owned writer')
            save(self.output/'writer-fault.json', dict(pid=pid, start_ticks=ticks,
                signal='SIGSTOP', model_pid=first['model_pid']))
            signal.pidfd_send_signal(fd, signal.SIGSTOP)
            (self.output/'writer-stalled').touch(exist_ok=False)
            for stream in (False, True):
                if stream:
                    gap = self.wait(lambda s: s['business_ready'] and
                        s['controller_health']['auxiliary_io']['worker_state'] == 'DISABLED' and
                        s['controller_health']['auxiliary_io']['worker_pid'] == 0, seconds=15)
                    stats = gap['controller_health']['auxiliary_io']
                    self.engine.require(gap['controller_health']['reason'] == 'auxiliary_io_unavailable' and
                        stats['failed_operations'] > 0 and stats['dropped_bytes'] > 0 and
                        stats['queued_bytes'] <= 128*1024 and stats['queued_records'] <= 32,
                        'bounded observation loss missing')
                    save(self.output/'observation-gap.json', gap)
                body = chat_client.make_request([dict(role='user',
                    content='Compute 17 + 25. Reply with the number only.')], [],
                    stream=stream, max_tokens=128, timeout_ms=60000)
                answer = self.request('isolated-sse' if stream else 'stalled-json', body)
                self.engine.require(answer.finish_reason == 'stop' and
                    answer.message['content'].strip() == '42', 'isolated I/O business quality')
                current = self.sample()
                for field in ('model_pid', 'model_generation', 'controller_pid', 'gateway_pid', 'guardian_pid'):
                    self.engine.require(current[field] == first[field], 'observation failure changed '+field)
            self.metrics('isolated-final', 3)
            save(self.output/'business-final.json', self.sample())
            self.engine.require(not Path(f'/proc/{pid}').exists(), 'writer was not actually reaped')
        finally:
            os.close(fd)

    def kill(self, state, role, label):
        pid, ticks = state[role+'_pid'], state[role+'_start_ticks']
        fd = os.pidfd_open(pid)
        try:
            self.engine.require(self.journal.process_start(pid)==ticks,'owned fault target identity')
            save(self.output/(label+'-fault.json'),dict(role=role,pid=pid,start_ticks=ticks,
                signal='SIGKILL',monotonic=time.monotonic()))
            signal.pidfd_send_signal(fd,signal.SIGKILL)
        finally:
            os.close(fd)

    def interrupted_stream(self, state):
        from tools.examples import chat_client as client
        body = client.make_request([dict(role='user',content='从1开始逐行输出每个整数，直到达到输出上限；不要提前结束。')],
            [],stream=True,max_tokens=256,timeout_ms=60000)
        self.budget.reserve('chats','interrupted-sse')
        save(self.output/'interrupted-sse-request.json',body)
        connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=65)
        captured, killed, error = bytearray(),False,None
        try:
            connection.request('POST','/v1/chat/completions',json.dumps(body).encode(),
                {'Content-Type':'application/json','Authorization':'Bearer '+self.keys['alpha']})
            response = connection.getresponse()
            self.engine.require(response.status==200,'in-flight SSE not admitted')
            while len(captured)<1024*1024:
                line=response.readline(65536)
                if not line: break
                captured.extend(line)
                if not killed and line.startswith(b'data:'):
                    self.engine.require(b'[DONE]' not in line,'SSE finished before fault')
                    self.kill(state,'model','model-exit')
                    killed=True
        except (http.client.HTTPException,ConnectionError) as caught:
            error=type(caught).__name__
        finally:
            connection.close()
            (self.output/'interrupted-sse-wire.bin').write_bytes(captured)
            save(self.output/'interrupted-sse-result.json',dict(killed=killed,error=error,bytes=len(captured)))
        self.engine.require(killed and b'[DONE]' not in captured,'old SSE synthesized success or was not interrupted')

    def unavailable(self,label,*,post=True):
        from tools.examples import chat_client as client
        self.wait(lambda s:not s['business_ready'],seconds=15)
        body=client.make_request([dict(role='user',content='Do not execute during recovery.')],[],
                                stream=False,max_tokens=16,timeout_ms=5000)
        result=self.request(label,body if post else None,expected=503)
        self.engine.require(result['error']['code'] in ('service_recovering','service_quota_wait'),
                            'wrong recovery error contract')
        self.request(label+'-unauthorized',client='invalid',expected=401)

    def stop(self):
        if self.process is None: return
        if self.process.poll() is None:
            try:
                result=self.control.request(self.service_id,'stop',timeout=45)
                save(self.output/'stop.json',result)
                self.engine.require(result['cleanup_confirmed'] and result['stop_durable'],'stop not confirmed')
            except BaseException:
                self.process.send_signal(signal.SIGTERM)
                raise
        self.process.wait(timeout=10)
        self.engine.require(self.process.returncode==0,'launcher cleanup failed')
        persistent=self.engine.read(self.output/'state/ledger.json')['state']
        save(self.output/'stopped-ledger.json',persistent)
        self.engine.require(persistent['desired']=='STOPPED' and
            all(v is None for v in persistent['active'].values()),'persistent service not stopped')
        for owner in self.models.values():
            try:
                self.engine.require(self.journal.process_start(owner['pid'])!=owner['start_ticks'],'owned model survives stop')
            except (FileNotFoundError,ProcessLookupError): pass
        for port in (self.port,self.policy['upstream_port'],self.policy['upstream_operations_port']):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                sock.bind((self.policy['listen_host'] if port == self.port else '127.0.0.1',port))
        self.child.verify_lease(self.lease,self.lock_path)
        time.sleep(2)
        try:
            self.control.request(self.service_id,'status',timeout=1)
        except (ConnectionRefusedError,FileNotFoundError):
            return
        raise RuntimeError('Guardian resurrected after durable stop')

    def run(self):
        failure, cleanup_failure = None,None
        def expired(*_args): raise TimeoutError('bounded execution deadline; cleanup only')
        previous = signal.signal(signal.SIGALRM,expired)
        previous_term = signal.signal(signal.SIGTERM,expired)
        signal.setitimer(signal.ITIMER_REAL,
            self.plan['limits']['seconds']-70-self.plan.get('prior_seconds',0))
        try:
            receipt=self.plan['package']
            initialized=self.service.initialize(receipt['package'],receipt['manifest_sha256'],self.output/'state')
            self.service_id=initialized['service_id']
            save(self.output/'initialized.json',initialized)
            with (self.output/'launcher.log').open('xb') as log:
                self.process=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),
                    'launcher','--plan',str(self.plan_path)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    pass_fds=(self.lease,),start_new_session=True,
                    env={k:v for k,v in os.environ.items() if k not in ('DBUS_SESSION_BUS_ADDRESS','NOTIFY_SOCKET','WATCHDOG_USEC')})
            self.exercise()
            verify(self.plan)
        except BaseException:
            failure=traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try: self.stop()
            except BaseException: cleanup_failure=traceback.format_exc()
            signal.signal(signal.SIGALRM,previous)
            signal.signal(signal.SIGTERM,previous_term)
            result=dict(status='PASS_BOUNDED_RECOVERY_BUSINESS' if not failure and not cleanup_failure else 'FAILED',
                failure=failure,cleanup_failure=cleanup_failure,seconds=time.monotonic()-self.started,
                budget=self.engine.read(self.output/'counts.json'),models=self.models,
                quota_deadlines=self.quota_deadlines,source_commit=self.plan['source_commit'],
                package=self.plan['package'],default_replaced=False,performance_acceptance=False)
            save(self.output/'result.json',result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        return int(failure is not None or cleanup_failure is not None)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','run','launcher','controller'))
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--receipt',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--prior-plan',type=Path)
    parser.add_argument('--native-access', action='store_true')
    parser.add_argument('--tls-ca', type=Path)
    parser.add_argument('--next-certificate', type=Path)
    parser.add_argument('--credential-directory', type=Path)
    parser.add_argument('--controller-completion',action='store_true',
        help='Prepared supplemental scenario; run only after explicit budget extension')
    parser.add_argument('--observation-isolation', action='store_true',
        help='Only observation sink isolation, 1 model / 4 Chat / 600 seconds')
    parser.add_argument('--observation-completion', action='store_true',
        help='Complete the recorded source-loader failure; cumulative 2 models / 4 Chat / 600 seconds')
    args=parser.parse_args()
    if args.mode=='prepare':
        prepare(args)
        return 0
    plan=json.loads(args.plan.read_text())
    if args.mode=='launcher': return launcher(plan,args.plan)
    if args.mode=='controller':
        controller_actor(plan,args.config)
        return 0
    from tools.release.release import child_subreaper
    with child_subreaper(): return Stage(plan,args.plan).run()


if __name__=='__main__':
    sys.path.insert(0,str(ROOT))
    raise SystemExit(main())
