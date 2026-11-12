"""Bounded real version fallback; private state and strict inherited GPU lease.

Validation wrappers only count dispatches and turn an owned SIGUSR1 into an
actual SIGABRT without a host core-dump pipeline. Production recovery choices,
device probe, whole-tree cleanup and all payload code remain unchanged.
"""
import argparse
import copy
import ctypes
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
MANAGED = runpy.run_path(str(ROOT/'tools/serve_recovery/profile_managed_acceptance.py'))
LEGACY = MANAGED['LEGACY']
LIMITS = dict(starts=2, chats=10, probes=1, controls=2500, seconds=900)
OLD_LIMITS = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS if name == 'version_recovery' else OLD_LIMITS(name))
save = LEGACY['save']


def payload_plan(plan, name):
    if name not in ('primary', 'fallback'):raise ValueError('unknown validation payload')
    receipt = plan['registration']['payloads'][name]['descriptor']
    return dict(plan, package=receipt)


def inject_fatal_signal():
    """Only an explicitly signalled validation actor invokes this handler."""
    if ctypes.CDLL(None,use_errno=True).prctl(4,0,0,0,0) != 0:
        raise OSError('cannot disable core dump for owned fatal-signal fixture')
    signal.signal(signal.SIGABRT,signal.SIG_DFL)
    os.kill(os.getpid(),signal.SIGABRT)


def prepare(args):
    bundle,_,_,_,_,engine = LEGACY['load_modules']()
    from tools.serve_recovery import profile_compat, version_contract
    engine.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
                   'commit reviewed sources before version acceptance')
    out=args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work'), 'private version acceptance directory')
    out.mkdir(mode=0o700,parents=True)
    origin=args.package.resolve(strict=True)
    old_sha=engine.sha(origin/'manifest.json');old=bundle.verify(origin,old_sha)
    base=engine.read(origin/'engine/manifest.json')
    engine.require(old['ingress_mode']=='native' and not old['tls'] and
        engine.read(origin/'gateway-policy.json')['listen_host']=='127.0.0.1',
        'bounded version scenario requires retained loopback service')
    build=ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
    native=engine.read(origin/'native-build.json')
    allowed={'cmake/GuardianProcess.cmake','src/service/guardian_main.cpp',
             'src/service/guardian_journal.cpp','include/q4t/service/guardian_journal.h',
             'src/service/guardian_version.cpp','include/q4t/service/guardian_version.h'}
    changed={p for p in bundle.NATIVE_SOURCES
             if native['source_files'].get(p)!=engine.sha(ROOT/p)}
    engine.require(changed <= allowed and set(native['source_files']) <= bundle.NATIVE_SOURCES,
                   'version native rebuild source scope changed')
    review=engine.read(ROOT/'docs/evidence/serve-version-native-root-20261112/decision.json')
    engine.require(engine.sha(build/'q4t-guardian')==review['guardian_sha256'] and
        all(engine.sha(ROOT/p)==h for p,h in review['sources'].items() if p in changed),
        'reviewed native root build changed')
    native.update(source_files={p:engine.sha(ROOT/p) for p in bundle.NATIVE_SOURCES},
                  guardian_sha256=engine.sha(build/'q4t-guardian'))
    engine.save(out/'native-build.json',native)
    qualified=out/'qualified';qualified.mkdir(mode=0o700)
    engine.save(qualified/'registry.json',old['recovery_profiles'])
    for name in ('primary','light'):
        source=origin/('profile-'+name+'.json')
        (qualified/(name+'.json')).write_bytes(source.read_bytes())
    settings=base['settings']
    capacity=dict(version=1,**{k:v for k,v in settings['profile'].items() if k not in ('name','host')})
    application={k:v for k,v in settings['application_state'].items() if k!='profile'}
    receipt=bundle.prepare(origin/'engine/q4t',origin/'engine/q4t.release.json',base['model_dir'],
        build/'q4t-guardian',origin/'bin/q4t-cuda-probe',None,
        origin/'gateway-policy.json',origin/'engine/client-policy.json',out/'bundle',
        native_receipt=out/'native-build.json',native_ingress=origin/'bin/q4t-ingress',
        capacity_config=capacity,application_state=application,model_names=settings.get('model_names'),
        embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=qualified/'registry.json',recovery_profile_policy=old['recovery_profile_policy'])
    fallback=profile_compat.prepare(origin,old_sha,out/'retained-fallback')
    root=dict(package=receipt['package'],manifest_sha256=receipt['manifest_sha256'])
    retained=dict(package=fallback['payload'],manifest_sha256=fallback['payload_manifest_sha256'])
    acceptance={name:dict(path=str(path.resolve(strict=True)),sha256=engine.sha(path))
        for name,path in [('release',args.release),('decision',args.decision)]}
    registration=version_contract.prepare(root,root,retained,str(uuid.uuid4()),
                                          fallback_acceptance=acceptance)
    engine.save(out/'receipt.json',receipt);engine.save(out/'registration.json',registration)
    qualification=engine.read(args.requests)
    names=subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_host',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope',
        'tools/examples','tools/release','src/service','include/q4t/service'],cwd=ROOT,text=True).splitlines()
    document='docs/SERVE_MEMORY_STOP_2026-11-03.md';names.append(document)
    plan=dict(version=1,scenario='version_recovery',limits=LIMITS,package=receipt,
        registration=registration,output=str(out),source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/n),sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json')],
        requests=qualification['requests'],fallback_adapter=fallback,
        planned=dict(starts=2,self_checks=2,max_business_requests=8,embedding_steps=20,probes=1),
        validation_wrappers=['pre-dispatch controller/model/self-check counts for each sealed payload',
            'pre-exec count then unchanged sealed CUDA probe',
            'owned gateway SIGUSR1 produces actual SIGABRT with dump disabled immediately beforehand'],
        sequence=['primary JSON/stored Chat/tool/Batch and beta quota',
            'owned gateway fatal signal; actual whole-tree cleanup and CUDA proof',
            'fixed root, older retained payload and original usage/profile budgets',
            'retained original Chat/Batch/file; tool response SSE and final structured SSE',
            'read-only actual-payload observation; durable stop and full cleanup'],
        automatic_request_or_tool_replay=False,default_replacement=False,
        performance_acceptance=False)
    helper=out/'counted-cuda-probe'
    helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
        str(Path(__file__).resolve()),'probe','--plan',str(out/'plan.json')])+\
        '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
    helper.chmod(0o500)
    plan['probe_wrapper']=dict(path=str(helper),sha256=engine.sha(helper))
    save(out/'plan.json',plan)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS,
                         primary=root['manifest_sha256'],fallback=retained['manifest_sha256'])))


def launcher(plan, path):
    _,_,_,_,service,engine=LEGACY['load_modules'](plan)
    LEGACY['verify'](plan)
    guardian=str(Path(plan['package']['package'])/'bin/q4t-guardian')
    original=subprocess.Popen
    def spawn(argv,**options):
        if argv[0]==guardian:
            config=Path(argv[argv.index('--config')+1]);value=engine.read(config)
            recovery=value['version_recovery']
            engine.require(recovery['registry']==plan['registration']['registry'],
                           'validation registry differs')
            evidence={}
            for name,commands in recovery['payloads'].items():
                evidence[name]=copy.deepcopy(commands)
                for role in ('controller','gateway'):
                    commands[role]=[sys.executable,'-I','-B',str(Path(__file__).resolve()),role,
                        '--plan',str(path),'--payload',name,'--config',commands[role][-1]]
            wrapper=plan['probe_wrapper']
            engine.require(engine.sha(wrapper['path'])==wrapper['sha256'],'probe wrapper changed')
            recovery['cuda_probe']=wrapper['path']
            engine.save(config,value)
            save(Path(plan['output'])/'execution/wrappers.json',
                 dict(production=evidence,validation=recovery))
        return original(argv,**options)
    subprocess.Popen=spawn
    result=service.launch(plan['package']['package'],plan['package']['manifest_sha256'],
                          Path(plan['output'])/'execution/state')
    save(Path(plan['output'])/'execution/launcher-result.json',result)
    return int(result['guardian_exit_code']!=0)


def actor(plan, name, role, config):
    selected=payload_plan(plan,name)
    LEGACY['load_modules'](selected);LEGACY['verify'](selected)
    output=Path(plan['output'])/'execution';budget=LEGACY['Budget'](output,limits=LIMITS)
    if role=='gateway':
        from tools.serve_recovery import native_ingress
        signal.signal(signal.SIGUSR1,lambda *_:inject_fatal_signal())
        save(output/('gateway-'+name+'-source.json'),dict(source=native_ingress.__file__,pid=os.getpid()))
        sys.argv=[sys.argv[0],'--config',str(config)];native_ingress.main();return
    from tools.serve_recovery import controller, core_http
    original_spawn,original_request=subprocess.Popen,core_http.request
    def spawn(argv,**options):
        if str(controller.ROOT/'tools/serve_recovery/child.py') in argv:
            budget.reserve('starts',name+'-model')
        elif str(Path(selected['package']['package'])/'bin/q4t-cuda-probe') in argv:
            budget.reserve('probes',name+'-controller-probe')
        return original_spawn(argv,**options)
    def request(port,method,path,**options):
        if method=='POST':
            ordinal=budget.reserve('chats',name+'-self-check')
            result=original_request(port,method,path,**options)
            save(output/('self-check-'+str(ordinal)+'.json'),result);return result
        return original_request(port,method,path,**options)
    save(output/('controller-'+name+'-source.json'),dict(source=controller.__file__,pid=os.getpid()))
    subprocess.Popen,core_http.request=spawn,request
    sys.argv=[sys.argv[0],'--config',str(config)];controller.main()


def probe(plan):
    # Reserve before exec; no substitute device result or altered probe.
    output=Path(plan['output'])/'execution'
    LEGACY['Budget'](output,limits=LIMITS).reserve('probes','root-version-device-proof')
    _,child,_,_,_,engine=LEGACY['load_modules'](plan)
    package=Path(plan['package']['package'])
    manifest=engine.read(package/'manifest.json');binary=package/'bin/q4t-cuda-probe'
    engine.require(engine.sha(binary)==manifest['files']['bin/q4t-cuda-probe'],'device probe changed')
    # Native SpawnProcess intentionally has an empty environment. Identify the
    # inherited descriptor itself, never an environment-only lease assertion.
    held=Path(manifest['lock_path']).stat();matches=[]
    entries=list(Path('/proc/self/fd').iterdir())
    engine.require(len(entries)<=64,'validation probe descriptor bound')
    for entry in entries:
        fd=int(entry.name)
        if fd<3:continue
        try:info=os.fstat(fd)
        except OSError:continue
        if (info.st_dev,info.st_ino)==(held.st_dev,held.st_ino):matches.append(fd)
    engine.require(len(matches)==1,'unique inherited device-probe lease required')
    child.verify_lease(matches[0],manifest['lock_path'])
    os.execv(str(binary),[str(binary)])


class Stage(MANAGED['Stage']):
    def initialize_state(self):
        from tools.serve_recovery import version_registration
        result=super().initialize_state()
        registration=self.plan['registration']
        installed=version_registration.install(self.output/'state',registration,
            request_id=registration['registry']['cycle_id'],expected_cycle=None)
        save(self.output/'version-installed.json',installed)
        return result

    def version_ready(self, generation, name):
        state=self.ready(generation,'primary')
        selected=state['version_recovery']
        expected=self.plan['registration']['payloads'][name]
        self.engine.require(selected['selected']==name and not selected['pending'] and
            selected['payload_manifest_sha256']==expected['descriptor']['manifest_sha256'] and
            not selected['protected'],'unexpected runtime recovery payload')
        return state

    def fatal_gateway(self, state):
        pid,ticks=state['gateway_pid'],state['gateway_start_ticks']
        fd=os.pidfd_open(pid)
        try:
            fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
            argv=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            self.engine.require(int(fields[1])==state['guardian_pid'] and
                self.journal.process_start(pid)==ticks and str(Path(__file__).resolve()).encode() in argv
                and b'gateway' in argv and b'primary' in argv,'fatal target is not owned primary gateway')
            save(self.output/'gateway-fault.json',dict(pid=pid,start_ticks=ticks,
                trigger='SIGUSR1',actual_wait_signal='SIGABRT',monotonic=time.monotonic()))
            signal.pidfd_send_signal(fd,signal.SIGUSR1)
        finally:os.close(fd)

    def exercise(self):
        from tools.serve_recovery import profile_evidence, version_launch
        from tools.serve_host import native
        from tools.examples.chat_client import read_sse
        first=self.version_ready(1,'primary')
        stored=self.chat('primary-schema',dict(self.plan['requests']['schema'],store=True,
            metadata=dict(purpose='version-recovery-continuity')))
        self.engine.require(json.loads(stored['choices'][0]['message']['content'])=={'answer':42},
                            'primary JSON schema quality')
        call=self.chat('primary-tool',self.plan['requests']['tool'])
        message=profile_evidence.message(call,'tool_calls');calls=message['tool_calls']
        document='docs/SERVE_MEMORY_STOP_2026-11-03.md'
        self.engine.require(len(calls)==1 and calls[0]['function']['name']=='project_document' and
            json.loads(calls[0]['function']['arguments'])==dict(path=document),'tool arguments differ')
        source=dict(path=document,content=(ROOT/document).read_text());digest=self.engine.sha(ROOT/document)
        self.engine.require(digest==self.plan['sources'][document],'tool source changed')
        reply=dict(role='tool',tool_call_id=calls[0]['id'],content=json.dumps(dict(
            path=document,sha256=digest,answer=digest[:12])))
        save(self.output/'tool-execution.json',dict(executions=1,source=source,reply=reply))
        batch,raw,_vector=self.batch()
        self.chat('beta-before',self.plan['requests']['schema'],client='beta')
        self.chat('beta-denial-before',self.plan['requests']['schema'],client='beta',expected=429)
        beta_at=time.monotonic()
        before=self.family('profiles');save(self.output/'profiles-before.json',before)
        self.fatal_gateway(first)
        second=self.version_ready(2,'fallback')
        self.engine.require(second['guardian_pid']==first['guardian_pid'] and
            all(second[role+'_generation']>first[role+'_generation'] and
                second[role+'_pid']!=first[role+'_pid'] for role in ('controller','model','gateway')),
            'fallback did not replace entire old payload under fixed root')
        observed=second['version_recovery']
        self.engine.require(observed['probe_count']==1 and observed['last_fault']['role']=='gateway' and
            observed['last_fault']['signal']==6,'actual native fault/device proof differs')
        usage=self.family('usage');save(self.output/'usage-after-fallback.json',usage)
        inherited=next(c for c in usage['snapshot']['clients'] if c['id']=='beta')
        self.engine.require(int(inherited['admitted'])>=1 and int(inherited['rpm_rejected'])>=1,
                            'fallback lost real client accounting')
        elapsed=time.monotonic()-beta_at
        if elapsed<59:self.chat('beta-denial-after',self.plan['requests']['schema'],client='beta',expected=429)
        save(self.output/'quota-window.json',dict(seconds=elapsed,denial_checked=elapsed<59))
        self.retained(batch,raw,stored)
        follow=copy.deepcopy(self.plan['requests']['tool']);follow.update(tool_choice='none',stream=True,
            stream_options=dict(include_usage=True));follow['messages'] += [message,reply]
        wire=self.chat('fallback-tool-result-sse',follow,raw_result=True)
        answer=read_sse(io.BytesIO(wire))
        self.engine.require(answer.finish_reason=='stop' and digest[:12] in answer.message['content'],
                            'real tool response did not survive version fallback')
        wire=self.chat('fallback-schema-sse',dict(self.plan['requests']['schema'],stream=True,
            stream_options=dict(include_usage=True)),raw_result=True)
        answer=read_sse(io.BytesIO(wire))
        self.engine.require(answer.finish_reason=='stop' and json.loads(answer.message['content'])=={'answer':42},
                            'fallback structured SSE quality')
        after=self.family('profiles');save(self.output/'profiles-after.json',after)
        self.engine.require(all(before[k]==after[k] for k in
            ('history','pending','selected','not_before_ns','light_since_ns')),
            'version recovery reset qualified profile history or budget')
        observer=native.bind(self.package,self.plan['package']['manifest_sha256'],self.output/'state')
        view=native.collect(observer);save(self.output/'observed-fallback.json',view)
        self.engine.require(view['business_ready'] is True and view['sources']['guardian'] and
            view['version_recovery']['payload_manifest_sha256']==observed['payload_manifest_sha256'],
            'actual older payload cannot be observed')
        desc=self.engine.read(self.output/'state/service.json')
        save(self.output/'version-status.json',version_launch.status(self.output/'state',desc))
        save(self.output/'business-final.json',dict(status='PASS',fixed_root=True,
            fallback=observed,original_batch=True,original_stored_chat=True,
            original_client_accounting=True,no_tool_replay=True))

    def run(self):
        failure=cleanup_failure=None
        def expired(*_):raise TimeoutError('bounded version recovery deadline')
        alarm=signal.signal(signal.SIGALRM,expired);term=signal.signal(signal.SIGTERM,expired)
        signal.setitimer(signal.ITIMER_REAL,LIMITS['seconds']-70)
        try:
            initialized=self.initialize_state();self.service_id=initialized['service_id']
            save(self.output/'initialized.json',initialized)
            with (self.output/'launcher.log').open('xb') as log:
                self.process=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),
                    'launcher','--plan',str(self.plan_path)],stdin=subprocess.DEVNULL,
                    stdout=log,stderr=log,pass_fds=(self.lease,),start_new_session=True)
            self.exercise();LEGACY['verify'](self.plan)
        except BaseException:failure=traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
            try:self.stop()
            except BaseException:cleanup_failure=traceback.format_exc()
            signal.signal(signal.SIGALRM,alarm);signal.signal(signal.SIGTERM,term)
            result=dict(status='PASS_BOUNDED_VERSION_RECOVERY' if not failure and not cleanup_failure else 'FAILED',
                failure=failure,cleanup_failure=cleanup_failure,seconds=time.monotonic()-self.started,
                budget=self.engine.read(self.output/'counts.json'),models=self.models,
                package=self.plan['package'],fallback=self.plan['registration']['payloads']['fallback']['descriptor'],
                default_replaced=False,performance_acceptance=False)
            save(self.output/'result.json',result);print(json.dumps(result),flush=True)
        return int(failure is not None or cleanup_failure is not None)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['prepare','run','launcher','controller','gateway','probe'])
    for name in ('package','release','decision','requests','output','plan','config'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--payload',choices=['primary','fallback'])
    args=parser.parse_args()
    if args.mode=='prepare':prepare(args);return 0
    plan=json.loads(args.plan.read_bytes())
    if args.mode=='probe':probe(plan);return 0
    if args.mode=='launcher':return launcher(plan,args.plan)
    if args.mode in ('controller','gateway'):actor(plan,args.payload,args.mode,args.config);return 0
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan,args.plan).run()


if __name__=='__main__':raise SystemExit(main())
