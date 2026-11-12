"""Targeted async continuation and late-result delivery: exact retained payload then real fallback.

Each stage owns a continuous strict lease. The first must finalize after lease
release before its exact package may qualify as the second stage's fallback.
Production actors stay sealed; wrappers count launches and inject one owned
gateway SIGABRT only in the explicit version stage.
"""
import argparse
import copy
import fcntl
import io
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
sys.path.insert(0, str(ROOT))
VERSION = runpy.run_path(str(ROOT/'tools/serve_recovery/version_acceptance.py'))
MANAGED, LEGACY = VERSION['MANAGED'], VERSION['LEGACY']
LIMITS = {'tool_retained': dict(starts=1, chats=2, probes=0, controls=1500, seconds=300),
          'tool_version': dict(starts=2, chats=4, probes=1, controls=2500, seconds=600)}
PREVIOUS_LIMITS = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS[name] if name in LIMITS else PREVIOUS_LIMITS(name))
for function in (VERSION['launcher'], MANAGED['launcher']):
    function.__globals__['__file__'] = __file__
save = LEGACY['save']
def requests():
    from tools.serve_responses.async_acceptance import requests as direct
    rows = direct()
    for key in ('tool', 'follow'):
        rows[key]['max_output_tokens'] = 256
        rows[key]['stream'] = False
        rows[key].pop('stream_options', None)
    return {key: rows[key] for key in ('tool', 'follow')}


def parse_response(raw, stream):
    from tools.serve_responses.tool_identity_acceptance import check_shape
    if stream:
        events = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith('data: {')]
        if not events or events[-1]['type'] != 'response.completed':
            raise RuntimeError('response did not complete')
        if [e['sequence_number'] for e in events] != list(range(len(events))):
            raise RuntimeError('stream sequence differs')
        for e in events:
            check_shape(e, 'response_event')
        value = events[-1]['response']
    else:
        value = json.loads(raw)
    check_shape(value, 'response')
    if value['status'] != 'completed' or b'q4t_ns_' in raw:
        raise RuntimeError('incomplete or exposed private alias')
    return value


def check_call(value):
    calls = [i for i in value['output'] if i['type'] == 'function_call']
    if len(calls) != 1 or calls[0]['namespace'] != 'project' or calls[0]['name'] != 'read_project_record':
        raise RuntimeError('namespace call identity differs')
    from tools.serve_responses.acceptance import text
    if calls[0].get('async') is not True or calls[0].get('caller') != dict(type='direct') or 'large language model' not in text(value).lower():
        raise RuntimeError('async dispatch or independent answer missing')
    args = json.loads(calls[0]['arguments'])
    if args != dict(path='docs/MODEL.md', start_line=1, line_count=3):
        raise RuntimeError('actual tool arguments differ')
    return calls[0], args


def check_stored(created, stored):
    # GET without include omits the optional empty logprobs create field.
    # All call identities, result contents and lifecycle fields stay exact.
    parse_response(json.dumps(stored).encode(), False)
    expected = copy.deepcopy(created['output'])
    for item in expected:
        for part in item.get('content', []):
            if part.get('type') == 'output_text':
                if part.pop('logprobs', []) != []:
                    raise RuntimeError('unexpected nonempty probability data')
    if stored['output'] != expected:
        raise RuntimeError('stored identity or content differs beyond projection')


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
    engine.require(manifest['ingress_mode'] == 'native' and not manifest['tls'] and
        policy['listen_host'] == '127.0.0.1' and manifest.get('recovery_profiles'),
        'exact native loopback qualified payload required')
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_host',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope',
        'tools/examples','tools/release','src/service','include/q4t/service'], cwd=ROOT,text=True).splitlines()
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
        scope='Exact retained-control/new-engine async tool service, then native whole-tree fallback preserving invocation-time result schema and actual document closure.')
    if args.scenario == 'tool_version':
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
    for name in [str(Path(__file__).resolve().relative_to(ROOT)), 'docs/MODEL.md', *subprocess.check_output(['git','ls-files','tools/serve_responses'], cwd=ROOT,text=True).splitlines()]:
        plan['sources'][name] = engine.sha(ROOT/name)
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=plan['limits'])))


class Stage(VERSION['Stage']):
    def initialize_state(self):
        if self.plan['scenario'] == 'tool_retained':
            return LEGACY['Stage'].initialize_state(self)
        return super().initialize_state()

    def request(self, label, body=None, *, client='alpha', path='/v1/models',
                method=None, expected=200, model_work=False, raw_result=False):
        import http.client
        time.sleep(.22)
        self.budget.reserve('chats' if model_work else 'controls', label)
        raw = None if body is None else json.dumps(body).encode()
        if body is not None:
            save(self.output/(label+'-request.json'),body)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=120)
        try:
            connection.request(method or ('GET' if body is None else 'POST'),path,raw,
                {'Authorization':'Bearer '+self.keys[client], 'Content-Type':'application/json'})
            response = connection.getresponse(); wire = response.read(4*1024**2+1)
            self.engine.require(len(wire) <= 4*1024**2, 'response byte bound')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'), dict(status=response.status))
            self.engine.require(response.status == expected, 'HTTP status differs: '+label)
            return wire if raw_result else json.loads(wire)
        finally:
            connection.close()

    def exercise(self):
        from tools.serve_responses import acceptance as foreground
        version = self.plan['scenario'] == 'tool_version'
        first = self.version_ready(1,'primary') if version else self.ready(1,'primary')
        body = copy.deepcopy(self.plan['requests']['tool'])
        if version:
            body.update(stream=True,stream_options=dict(include_obfuscation=False))
        tool = parse_response(self.request('namespace-call', body, path='/v1/responses',
            model_work=True, raw_result=True), version)
        call, args = check_call(tool)
        saved = self.request('stored-call', path='/v1/responses/'+tool['id'])
        check_stored(tool, saved)
        caps = self.request('namespace-capabilities', path='/v1/capabilities')
        self.engine.require(caps['responses']['async_tools']['same_response_continuation'] and
            caps['responses']['tool_identity']['namespaces'] and
            caps['responses']['tool_results']['binding'] == 'invocation_time' and
            caps['embeddings']['native_backend']['enabled'], 'new or inherited capability missing')
        if version:
            before = self.family('profiles'); save(self.output/'profiles-before.json',before)
            self.fatal_gateway(first)
            second = self.version_ready(2,'fallback')
            self.engine.require(first['guardian_pid'] == second['guardian_pid'] and
                all(first[role+'_pid'] != second[role+'_pid'] and
                    first[role+'_generation'] < second[role+'_generation']
                    for role in ('controller','model','gateway')), 'whole payload was not replaced')
            self.engine.require(second['version_recovery']['probe_count'] == 1 and
                second['version_recovery']['last_fault']['signal'] == 6, 'fault/device proof differs')
            restored = self.request('restored-call',path='/v1/responses/'+tool['id'])
            self.engine.require(restored['output'] == saved['output'], 'fallback lost namespace call')
            self.engine.require(self.request('fallback-capabilities',path='/v1/capabilities') == caps,
                                'fallback changed actual capabilities')
        follow = copy.deepcopy(self.plan['requests']['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=call['call_id'], output='{"ok":false,"source":{}}')])
        rejected = self.request('invalid-result', follow, path='/v1/responses', expected=400)
        self.engine.require(rejected['error']['code'] == 'invalid_tool_output' and
            rejected['error']['param'].endswith('.output'), 'invocation contract was bypassed')
        if version:
            result = foreground.base.ProjectRecords(ROOT).read(args)
            self.engine.require(result['ok'] and result['source']['sha256'] ==
                self.plan['sources']['docs/MODEL.md'], 'actual document identity changed')
            save(self.output/'tool-execution.json', dict(call=call,result=result,executions=1))
            follow['input'][0]['output'] = json.dumps(result, ensure_ascii=False)
            follow.update(stream=True,stream_options=dict(include_obfuscation=False))
            answer = parse_response(self.request('fallback-follow', follow,
                path='/v1/responses', model_work=True, raw_result=True), True)
            value = json.loads(foreground.text(answer))
            title = result['source']['text'].splitlines()[0].split(': ',1)[1]
            self.engine.require(value['title'] in (title,title.lstrip('# ')) and
                value['citation'] == result['source']['citation'], 'actual document closure differs')
            after = self.family('profiles'); save(self.output/'profiles-after.json',after)
            self.engine.require(all(before[k] == after[k] for k in
                ('history','pending','selected','not_before_ns','light_since_ns')),
                'fallback changed profile state')
        save(self.output/'business-final.json', dict(status='PASS',actual_tool_identity=True, actual_async_continuation=True,
            stored_contract=True,version_switch=version,default_replaced=False))

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
            result=dict(status='PASS_BOUNDED_FEATURE_SERVICE' if not failure and not cleanup_failure
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
    require(result['status']=='PASS_BOUNDED_FEATURE_SERVICE' and
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
    engine.require(business==dict(status='PASS',actual_tool_identity=True, actual_async_continuation=True,stored_contract=True,
        version_switch=plan['scenario']=='tool_version',default_replaced=False),
        'business completion record differs')
    acceptance='ACCEPT_TOOL_IDENTITY_'+('VERSION' if plan['scenario']=='tool_version' else 'RETAINED')
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


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','run','finalize','launcher','controller','gateway','probe'))
    parser.add_argument('--scenario',choices=tuple(LIMITS),default='tool_retained')
    for name in ('package','output','plan','config','retained','release','decision'):
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
        function=VERSION['launcher'] if plan['scenario']=='tool_version' else MANAGED['launcher']
        raise SystemExit(function(plan,args.plan.resolve()))
    if args.mode == 'probe': VERSION['probe'](plan)
    elif plan['scenario']=='tool_version': VERSION['actor'](plan,args.payload,args.mode,args.config)
    else: MANAGED['controller_actor'](plan,args.config)


if __name__ == '__main__': main()
