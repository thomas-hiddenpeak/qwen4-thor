"""Native retrieval delivery: retained payload, real fallback, and optional native TLS.

Each stage owns a continuous strict lease. The first must finalize after lease
release before its exact package may qualify as the second stage's fallback.
Production actors stay sealed; wrappers count launches and inject one owned
gateway SIGABRT only in the explicit version stage.
"""
import argparse
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import runpy
import signal
import socket
import subprocess
import sys
import time
import traceback
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
VERSION = runpy.run_path(str(ROOT/'tools/serve_recovery/version_acceptance.py'))
MANAGED, LEGACY = VERSION['MANAGED'], VERSION['LEGACY']
LIMITS = {'retrieval_retained': dict(starts=1, chats=2, probes=0, controls=1500, seconds=300),
          'retrieval_version': dict(starts=2, chats=4, probes=1, controls=2500, seconds=600),
          'retrieval_tls': dict(starts=1, chats=2, probes=0, controls=1500, seconds=300)}
PREVIOUS_LIMITS = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS[name] if name in LIMITS else PREVIOUS_LIMITS(name))
for function in (VERSION['launcher'], MANAGED['launcher']):
    function.__globals__['__file__'] = __file__
save = LEGACY['save']
DOCUMENT = 'docs/SSD_OFFLOAD_CONTRACT.md'
PYTHON_SDK = ROOT/'.q4t-work/application-service-20261102/sdk-env/bin/python'


def requests():
    return dict(search=dict(model='qwen3.8-flash-next', temperature=0, store=True,
        reasoning=dict(effort='none'), max_output_tokens=256,
        max_tool_calls=1, parallel_tool_calls=False,
        tools=[dict(type='file_search', vector_store_ids=['vs_actual'], max_num_results=1)],
        tool_choice=dict(type='file_search'), include=['file_search_call.results'],
        instructions='Use file_search with exactly one query. Answer from the actual '
            'returned source in at most two sentences. Copy its exact citation_marker '
            'into your answer. Do not invent file IDs.',
        input='According to this project, where may MoE expert capacity be '
              'offloaded, and why is moving it to CPU memory insufficient? '
              'Include the terms SSD and DRAM.'),
        follow=dict(model='qwen3.8-flash-next', temperature=0, store=True,
            reasoning=dict(effort='none'), max_output_tokens=128,
            input='Using the previous source, repeat the permitted offload target '
                  'and shared memory type. Include SSD and DRAM.'))


def check_business(value, *, version, tls):
    from tools.serve_responses import file_search_acceptance as direct
    direct.require(value['status'] == 'PASS' and value['version_switch'] is version and
        value['native_tls'] is tls and value['default_replaced'] is False,
        'incomplete retrieval business')
    direct.check_answer(value['response'], value['file_id'], included=True)
    def output(response):
        # Retrieval omits unrequested logprobs; empty generation logprobs and
        # that projection carry the same content. Nonempty probabilities are
        # never discarded or compared as if they had been returned.
        items = copy.deepcopy(response['output'])
        for item in items:
            for part in item.get('content', []):
                if part.get('logprobs') == []:
                    del part['logprobs']
        return items
    direct.require(output(value['retained_response']) == output(value['response']) and
        value['retained_response']['id'] == value['response']['id'] and
        value['indexed_text'] == value['source_text'], 'retrieval state or source changed')
    if version:
        direct.require(output(value['restored_response']) == output(value['response']) and
            value['restored_response']['id'] == value['response']['id'], 'fallback lost response')
        follow = value['follow']
        direct.require(follow['status'] == 'completed' and
            not any(item['type'] == 'file_search_call' for item in follow['output']),
            'fallback repeated a search')
        text = direct.foreground.text(follow)
        direct.require('SSD' in text and 'DRAM' in text, 'fallback lost actual retrieval context')
    if tls:
        from tools.serve_responses import sdk_capture
        sdk = value['sdk']
        sdk_capture.validate(sdk)
        direct.require(sdk['version'] == '3.42.1' and type(sdk['requests']) is int and
            sdk['requests'] == 1 and
            sdk['response'] == value['response'] and sdk['events'], 'native TLS SDK incomplete')
        for event in sdk['events']:
            direct.require(not direct.errors(event, 'response_event'), 'SDK event schema differs')
        direct.require(sdk['events'][-1]['type'] == 'response.completed' and
            [v['sequence_number'] for v in sdk['events']] == list(range(len(sdk['events']))),
            'SDK sequence incomplete')


def prepare(args):
    bundle, _, _, _, _, engine = LEGACY['load_modules']()
    engine.require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
                   'commit reviewed source before freezing')
    out = args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work'), 'private output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    package = args.package.resolve(strict=True)
    digest = engine.sha(package/'manifest.json'); manifest = bundle.verify(package, digest)
    policy = engine.read(package/'gateway-policy.json')
    engine.require(manifest['ingress_mode'] == 'native' and
        bool(manifest['tls']) == (args.scenario == 'retrieval_tls') and
        policy['listen_host'] == '127.0.0.1' and manifest.get('recovery_profiles'),
        'exact qualified native payload/TLS mode required')
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_host',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope',
        'tools/examples','tools/release','tools/serve_responses','src/service','include/q4t/service',
        'docs/serve-application-v1.schema.json'], cwd=ROOT,text=True).splitlines()
    plan = dict(version=1, scenario=args.scenario, limits=LIMITS[args.scenario],
        package=dict(package=str(package), manifest_sha256=digest), output=str(out),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n),sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json')],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        command=[sys.executable,'-B',str(Path(__file__).resolve()),'run','--plan',str(out/'plan.json')],
        requests=requests(), default_replacement=False, automatic_request_replay=False,
        planned=dict(starts=LIMITS[args.scenario]['starts'],
            self_checks=LIMITS[args.scenario]['starts'], user_generations=LIMITS[args.scenario]['starts']),
        scope='Exact managed native retrieval; optional TLS SDK or actual whole-tree fallback preserving files/index/response/history.')
    plan['source_document'] = dict(path=DOCUMENT, sha256=engine.sha(ROOT/DOCUMENT),
        text='\n'.join((ROOT/DOCUMENT).read_text().splitlines()[:28])+'\n')
    plan['sources'][DOCUMENT] = plan['source_document']['sha256']
    plan['planned']['primary_generation_steps'] = 3 if args.scenario == 'retrieval_version' else 2
    plan['planned']['native_ingestion_inputs'] = 1
    plan['planned']['native_query_inputs'] = 1
    if args.scenario == 'retrieval_tls':
        engine.require(args.tls_ca is not None, 'private TLS CA required')
        ca = args.tls_ca.resolve(strict=True)
        engine.require(ca.is_relative_to(ROOT/'.q4t-work'), 'private test CA required')
        plan['tls_inputs'] = dict(ca=dict(path=str(ca),sha256=engine.sha(ca)))
        for key in ('certificate','key'):
            path = Path(manifest['tls_configuration'][key]).resolve(strict=True)
            engine.require(path.is_relative_to(ROOT/'.q4t-work'), 'private TLS material required')
            plan['tls_inputs'][key] = dict(path=str(path),sha256=engine.sha(path))
        plan['python_sdk'] = dict(executable=str(PYTHON_SDK.absolute()),
            executable_sha256=engine.sha(PYTHON_SDK), version='3.42.1')
    if args.scenario == 'retrieval_version':
        from tools.serve_recovery import version_contract
        engine.require(args.retained and args.release and args.decision,
                       'finalized exact retained acceptance required')
        retained = args.retained.resolve(strict=True)
        acceptance = {name:dict(path=str(path.resolve(strict=True)),sha256=engine.sha(path))
            for name,path in [('release',args.release),('decision',args.decision)]}
        registration = version_contract.prepare(plan['package'], plan['package'],
            dict(package=str(retained), manifest_sha256=engine.sha(retained/'manifest.json')),
            str(uuid.uuid4()), fallback_acceptance=acceptance)
        plan['registration'] = registration
        helper = out/'counted-cuda-probe'
        helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
            str(Path(__file__).resolve()),'probe','--plan',str(out/'plan.json')])+
            '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        helper.chmod(0o500)
        plan['probe_wrapper'] = dict(path=str(helper),sha256=engine.sha(helper))
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=plan['limits'])))


class Stage(VERSION['Stage']):
    def initialize_state(self):
        if self.plan['scenario'] != 'retrieval_version':
            return LEGACY['Stage'].initialize_state(self)
        return super().initialize_state()

    def request(self, label, body=None, *, client='alpha', path='/v1/models',
                method=None, expected=200, model_work=False,
                content_type='application/json', raw_result=False):
        # Stay inside the original unchanged five-RPS client policy.
        time.sleep(.22)
        self.budget.reserve('chats' if model_work else 'controls', label)
        raw = body if isinstance(body,bytes) else None if body is None else json.dumps(body).encode()
        if body is not None and not isinstance(body,bytes):
            save(self.output/(label+'-request.json'),body)
        if self.plan['scenario'] == 'retrieval_tls':
            import ssl
            context = ssl.create_default_context(cafile=self.plan['tls_inputs']['ca']['path'])
            connection = http.client.HTTPSConnection('127.0.0.1',self.port,
                                                     timeout=120,context=context)
        else:
            connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=120)
        try:
            connection.request(method or ('GET' if body is None else 'POST'),path,raw,
                {'Authorization':'Bearer '+self.keys[client],'Content-Type':content_type})
            response=connection.getresponse(); wire=response.read(4*1024**2+1)
            self.engine.require(len(wire) <= 4*1024**2,'response bound')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'),dict(status=response.status,
                monotonic_ns=time.monotonic_ns()))
            self.engine.require(response.status == expected,'unexpected HTTP status: '+label)
            return wire if raw_result else json.loads(wire)
        finally:connection.close()

    def sources(self):
        source=self.plan['source_document']
        self.engine.require(self.engine.sha(ROOT/source['path']) == source['sha256'],
                            'actual source changed')
        boundary='q4t-actual-retrieval'
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="SSD_OFFLOAD_CONTRACT.md"\r\n'
            'Content-Type: text/plain; charset=utf-8\r\n\r\n').encode()+source['text'].encode()+f'\r\n--{boundary}--\r\n'.encode()
        file=self.request('project-file',body,path='/v1/files',
                          content_type='multipart/form-data; boundary='+boundary)
        store=self.request('project-store',dict(name='actual-project-record',file_ids=[file['id']],
            chunking_strategy=dict(type='static',static=dict(max_chunk_size_tokens=4096,
                                                            chunk_overlap_tokens=0))),
            path='/v1/vector_stores')
        path='/v1/vector_stores/'+store['id']+'/files/'+file['id']
        for i in range(30):
            current=self.request('index-'+str(i),path=path)
            if current['status'] == 'completed':break
            self.engine.require(current['status'] == 'in_progress','source ingestion failed')
            time.sleep(1)
        else:raise TimeoutError('actual source index deadline')
        indexed=self.request('indexed-source',path=path+'/content')
        text=''.join(row['text'] for row in indexed['data'])
        self.engine.require(text == source['text'],'actual index changed the document')
        self.request('other-owner-denied',client='beta',path='/v1/vector_stores/'+store['id'],expected=404)
        return file,store,text

    def sdk_search(self, request):
        self.budget.reserve('chats','native-tls-sdk-search')
        config=dict(base_url='https://127.0.0.1:'+str(self.port)+'/v1',
            key=self.keys['alpha'],ca=self.plan['tls_inputs']['ca']['path'],request=request)
        sdk=self.plan['python_sdk']
        self.engine.require(self.engine.sha(sdk['executable']) == sdk['executable_sha256'],
                            'SDK interpreter changed')
        result=subprocess.run([sdk['executable'],'-I','-B',str(Path(__file__).resolve()),'sdk'],
            input=json.dumps(config),capture_output=True,text=True,timeout=140)
        (self.output/'sdk.stderr').write_text(result.stderr)
        (self.output/'sdk.stdout').write_text(result.stdout)
        self.engine.require(result.returncode == 0 and not result.stderr,'official SDK failure')
        value=json.loads(result.stdout); save(self.output/'sdk.json',value)
        return value

    def exercise(self):
        from tools.serve_responses import file_search_acceptance as direct
        version=self.plan['scenario'] == 'retrieval_version'
        tls=self.plan['scenario'] == 'retrieval_tls'
        first=self.version_ready(1,'primary') if version else self.ready(1,'primary')
        file,store,indexed=self.sources()
        request=copy.deepcopy(self.plan['requests']['search'])
        request['tools'][0]['vector_store_ids']=[store['id']]
        sdk=None
        if version or tls:
            request.update(stream=True,stream_options=dict(include_obfuscation=False))
        if tls:
            sdk=self.sdk_search(request); response=sdk['response']
        elif version:
            wire=self.request('native-search',request,path='/v1/responses',model_work=True,raw_result=True)
            events=[json.loads(line[6:]) for line in wire.decode().splitlines() if line.startswith('data: {')]
            self.engine.require(events and events[-1]['type']=='response.completed','retrieval SSE incomplete')
            for event in events:self.engine.require(not direct.errors(event,'response_event'),'event schema differs')
            self.engine.require([e['sequence_number'] for e in events] == list(range(len(events))),
                                'retrieval sequence differs')
            response=events[-1]['response']
        else:
            response=self.request('native-search',request,path='/v1/responses',model_work=True)
        direct.check_answer(response,file['id'],included=True)
        response_path='/v1/responses/'+response['id']+'?include[]=file_search_call.results'
        retained=self.request('retained-search',path=response_path)
        caps=self.request('retrieval-capabilities',path='/v1/capabilities')
        self.engine.require(caps['responses']['file_search']['enabled'] and
            caps['vector_stores']['native_embedding_configured'],'native retrieval capability lost')
        result=dict(status='PASS',file_id=file['id'],store_id=store['id'],
            source_text=self.plan['source_document']['text'],indexed_text=indexed,
            response=response,retained_response=retained,version_switch=version,
            native_tls=tls,default_replaced=False,sdk=sdk)
        if version:
            before=self.family('profiles');save(self.output/'profiles-before.json',before)
            self.fatal_gateway(first)
            second=self.version_ready(2,'fallback')
            self.engine.require(first['guardian_pid']==second['guardian_pid'] and all(
                first[role+'_pid']!=second[role+'_pid'] and
                first[role+'_generation']<second[role+'_generation']
                for role in ('controller','model','gateway')),'payload tree did not change')
            self.engine.require(second['version_recovery']['probe_count']==1 and
                second['version_recovery']['last_fault']['signal']==6,'fault/device proof differs')
            result['restored_response']=self.request('restored-search',path=response_path)
            restored=self.request('restored-source',path='/v1/vector_stores/'+store['id']+
                                  '/files/'+file['id']+'/content')
            self.engine.require(''.join(row['text'] for row in restored['data'])==indexed,
                                'fallback lost actual indexed source')
            self.engine.require(self.request('fallback-capabilities',path='/v1/capabilities')==caps,
                                'fallback changed actual capabilities')
            follow=dict(self.plan['requests']['follow'],previous_response_id=response['id'])
            result['follow']=self.request('fallback-history',follow,path='/v1/responses',model_work=True)
            after=self.family('profiles');save(self.output/'profiles-after.json',after)
            self.engine.require(all(before[k]==after[k] for k in
                ('history','pending','selected','not_before_ns','light_since_ns')),
                'fallback changed original profile state')
        check_business(result,version=version,tls=tls)
        save(self.output/'business-final.json',result)

    def run(self):
        failure = cleanup_failure = None
        def expired(*_): raise TimeoutError('bounded feature service deadline')
        old_alarm = signal.signal(signal.SIGALRM,expired)
        old_term = signal.signal(signal.SIGTERM,expired)
        signal.setitimer(signal.ITIMER_REAL,self.plan['limits']['seconds']-70)
        try:
            initialized = self.initialize_state(); self.service_id=initialized['service_id']
            save(self.output/'initialized.json',initialized)
            with (self.output/'launcher.log').open('xb') as log:
                self.process = subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),
                    'launcher','--plan',str(self.plan_path)],stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,pass_fds=(self.lease,),start_new_session=True)
            self.exercise(); LEGACY['verify'](self.plan)
        except BaseException:
            failure=traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:self.stop()
            except BaseException:cleanup_failure=traceback.format_exc()
            signal.signal(signal.SIGALRM,old_alarm); signal.signal(signal.SIGTERM,old_term)
            result=dict(status='PASS_BOUNDED_RETRIEVAL_SERVICE' if not failure and not cleanup_failure
                else 'FAILED',failure=failure,cleanup_failure=cleanup_failure,
                seconds=time.monotonic()-self.started,budget=self.engine.read(self.output/'counts.json'),
                models=self.models,package=self.plan['package'],scenario=self.plan['scenario'],
                default_replaced=False,performance_acceptance=False)
            save(self.output/'result.json',result)
            print(json.dumps(result),flush=True)
        return int(failure is not None or cleanup_failure is not None)


def accepted_run(plan, result, guard, queries, events):
    """No acceptance for partial business, monitoring gaps or overspent bounds."""
    def require(condition, reason):
        if not condition:raise RuntimeError(reason)
    limits=LIMITS[plan['scenario']]
    require(plan['limits']==limits,'frozen bounds differ')
    require(result['status']=='PASS_BOUNDED_RETRIEVAL_SERVICE' and
        result['failure'] is None and result['cleanup_failure'] is None and
        result['package']==plan['package'] and result['scenario']==plan['scenario'] and
        0 < result['seconds'] < limits['seconds'],'managed business not accepted')
    counts=result['budget']['counts']
    require(set(counts)==set(limits)-{'seconds'} and all(type(v) is int and
        0 <= v <= limits[k] for k,v in counts.items()) and
        all(counts[k]==limits[k] for k in ('starts','chats','probes')),
        'actual managed work incomplete or exceeds frozen bounds')
    require(guard.get('status')=='COMPLETED' and guard.get('child_returncode')==0 and
        guard.get('compute_backend')=='nvml' and guard.get('lease_inherited_by_child') is True and
        guard.get('external_lease') is False and guard.get('command')==plan['command'],
        'strict managed environment not accepted')
    require(queries and all(q.get('status')=='OK' and q.get('returncode')==0 for q in queries),
        'missing or failed NVML observation')
    acquired=[e for e in events if e.get('event')=='lease_acquired']
    released=[e for e in events if e.get('event')=='lease_released']
    require(len(acquired)==len(released)==1 and released[0].get('status')=='COMPLETED' and
        acquired[0]['pid']==released[0]['pid'],'strict lease not completed')
    seconds=released[0]['monotonic']-acquired[0]['monotonic']
    require(0 < seconds < limits['seconds'],'strict lease duration exceeds frozen bound')
    return dict(strict_lease_seconds=seconds,nvml_queries=len(queries),env_abort=False)


def finalize(path):
    path=path.resolve(strict=True);plan=json.loads(path.read_bytes())
    bundle,_,_,journal,_,engine=LEGACY['load_modules'](plan)
    manifest=LEGACY['verify'](plan)
    out=Path(plan['output']);execution=out/'execution';lease=out/'lease'
    result=engine.read(execution/'result.json')
    environment=accepted_run(plan,result,engine.read(lease/'exit.json'),
        [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()],
        [json.loads(line) for line in (lease/'events.jsonl').read_text().splitlines()])
    engine.require(result['budget']==engine.read(execution/'counts.json'),'count receipt changed')
    stopped=engine.read(execution/'stop.json')
    ledger=engine.read(execution/'state/ledger.json')['state']
    initialized=engine.read(execution/'initialized.json')
    engine.require(stopped['cleanup_confirmed'] and stopped['stop_durable'] and
        ledger['desired']=='STOPPED' and all(v is None for v in ledger['active'].values()) and
        ledger['service_id']==initialized['service_id'],'durable stop not confirmed')
    identities={(int(v['pid']),int(v['start_ticks'])) for v in result['models'].values()}
    for line in (execution/'status.jsonl').read_text().splitlines():
        value=json.loads(line)
        for role in ('guardian','controller','model','gateway'):
            if value.get(role+'_pid') is not None:
                identities.add((value[role+'_pid'],value[role+'_start_ticks']))
    for pid,ticks in identities:
        try:engine.require(journal.process_start(pid)!=ticks,'owned process survived cleanup')
        except (FileNotFoundError,ProcessLookupError):pass
    policy=engine.read(Path(plan['package']['package'])/'gateway-policy.json')
    for port in (policy['listen_port'],policy['upstream_port'],policy['upstream_operations_port']):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',port))
    with Path(manifest['lock_path']).open('r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if 'registration' in plan:
        from tools.serve_recovery import version_contract
        version_contract.verify(plan['registration'])
    business=engine.read(execution/'business-final.json')
    check_business(business,version=plan['scenario']=='retrieval_version',
                   tls=plan['scenario']=='retrieval_tls')
    engine.require(business['source_text']==plan['source_document']['text'],
                   'recorded source differs from frozen document')
    acceptance='ACCEPT_NATIVE_RETRIEVAL_'+plan['scenario'].removeprefix('retrieval_').upper()
    package=Path(plan['package']['package'])
    binary_sha=engine.sha(package/'engine/q4t')
    release_sha=engine.sha(package/'engine/q4t.release.json')
    decision=dict(status='PASS_'+acceptance.removeprefix('ACCEPT_'),acceptance=acceptance,
        manifest_sha256=plan['package']['manifest_sha256'],
        engine_binary_sha256=binary_sha,
        actual_business=dict(status=acceptance,**environment,counts=result['budget']['counts'],
            source_commit=plan['source_commit'],plan_sha256=engine.sha(path),
            result_sha256=engine.sha(execution/'result.json'),lease_result_sha256=engine.sha(lease/'exit.json'),
            postflight=dict(sources_unchanged=True,owned_processes_gone=True,ports_free=True,
                            lock_released=True,default_unchanged=True)),
        default_replaced=False,performance_acceptance=False,long_stability_accepted=False)
    engine.require(not (out/'decision.json').exists() and not (out/'service-release.json').exists(),
                   'preserve existing acceptance record')
    save(out/'decision.json',decision)
    save(out/'service-release.json',dict(version=4,profile='q4t-service-recovery-v4',
        acceptance=acceptance,recovery_accepted=True,acceptance_record=str(out/'decision.json'),
        acceptance_sha256=engine.sha(out/'decision.json'),**plan['package'],
        engine_binary_sha256=binary_sha,
        engine_release_sha256=release_sha,ingress_mode='native',
        state=str(execution/'state'),service_id=initialized['service_id'],
        initial_desired='STOPPED',automatically_started=False,
        scope=plan['scope'],default_replaced=False))
    print(json.dumps(dict(acceptance=acceptance,**environment,counts=result['budget']['counts'])))


def sdk_child():
    sys.path.insert(0,str(ROOT))
    from tools.serve_responses.sdk_capture import stream_response
    print(json.dumps(stream_response(json.load(sys.stdin))))


def main():
    os.umask(0o077)
    if sys.argv[1:] == ['sdk']:
        sdk_child();return
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','run','finalize','launcher','controller','gateway','probe'))
    parser.add_argument('--scenario',choices=tuple(LIMITS),default='retrieval_retained')
    for name in ('package','output','plan','config','retained','release','decision','tls-ca'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--payload',choices=('primary','fallback'))
    args=parser.parse_args()
    if args.mode == 'prepare':
        prepare(args); return
    if args.mode == 'finalize':
        finalize(args.plan); return
    plan=json.loads(args.plan.read_text())
    if args.mode == 'run':
        LEGACY['load_modules'](plan)
        from tools.release.release import child_subreaper
        with child_subreaper():
            raise SystemExit(Stage(plan,args.plan.resolve()).run())
    for function in (VERSION['actor'],VERSION['probe'],MANAGED['controller_actor']):
        function.__globals__['LIMITS']=plan['limits']
    if args.mode == 'launcher':
        function=VERSION['launcher'] if plan['scenario']=='retrieval_version' else MANAGED['launcher']
        raise SystemExit(function(plan,args.plan.resolve()))
    if args.mode == 'probe': VERSION['probe'](plan)
    elif plan['scenario']=='retrieval_version': VERSION['actor'](plan,args.payload,args.mode,args.config)
    else: MANAGED['controller_actor'](plan,args.config)


if __name__ == '__main__': main()
