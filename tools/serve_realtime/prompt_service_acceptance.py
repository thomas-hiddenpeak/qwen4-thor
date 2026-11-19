"""Focused stored Prompt recovery; unchanged independent backends are reused.

The production payload makes every recovery decision. Only the owned fault
targets are signalled; each new SDK connection is explicit and never replays
an interrupted request. Original model quality is recorded separately.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import runpy
import ssl
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = runpy.run_path(str(ROOT/'tools/serve_responses/web_service_acceptance.py'))
GLOBALS = BASE['main'].__globals__
VERSION, MANAGED, LEGACY = BASE['VERSION'], BASE['MANAGED'], BASE['LEGACY']
require, save = BASE['require'], BASE['save']
LIMITS = dict(
    tool_retained=dict(starts=1, chats=3, probes=0, controls=1500,
                       websocket_sessions=1, seconds=300),
    tls=dict(starts=2, chats=9, probes=0, controls=2000,
             websocket_sessions=3, seconds=360),
    tls_controller=dict(starts=2, chats=6, probes=0, controls=2000,
                        websocket_sessions=2, seconds=360),
    tool_version=dict(starts=2, chats=6, probes=1, controls=2500,
                      websocket_sessions=2, seconds=540))


def check_prompt(value, reference, definition):
    updates = [e['session'] for e in value['events']
               if e.get('type') == 'session.updated']
    require(len(updates) == 1 and updates[0].get('prompt') == reference,
            'actual Prompt reference differs or was updated again')
    wanted = definition['request']
    require(all(updates[0].get(k) == wanted[k] for k in
        ('instructions', 'tools', 'max_output_tokens', 'output_modalities')),
        'resolved Prompt did not provide exact instructions/tools/budget')


def cold_controller_usage(usage, generation, deadline, now):
    require(usage['phase']=='active' and usage['generation']==generation and
        usage['source'] is None and usage['snapshot'] is None and
        type(deadline) in (int,float) and deadline > 0 and now >= deadline-.05,
        'controller crash needs actual full quota wait, not an invented handoff')
    return dict(mode='full_window_after_unsealed_controller_death',
                generation=generation,observed_deadline=deadline,checked_at=now)


def prepare(args):
    bundle, _, _, _, _, engine = LEGACY['load_modules']()
    from tools.serve_recovery import profile_evidence, profile_record
    from tools.serve_realtime import conversation_managed
    require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
            'commit reviewed source before freezing')
    out = args.output.resolve()
    require(out.is_relative_to(ROOT/'.q4t-work'), 'private recovery output required')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    pack = args.package.resolve(strict=True)
    digest = engine.sha(pack/'manifest.json'); manifest = bundle.verify(pack, digest)
    policy = engine.read(pack/'gateway-policy.json')
    require(manifest['ingress_mode'] == 'native' and
        bool(manifest['tls']) == args.scenario.startswith('tls') and
        policy['listen_host'] == '127.0.0.1', 'exact native loopback/TLS payload required')
    qualified = profile_record.read(pack/'profile-primary.json')
    require(profile_evidence.prompt_enabled(qualified['capabilities']),
            'qualified real Prompt execution required')
    conversation, sdk = conversation_managed.prepare(
        args.profile_reference, manifest, qualified, pack)
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery',
        'tools/serve_host','tools/serve_service','tools/serve_ops','tools/serve_gateway',
        'tools/evalscope','tools/examples','tools/release','tools/serve_responses',
        'tools/serve_realtime','src/service','include/q4t/service'],
        cwd=ROOT,text=True).splitlines()
    definition = engine.read(ROOT/'tools/serve_realtime/prompt_definitions.json')['tool']
    plan = dict(version=1, scenario=args.scenario, limits=LIMITS[args.scenario],
        package=dict(package=str(pack), manifest_sha256=digest), output=str(out),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n), sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json',
             'build/service.release.json','build/service-native-state/ledger.json')],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        command=[sys.executable,'-I','-B',str(Path(__file__).resolve()),'run',
                 '--plan',str(out/'plan.json')], requests={}, conversation=conversation,
        sdk=copy.deepcopy(sdk), prompt_recovery=dict(format='q4t-prompt-recovery-v1',
            definition=definition, registry=manifest['recovery_profiles']),
        default_replacement=False, automatic_request_replay=False, scope=__doc__)
    plan['sources'].update(conversation['files'])
    plan['sources'].update({str(Path(sdk['sdk_directory'])/n):h
                            for n,h in sdk['files'].items()})
    plan['sources'][sdk['node']['path']] = sdk['node']['sha256']
    # Keep one exact SDK inventory. Ordinary plan reading retains its 2 MiB cap.
    del plan['sdk']['files']
    plan['sdk']['source_inventory_count'] = len(sdk['files'])
    if args.scenario.startswith('tls'):
        plan['tls_inputs'] = {}
        for key in ('certificate','key'):
            path = Path(manifest['tls_configuration'][key]).resolve(strict=True)
            require(path.is_relative_to(out.parent), 'private TLS material required')
            plan['tls_inputs'][key] = dict(path=str(path),sha256=engine.sha(path))
        plan['tls_inputs']['ca'] = plan['tls_inputs']['certificate']
    if args.scenario == 'tool_version':
        from tools.serve_recovery import version_contract
        require(args.retained and args.release and args.decision,
                'exact retained real business must finalize before fallback')
        retained = args.retained.resolve(strict=True)
        accepted = {n:dict(path=str(p.resolve(strict=True)),sha256=engine.sha(p))
                    for n,p in [('release',args.release),('decision',args.decision)]}
        plan['registration'] = version_contract.prepare(plan['package'], plan['package'],
            dict(package=str(retained),manifest_sha256=engine.sha(retained/'manifest.json')),
            str(uuid.uuid4()),fallback_acceptance=accepted)
        helper = out/'counted-cuda-probe'
        helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
            str(Path(__file__).resolve()),'probe','--plan',str(out/'plan.json')])+
            '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
        helper.chmod(0o500)
        plan['probe_wrapper'] = dict(path=str(helper),sha256=engine.sha(helper))
    save(out/'plan.json',plan)
    require(engine.read(out/'plan.json') == plan, 'bounded plan did not round trip')
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=plan['limits'])))


def prompt_actor(stage):
    from tools.serve_realtime import conversation_managed
    class Actor(conversation_managed.Actor):
        def client_arguments(self, args):
            return dict(args, prompt_reference=self.stage.prompt_reference)

        def verify_result(self, value, mode):
            super().verify_result(value,mode)
            check_prompt(value,self.stage.prompt_reference,
                         self.stage.plan['prompt_recovery']['definition'])
    return Actor(stage)


class Stage(BASE['Stage']):
    def initialize_state(self):
        return VERSION['Stage'].initialize_state(self) if self.plan['scenario'] == 'tool_version' \
            else LEGACY['Stage'].initialize_state(self)

    def request(self,label,body=None,*,client='alpha',path='/v1/models',
                method=None,expected=200,model_work=False,raw_result=False):
        require(not model_work, 'model work uses the counted SDK only')
        time.sleep(.22);self.budget.reserve('controls',label)
        raw = None if body is None else json.dumps(body).encode()
        if body is not None:save(self.output/(label+'-request.json'),body)
        if self.plan['scenario'].startswith('tls'):
            context = ssl.create_default_context(cafile=self.plan['tls_inputs']['ca']['path'])
            connection = http.client.HTTPSConnection('127.0.0.1',self.port,timeout=30,context=context)
        else:connection = http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        try:
            connection.request(method or ('GET' if body is None else 'POST'),path,raw,
                {'Authorization':'Bearer '+self.keys[client],'Content-Type':'application/json'})
            response=connection.getresponse();wire=response.read(4*1024**2+1)
            require(len(wire)<=4*1024**2,'control response byte bound')
            (self.output/(label+'.wire')).write_bytes(wire)
            save(self.output/(label+'-http.json'),dict(status=response.status))
            require(response.status==expected,'control HTTP differs: '+label)
            return wire if raw_result else json.loads(wire)
        finally:connection.close()

    def retained_prompt(self,label):
        current=self.request(label,path='/v1/prompts/'+self.prompt_reference['id'])
        require(current==self.prompt_baseline,'durable Prompt changed during recovery')

    def controller_ready(self):
        from tools.serve_recovery import ledger
        # Hard controller death has no sealed handoff. The existing generic
        # recovery gate requires an observed full quota window before READY.
        ready=LEGACY['Stage'].ready(self,2)
        require(ready['model_capacity_sha256']==ledger.digest(
            self.registry['profiles']['primary']),'unqualified recovered model')
        usage=self.family('usage')
        checked=cold_controller_usage(usage,2,self.quota_deadlines.get(2),time.monotonic())
        save(self.output/'controller-usage.json',dict(usage=usage,verification=checked))
        return ready

    def recover_controller_prompt(self,previous,actor,label):
        self.kill(previous,'controller','prompt-controller-exit')
        current=self.controller_ready()
        require(current['controller_generation']==previous['controller_generation']+1 and
            current['model_pid']!=previous['model_pid'] and all(current[k]==previous[k]
                for k in ('guardian_pid','gateway_pid','gateway_start_ticks')),
            'controller death did not recover its owned model exactly once')
        self.retained_prompt('prompt-after-controller');actor.start(label)
        save(self.output/'controller-continuity.json',dict(before=previous,after=current))

    def exercise(self):
        version=self.plan['scenario']=='tool_version';tls=self.plan['scenario']=='tls'
        controller_only=self.plan['scenario']=='tls_controller'
        first=self.version_ready(1,'primary') if version else self.ready(1,'primary')
        self.request('capabilities',path='/v1/capabilities')
        definition=self.plan['prompt_recovery']['definition']
        created=self.request('prompt-created',dict(definition=definition),path='/v1/prompts')
        self.prompt_reference=dict(id=created['id'],version='1',variables={})
        save(self.output/'prompt-reference.json',self.prompt_reference)
        self.prompt_baseline=self.request('prompt-before',path='/v1/prompts/'+created['id'])
        self.request('prompt-other-client',client='beta',path='/v1/prompts/'+created['id'],expected=404)
        actor=prompt_actor(self)
        try:
            actor.start('controller-before' if controller_only else 'tls-before' if tls else 'primary')
            if controller_only:
                self.recover_controller_prompt(first,actor,'controller-after')
            elif tls:
                self.kill(first,'gateway','prompt-gateway-exit');actor.finish()
                second=self.wait(lambda s:s['business_ready'] and
                    s['gateway_generation']==first['gateway_generation']+1)
                require(all(second[k]==first[k] for k in ('guardian_pid','model_pid',
                    'model_start_ticks','model_generation','controller_pid','controller_start_ticks')),
                    'gateway recovery changed healthy core')
                self.retained_prompt('prompt-after-gateway');actor.start('tls-after')
                self.recover_controller_prompt(second,actor,'tls-controller')
                save(self.output/'gateway-continuity.json',dict(before=first,after=second))
            elif version:
                before=self.family('profiles');save(self.output/'profiles-before.json',before)
                self.fatal_gateway(first);actor.finish()
                second=self.version_ready(2,'fallback')
                require(first['guardian_pid']==second['guardian_pid'] and all(
                    first[r+'_pid']!=second[r+'_pid'] and
                    first[r+'_generation']<second[r+'_generation']
                    for r in ('controller','model','gateway')),'whole payload not replaced')
                require(second['version_recovery']['probe_count']==1 and
                    second['version_recovery']['last_fault']['signal']==6,'actual device/fault proof differs')
                self.retained_prompt('prompt-after-fallback');actor.start('fallback')
                after=self.family('profiles');save(self.output/'profiles-after.json',after)
                require(all(before[k]==after[k] for k in
                    ('history','pending','selected','not_before_ns','light_since_ns')),
                    'fallback changed profile selection')
                save(self.output/'version-continuity.json',dict(before=first,after=second))
            actor.finish_phases()
        finally:actor.close()
        self.request('capabilities-after',path='/v1/capabilities')
        save(self.output/'media-usage.json',check_business(self.output))
        save(self.output/'business-final.json',business_record(version))


def check_business(out):
    from tools.serve_service import package as engine
    from tools.serve_realtime import conversation_managed
    out=Path(out);plan=engine.read(out.parent/'plan.json')
    caps=json.loads((out/'capabilities.wire').read_bytes())
    require(caps==json.loads((out/'capabilities-after.wire').read_bytes()),
            'actual declared capabilities changed')
    reference=engine.read(out/'prompt-reference.json')
    before=json.loads((out/'prompt-before.wire').read_bytes())
    require(reference==dict(id=before['id'],version='1',variables={}) and
        engine.read(out/'prompt-other-client-http.json')['status']==404,
        'Prompt identity/client isolation differs')
    rows=conversation_managed.check(out,plan,{},caps)
    for label,_ in conversation_managed.phases(plan):
        check_prompt(engine.read(out/(label+'-conversation.sdk.json')),reference,
                     plan['prompt_recovery']['definition'])
    paths={'tool_retained':[], 'tls':['prompt-after-gateway','prompt-after-controller'],
           'tls_controller':['prompt-after-controller'],
           'tool_version':['prompt-after-fallback']}[plan['scenario']]
    for name in paths:
        require(json.loads((out/(name+'.wire')).read_bytes())==before,
                'saved Prompt differs after recovery')
    if plan['scenario'] in ('tls','tls_controller'):
        row=engine.read(out/'controller-usage.json');v=row['verification']
        require(cold_controller_usage(row['usage'],2,v['observed_deadline'],v['checked_at'])==v,
                'cold controller quota receipt differs')
    return dict(conversations=rows,prompt_reference=reference,persistent_phases=paths,
                model_quality_separate=True,automatic_replay=False)


def business_record(version):
    return dict(status='PASS',prompt_reference=True,persistent_registry=True,
        actual_tool_source=True,normal_close_and_settlement=True,
        version_switch=version,default_replaced=False,
        model_quality_separate=True,automatic_request_or_tool_replay=False)


GLOBALS.update(__file__=__file__,LIMITS=LIMITS,prepare=prepare,Stage=Stage,
    check_business=check_business,business_record=business_record,
    PREPARE_PATH_ARGUMENTS=('profile-reference',),ACCEPTANCE_PREFIX='REALTIME_PROMPT_SERVICE_')
for function in (VERSION['launcher'],MANAGED['launcher']):
    function.__globals__['__file__']=__file__

if __name__=='__main__':BASE['main']()
