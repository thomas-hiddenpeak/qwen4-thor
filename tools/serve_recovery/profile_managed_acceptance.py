"""Real managed primary -> light -> primary, bounded RAM and business only.

Validation wrappers count starts/self-checks before dispatch. They do not alter
production observations, recovery choices, journal authority or model behavior.
"""
import argparse
import copy
import http.client
import io
import json
import mmap
import os
from pathlib import Path
import runpy
import select
import signal
import socket
import subprocess
import sys
import time
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
LEGACY = runpy.run_path(str(ROOT/'tools/serve_recovery/acceptance.py'))
LIMITS = dict(starts=3, chats=12, probes=0, controls=4000, seconds=900)
OLD_LIMITS = LEGACY['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS if name == 'adaptive_profiles' else OLD_LIMITS(name))
save = LEGACY['save']
MIB = 1024**2
PRESSURE = dict(target_mib=23552, floor_mib=20480, max_mib=24576,
                chunk_mib=128, seconds=120)


def pressure(output, release_fd, config):
    """Owned anonymous pages; real MemAvailable, EOF/deadline always release."""
    from tools.serve_service import memory
    from tools.serve_service.package import require
    require(0 < config['chunk_mib'] <= config['max_mib'] and
        0 < config['floor_mib'] < config['target_mib'] and
        0 < config['seconds'] <= 120, 'invalid pressure bounds')
    pages, observations = [], []
    started, reason, code = time.monotonic(), 'deadline', 2
    try:
        while time.monotonic()-started < config['seconds']:
            if select.select([release_fd], [], [], .05)[0]:
                os.read(release_fd, 1)
                reason, code = 'released_by_owner', 0
                break
            available = memory.read()['available_bytes']
            allocated = len(pages)*config['chunk_mib']*MIB
            observations.append(dict(monotonic=time.monotonic(),
                available_bytes=available, allocated_bytes=allocated))
            save(output, dict(state='holding' if available <= config['target_mib']*MIB
                else 'allocating', observations=observations, allocated_bytes=allocated))
            if available < config['floor_mib']*MIB:
                reason = 'independent_floor'; break
            if available <= config['target_mib']*MIB: continue
            if allocated+config['chunk_mib']*MIB > config['max_mib']*MIB:
                reason = 'allocation_bound'; break
            size = config['chunk_mib']*MIB
            page = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            pages.append(page)
            page[::4096] = b'\1'*(size//4096)
    finally:
        allocated = len(pages)*config['chunk_mib']*MIB
        for page in pages: page.close()
        save(output, dict(state='released', reason=reason, allocated_bytes=allocated,
            observations=observations, seconds=time.monotonic()-started,
            all_owned_pages_released=True))
    return code


def prepare(args):
    bundle, _, _, _, _, engine = LEGACY['load_modules']()
    from tools.serve_recovery import profile_policy
    engine.require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
                   'commit reviewed sources before managed acceptance')
    out = args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work'), 'private acceptance directory')
    out.mkdir(mode=0o700, parents=True)
    original = args.package.resolve()
    old = bundle.verify(original, engine.sha(original/'manifest.json'))
    base = engine.read(original/'engine/manifest.json')
    gateway = engine.read(original/'gateway-policy.json')
    backend = engine.read(original/'engine/client-policy.json')
    engine.require([c['id'] for c in backend['clients']] == ['alpha','beta'] and
        backend['clients'][1]['requests_per_minute'] == 1, 'original client quota differs')
    gateway.update(listen_port=args.port, upstream_port=args.port+1,
                   upstream_operations_port=args.port+2)
    for item in gateway['clients']: item['api_key'] = 'qualified-'+item['id']+'-'+'c'*48
    backend['gateway_token'] = 'd'*64
    engine.save(out/'gateway-policy.json', gateway)
    engine.save(out/'client-policy.json', backend)
    native = engine.read(original/'native-build.json')
    changed = {p for p,h in native['source_files'].items() if engine.sha(ROOT/p) != h}
    engine.require(changed == {'src/service/guardian_main.cpp'}, 'native rebuild source scope')
    build = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
    evidence = engine.read(ROOT/'docs/evidence/serve-adaptive-profile-integration-20261111/review.json')
    engine.require(engine.sha(build/'q4t-guardian') == evidence['guardian_sha256'],
                   'accepted Guardian build changed')
    native['source_files'] = {p:engine.sha(ROOT/p) for p in native['source_files']}
    native['guardian_sha256'] = engine.sha(build/'q4t-guardian')
    engine.save(out/'native-build.json', native)
    policy = dict(profile_policy.DEFAULTS, degrade_below_mib=24576,
        recover_above_mib=28672, pressure_seconds=2, recovery_seconds=30,
        minimum_light_seconds=60, cooldown_seconds=60)
    application = {k:v for k,v in base['settings']['application_state'].items() if k != 'profile'}
    receipt = bundle.prepare(original/'engine/q4t', original/'engine/q4t.release.json',
        base['model_dir'], build/'q4t-guardian', original/'bin/q4t-cuda-probe', None,
        out/'gateway-policy.json', out/'client-policy.json', out/'bundle',
        native_receipt=out/'native-build.json', native_ingress=original/'bin/q4t-ingress',
        capacity_config=dict(version=1), application_state=application,
        embedding_model_dir=Path(base['settings']['native_embedding']['directory']),
        recovery_profiles=args.registry, recovery_profile_policy=policy)
    engine.save(out/'receipt.json', receipt)
    qualification = engine.read(args.registry.parent.parent/'plan.json')
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope',
        'tools/examples','src/service','include/q4t/service'], cwd=ROOT,text=True).splitlines()
    source = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
    names.append(source)
    plan = dict(version=1, scenario='adaptive_profiles', limits=LIMITS,
        package=receipt, output=str(out), source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        sources={n:engine.sha(ROOT/n) for n in names},
        protected=[dict(path=str(ROOT/n),sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json')],
        pressure=PRESSURE, profile_policy=policy, requests=qualification['requests'],
        planned=dict(starts=3, self_checks=3, max_business_requests=9,
                     embedding_steps=60), default_replacement=False,
        sequence=['primary self-check/schema/stored Chat/tool/Batch/beta quota',
            'real bounded RAM pressure until automatic admission closure',
            'release owned RAM; qualified light; exact original usage and resources',
            'actual tool result and native embedding; no client or tool replay',
            'continuous high water plus 60s residency; automatic primary; JSON SSE',
            'durable stop; owned tree/ports/lease cleanup'],
        validation_wrappers=['count each model spawn and self-check before dispatch',
            'owned RAM helper only; never replace production memory observations'],
        automatic_request_or_tool_replay=False)
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), limits=LIMITS)))


def launcher(plan, plan_path):
    _, _, _, _, service, engine = LEGACY['load_modules'](plan)
    LEGACY['verify'](plan)
    original = subprocess.Popen
    guardian = str(Path(plan['package']['package'])/'bin/q4t-guardian')
    def spawn(argv, **options):
        if argv[0] == guardian:
            path = Path(argv[argv.index('--config')+1]); value = engine.read(path)
            production = value['controller']
            value['controller'] = [sys.executable,'-I','-B',str(Path(__file__).resolve()),
                'controller','--plan',str(plan_path),'--config',production[-1]]
            engine.save(path,value)
            save(Path(plan['output'])/'execution/wrapper.json',
                 dict(production=production, validation=value['controller']))
        return original(argv, **options)
    subprocess.Popen = spawn
    receipt = plan['package']
    result = service.launch(receipt['package'], receipt['manifest_sha256'],
                            Path(plan['output'])/'execution/state')
    save(Path(plan['output'])/'execution/launcher-result.json',result)
    return int(result['guardian_exit_code'] != 0)


def controller_actor(plan, config):
    LEGACY['load_modules'](plan); LEGACY['verify'](plan)
    from tools.serve_recovery import controller, core_http
    out = Path(plan['output'])/'execution'
    budget = LEGACY['Budget'](out, limits=LIMITS)
    original_spawn, original_request = subprocess.Popen, core_http.request
    def spawn(argv, **options):
        if str(controller.ROOT/'tools/serve_recovery/child.py') in argv:
            budget.reserve('starts','managed-model')
        elif str(Path(plan['package']['package'])/'bin/q4t-cuda-probe') in argv:
            budget.reserve('probes','unexpected-device-probe')
        return original_spawn(argv, **options)
    def request(port, method, path, **options):
        if method == 'POST':
            number = budget.reserve('chats','managed-self-check')
            result = original_request(port,method,path,**options)
            save(out/f'self-check-{number}.json',result)
            return result
        return original_request(port,method,path,**options)
    subprocess.Popen, core_http.request = spawn, request
    sys.argv = [sys.argv[0],'--config',str(config)]
    controller.main()


class Stage(LEGACY['Stage']):
    def __init__(self, plan, path):
        super().__init__(plan,path)
        self.pressure_process = None
        self.release_fd = None
        self.registry = self.engine.read(self.package/'manifest.json')['recovery_profiles']

    def family(self, name):
        from tools.serve_recovery import lifecycle, profile_store, usage_store
        writer = profile_store.Store if name == 'profiles' else usage_store.Store
        end = time.monotonic()+5
        while True:
            try:
                return lifecycle.read_family(self.output/'state', dict(service_id=self.service_id), writer)
            except BlockingIOError:
                if time.monotonic() >= end: raise
                time.sleep(.025)

    def ready(self, generation, name):
        from tools.serve_recovery import ledger
        state = self.wait(lambda s:s['model_generation'] == generation and s['business_ready'])
        self.engine.require(state['model_capacity_sha256'] ==
            ledger.digest(self.registry['profiles'][name]), 'unqualified live model binding')
        save(self.output/f'ready-{generation}.json',state)
        usage = self.family('usage'); save(self.output/f'usage-{generation}.json',usage)
        self.engine.require(usage['phase'] == 'active' and usage['generation'] == generation and
            (generation == 1 or usage['source']['generation'] == generation-1),
            'profile switch lost original sealed usage')
        return state

    def request(self,label,body=None,*,client='alpha',path='/v1/models',method=None,
                expected=200,model_work=False,content_type='application/json',raw_result=False):
        self.budget.reserve('chats' if model_work else 'controls', label)
        raw = body if isinstance(body,bytes) else None if body is None else json.dumps(body).encode()
        if body is not None and not isinstance(body,bytes):save(self.output/(label+'-request.json'),body)
        connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=120)
        try:
            connection.request(method or ('GET' if body is None else 'POST'),path,raw,
                {'Authorization':'Bearer '+self.keys[client],'Content-Type':content_type})
            response = connection.getresponse(); wire = response.read(4*1024**2+1)
            self.engine.require(len(wire) <= 4*1024**2,'response bound')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'),dict(status=response.status,
                                                       monotonic_ns=time.monotonic_ns()))
            self.engine.require(response.status == expected,'unexpected HTTP status: '+label)
            return wire if raw_result else json.loads(wire)
        finally:connection.close()

    def chat(self,label,body,**options):
        return self.request(label,body,path='/v1/chat/completions',model_work=True,**options)

    def retained(self,batch,output,stored):
        current = self.request('retained-batch',path='/v1/batches/'+batch['id'])
        self.engine.require(current == batch,'original Batch changed at profile switch')
        wire = self.request('retained-file',path='/v1/files/'+batch['output_file_id']+'/content',
                            raw_result=True)
        self.engine.require(wire == output,'original Batch file changed')
        value = self.request('retained-chat',path='/v1/chat/completions/'+stored['id'])
        self.engine.require(value['choices'] == stored['choices'],'stored Chat changed')

    def batch(self):
        line = json.dumps(dict(custom_id='qualified-vector',method='POST',url='/v1/embeddings',
                               body=self.plan['requests']['embedding'])).encode()+b'\n'
        boundary = 'qualified-profile-batch'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input.jsonl"\r\n'
            'Content-Type: application/jsonl\r\n\r\n').encode()+line+f'\r\n--{boundary}--\r\n'.encode()
        file = self.request('batch-file',body,path='/v1/files',
                            content_type='multipart/form-data; boundary='+boundary)
        batch = self.request('batch-create',dict(input_file_id=file['id'],endpoint='/v1/embeddings',
            completion_window='24h'),path='/v1/batches',model_work=True)
        end = time.monotonic()+30; index = 0
        while time.monotonic() < end:
            batch = self.request('batch-poll-'+str(index),path='/v1/batches/'+batch['id']);index += 1
            if batch['status'] == 'completed': break
            self.engine.require(batch['status'] not in ('failed','expired','cancelled'),'Batch failed')
            time.sleep(.25)
        self.engine.require(batch['status'] == 'completed','Batch did not complete')
        raw = self.request('batch-output',path='/v1/files/'+batch['output_file_id']+'/content',raw_result=True)
        row = json.loads(raw)
        self.engine.require(row['response']['status_code'] == 200 and row['error'] is None,
                            'Batch embedding failed')
        return batch,raw,row['response']['body']

    def start_pressure(self):
        source, release = os.pipe2(os.O_CLOEXEC)
        self.release_fd = release
        config = self.plan['pressure']
        with (self.output/'pressure.log').open('xb') as log:
            self.pressure_process = subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),
                'pressure','--output',str(self.output/'pressure.json'),'--release-fd',str(source),
                '--pressure-config',json.dumps(config)],pass_fds=(source,self.lease),stdout=log,stderr=log)
        os.close(source)

    def release_pressure(self):
        if self.release_fd is not None:
            os.close(self.release_fd);self.release_fd=None
        if self.pressure_process is not None:
            try:self.pressure_process.wait(10)
            except subprocess.TimeoutExpired:
                self.pressure_process.kill()
                self.pressure_process.wait(5)
                raise
            self.engine.require(self.pressure_process.returncode == 0,'RAM helper bound or failure')

    def exercise(self):
        from tools.serve_recovery import profile_evidence
        first = self.ready(1,'primary')
        schema_body = dict(self.plan['requests']['schema'],store=True,
                           metadata=dict(purpose='qualified-profile-continuity'))
        stored = self.chat('primary-schema',schema_body)
        self.engine.require(json.loads(stored['choices'][0]['message']['content']) == {'answer':42},'schema quality')
        call = self.chat('primary-tool',self.plan['requests']['tool'])
        message = profile_evidence.message(call,'tool_calls')
        calls = message['tool_calls']
        document = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
        self.engine.require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=document),'real tool arguments')
        source = dict(path=document,content=(ROOT/document).read_text())
        digest = self.engine.sha(ROOT/document)
        self.engine.require(digest == self.plan['sources'][document],'document changed')
        reply = dict(role='tool',tool_call_id=calls[0]['id'],content=json.dumps(dict(
            path=document,sha256=digest,answer=digest[:12])))
        save(self.output/'tool-execution.json',dict(executions=1,source=source,reply=reply))
        batch,output,vector = self.batch()
        beta = copy.deepcopy(self.plan['requests']['schema'])
        self.chat('beta-before-switch',beta,client='beta')
        self.chat('beta-quota-before',beta,client='beta',expected=429)
        beta_at = time.monotonic()
        self.start_pressure()
        closed = self.wait(lambda s:not s['business_ready'] and
            (s['controller_health'].get('reason') == 'qualified_profile_switch' or
             s['model_generation'] > 1 or s['model_pid'] is None),seconds=90)
        save(self.output/'pressure-transition.json',closed)
        # A GET checks closed ingress without accidentally queuing/replaying model work.
        self.request('recovering-models',expected=503)
        self.release_pressure()
        light = self.ready(2,'light')
        self.engine.require(all(first[k] == light[k] for k in
            ('guardian_pid','controller_pid','gateway_pid')),'profile change replaced control tree')
        elapsed = time.monotonic()-beta_at
        if elapsed < 59:
            self.chat('beta-quota-after',beta,client='beta',expected=429)
        save(self.output/'quota-window.json',dict(elapsed_seconds=elapsed,
            denial_checked=elapsed < 59, exact_durable_handoff=True))
        usage = self.family('usage')
        inherited = next(c for c in usage['snapshot']['clients'] if c['id'] == 'beta')
        self.engine.require(int(inherited['admitted']) >= 1 and int(inherited['rpm_rejected']) >= 1,
                            'actual prior client accounting lost')
        self.retained(batch,output,stored)
        follow = copy.deepcopy(self.plan['requests']['tool']);follow['tool_choice']='none'
        follow['messages'] += [message,reply]
        result = self.chat('light-tool-result',follow)
        embedded = self.request('light-embedding',self.plan['requests']['embedding'],
                               path='/v1/embeddings',model_work=True)
        profile_evidence.business(dict(schema=stored,tool_call=call,tool_result=result,
            tool_reply=reply,tool_source=source,embedding=embedded),self.registry['identity'])
        self.engine.require(embedded['data'] == vector['data'],'native vector changed on light profile')
        primary = self.ready(3,'primary')
        self.engine.require(all(first[k] == primary[k] for k in
            ('guardian_pid','controller_pid','gateway_pid')),'promotion replaced control tree')
        final = dict(self.plan['requests']['schema'],stream=True,stream_options=dict(include_usage=True))
        wire = self.chat('primary-final-sse',final,raw_result=True)
        from tools.examples.chat_client import read_sse
        answer = read_sse(io.BytesIO(wire))
        self.engine.require(answer.finish_reason == 'stop' and json.loads(answer.message['content']) ==
                            {'answer':42},'promoted actual SSE quality')
        profiles = self.family('profiles');save(self.output/'final-profiles.json',profiles)
        self.engine.require(profiles['selected'] == 'primary' and profiles['pending'] is None and
            [h['reason'] for h in profiles['history']] ==
                ['sustained_memory_pressure','sustained_memory_recovery'] and
            [h['source_generation'] for h in profiles['history']] == [1,2] and
            profiles['completed_generation'] == 3,'automatic profile history differs')
        save(self.output/'business-final.json',dict(status='PASS',profiles=profiles,
             no_tool_replay=True, original_batch=True, original_chat=True,
             original_client_accounting=True, control_tree_unchanged=True))

    def run(self):
        failure = cleanup_failure = None
        def expired(*_):raise TimeoutError('managed profile deadline')
        prior = signal.signal(signal.SIGALRM,expired)
        previous_term = signal.signal(signal.SIGTERM,expired)
        signal.setitimer(signal.ITIMER_REAL,LIMITS['seconds']-70)
        try:
            initialized = self.initialize_state();self.service_id=initialized['service_id']
            save(self.output/'initialized.json',initialized)
            with (self.output/'launcher.log').open('xb') as log:
                self.process = subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),
                    'launcher','--plan',str(self.plan_path)],stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,pass_fds=(self.lease,),start_new_session=True)
            self.exercise(); LEGACY['verify'](self.plan)
        except BaseException:failure=traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:self.release_pressure()
            except BaseException:cleanup_failure=traceback.format_exc()
            try:self.stop()
            except BaseException:cleanup_failure=(cleanup_failure or '')+traceback.format_exc()
            signal.signal(signal.SIGALRM,prior);signal.signal(signal.SIGTERM,previous_term)
            result = dict(status='PASS_MANAGED_ADAPTIVE_PROFILES' if not failure and not cleanup_failure
                else 'FAILED',failure=failure,cleanup_failure=cleanup_failure,
                seconds=time.monotonic()-self.started,budget=self.engine.read(self.output/'counts.json'),
                models=self.models,package=self.plan['package'],default_replaced=False,
                performance_acceptance=False)
            save(self.output/'result.json',result)
            print(json.dumps(result),flush=True)
        return int(failure is not None or cleanup_failure is not None)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','run','launcher','controller','pressure'])
    for name in ('package','registry','output','plan','config'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--port',type=int,default=19071)
    parser.add_argument('--release-fd',type=int)
    parser.add_argument('--pressure-config')
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args);return 0
    if args.mode=='pressure':return pressure(args.output,args.release_fd,json.loads(args.pressure_config))
    plan=json.loads(args.plan.read_text())
    if args.mode=='launcher':return launcher(plan,args.plan)
    if args.mode=='controller':controller_actor(plan,args.config);return 0
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan,args.plan).run()


if __name__=='__main__':raise SystemExit(main())
