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
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
LIMITS = dict(starts=4, chats=20, probes=1, controls=10000, seconds=1800)


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    with temporary.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


class Budget:
    def __init__(self, directory, *, initialize=False):
        self.directory = Path(directory)
        if initialize:
            with (self.directory/'budget.lock').open('x'):
                pass
            save(self.directory/'counts.json', dict(
                counts={name: 0 for name in LIMITS if name != 'seconds'}, events=[]))

    def reserve(self, kind, label):
        if kind not in LIMITS or kind == 'seconds':
            raise ValueError('unknown acceptance budget')
        with (self.directory/'budget.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = json.loads((self.directory/'counts.json').read_text())
            if value['counts'][kind] >= LIMITS[kind]:
                raise RuntimeError(kind+' acceptance budget exhausted')
            value['counts'][kind] += 1
            if kind != 'controls':
                value['events'].append(dict(kind=kind, label=label,
                    ordinal=value['counts'][kind], monotonic_ns=time.monotonic_ns()))
            save(self.directory/'counts.json', value)
            return value['counts'][kind]


def load_modules(plan=None):
    source = Path(plan['package']['package'])/'source' if plan else ROOT
    sys.path[:0] = [str(source), str(source/'tools/evalscope'), str(ROOT/'tools/examples')]
    from tools.serve_recovery import bundle, child, control, journal, service
    from tools.serve_service import package as engine
    return bundle, child, control, journal, service, engine


def verify(plan):
    bundle, _, _, _, _, engine = load_modules(plan)
    engine.require(plan['limits'] == LIMITS, 'acceptance budget changed')
    for name, digest in plan['sources'].items():
        engine.require(engine.sha(ROOT/name) == digest, 'acceptance source changed: '+name)
    for row in plan['protected']:
        engine.require(engine.sha(row['path']) == row['sha256'], 'protected artifact changed')
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
    engine.require(policy['listen_host'] == '127.0.0.1' and not manifest['tls'],
                   'this bounded acceptance is loopback only')
    args.output.mkdir(mode=0o700, exist_ok=False)
    paths = subprocess.check_output(['git', 'ls-files', 'tools/serve_recovery',
        'tools/serve_service', 'tools/serve_gateway', 'tools/serve_ops', 'tools/evalscope',
        'tools/examples', 'tools/release', 'src/service', 'include/q4t/service',
        'cmake/GuardianProcess.cmake', 'cmake/RecoveryProbe.cmake', 'docs/MODEL.md'],
        cwd=ROOT, text=True).splitlines()
    plan = dict(version=1, limits=LIMITS, package=receipt, output=str(args.output.resolve()),
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
        planned_model_starts=3, planned_chats=19, performance_acceptance=False,
        automatic_request_or_tool_replay=False, default_replacement=False)
    save(args.output/'plan.json', plan)
    print(json.dumps(dict(plan=str(args.output/'plan.json'), limits=LIMITS)))


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
    budget = Budget(output)
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
        self.budget = Budget(self.output, initialize=True)
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
        end = min(time.monotonic()+seconds, self.started+1730)
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
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=65)
        start = time.monotonic()
        try:
            connection.request('POST' if body is not None else 'GET', path,
                json.dumps(body).encode() if body is not None else None,
                {'Content-Type': 'application/json', 'Authorization': 'Bearer '+self.keys[client]})
            response = connection.getresponse()
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

    def business(self, label, *, first=False, before_tools=lambda: None):
        from tools.examples import chat_client as client
        from project_records import ProjectRecords, TOOLS, VALIDATORS
        for stream in (False, True):
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
        parameters['start_line'] = dict(type='integer',enum=[1])
        parameters['line_count'] = dict(type='integer',enum=[16])
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
        self.budget.reserve('controls',label+'-metrics')
        response=transport.get(self.policy['upstream_operations_port'],'/metrics',timeout=5)
        self.engine.require(response['http_status']==200,'acceptance metrics unavailable')
        values,_=contracts.parse_metrics(response['body'],version=5)
        save(self.output/(label+'-metrics.json'),dict(response=response,values=values))
        self.engine.require(values['q4t_requests_total']==values['q4t_requests_success_total']==expected,
                            'new generation replayed or lost a request')
        self.engine.require(values['q4t_seq_slots_free']==values['q4t_seq_slots_total']==1 and
            values['q4t_structured_workers_started_total']==values['q4t_structured_workers_reaped_total'],
            'request/grammar ownership did not return')

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

    def unavailable(self,label):
        from tools.examples import chat_client as client
        self.wait(lambda s:not s['business_ready'],seconds=15)
        body=client.make_request([dict(role='user',content='Do not execute during recovery.')],[],
                                stream=False,max_tokens=16,timeout_ms=5000)
        result=self.request(label,body,expected=503)
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
                sock.bind(('127.0.0.1',port))
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
        signal.setitimer(signal.ITIMER_REAL,1730)
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
            first=self.ready(1)
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
            self.kill(second,'controller','controller-exit')
            self.unavailable('controller-recovery-503')
            third=self.ready(3)
            self.engine.require(third['controller_generation']==2 and third['gateway_pid']==first['gateway_pid'],
                                'controller recovery changed wrong role')
            def gateway_recovery():
                self.kill(third,'gateway','gateway-exit')
                rebound=self.wait(lambda s:s['business_ready'] and s['gateway_generation']==2,seconds=25)
                self.engine.require(rebound['model_pid']==third['model_pid'] and
                    rebound['controller_pid']==third['controller_pid'],'gateway recovery replaced model/controller')
                save(self.output/'gateway-recovered.json',rebound)
            self.business('generation-3',before_tools=gateway_recovery)
            self.metrics('generation-3',5)
            final=self.sample()
            self.engine.require(final['model_generation']==3 and final['controller_generation']==2 and
                final['guardian_pid']==first['guardian_pid'],'unexpected replacement count')
            counts=self.engine.read(self.output/'fault-counts-1.json')
            self.engine.require(counts['nvml_injected']>0 and counts['log_errors']>0 and
                counts['metrics_attempts']==0,'auxiliary coverage incomplete')
            save(self.output/'business-final.json',final)
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
