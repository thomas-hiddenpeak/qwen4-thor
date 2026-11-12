"""Bounded real outer/resource/version business; no foreign-service mutation.

Only validation dispatches are counted/wrapped. Production observation,
decisions, model code, device probes and persistent recovery budgets stay intact.
The explicit root entry installs/removes one new owned CPU resource hierarchy;
all GPU work runs after permanent UID drop under the normal strict NVML guard.
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
import stat
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
VERSION = runpy.run_path(str(ROOT/'tools/serve_recovery/version_acceptance.py'))
LEGACY = VERSION['LEGACY']
LIMITS = dict(starts=3, chats=8, probes=1, controls=2500, seconds=900)
OLD_LIMITS = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS if name == 'outer_resource_version' else OLD_LIMITS(name))
VERSION['actor'].__globals__['LIMITS'] = LIMITS
save = LEGACY['save']


def resource_fd():
    raw = os.environ['Q4T_RESOURCE_ENTRY_FD']
    if not raw.isdecimal() or not 3 <= int(raw) < 2**31:
        raise ValueError('explicit resource entry descriptor required')
    fd = int(raw); os.fstat(fd)
    return fd


def guard(plan, path):
    LEGACY['verify'](plan)
    from tools.serve_recovery import child
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    child.verify_lease(lease, plan['lease_path'])
    fd = resource_fd()
    command = [sys.executable, '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
        '--output', str(Path(plan['output'])/'lease'), '--compute-backend', 'nvml',
        '--lease-fd', str(lease), '--inherit-lease', '--inherit-fd', str(fd), '--',
        sys.executable, '-I', '-B', str(Path(__file__).resolve()), 'run', '--plan', str(path)]
    os.execve(command[0], command, dict(os.environ))


def prepare(args):
    bundle, _, _, _, service, engine = LEGACY['load_modules']()
    from tools.serve_recovery import outer_manager, resource_compat, version_contract, version_registration
    engine.require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
                   'commit reviewed validation sources first')
    out = args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work') and not out.exists(), 'new private output required')
    out.mkdir(mode=0o700, parents=True)
    origin = args.package.resolve(strict=True)
    old = bundle.verify(origin, engine.sha(origin/'manifest.json'))
    base = engine.read(origin/'engine/manifest.json'); settings = base['settings']
    gateway = engine.read(origin/'gateway-policy.json')
    engine.require(old['ingress_mode'] == 'native' and not old['tls'] and
        gateway['listen_host'] == '127.0.0.1', 'bounded loopback acceptance required')
    native = engine.read(origin/'native-build.json')
    changed = {p for p in bundle.NATIVE_SOURCES if native['source_files'].get(p) != engine.sha(ROOT/p)}
    allowed = {'cmake/GuardianProcess.cmake', 'src/service/guardian_main.cpp',
        'include/q4t/service/device_filter.h', 'src/service/device_filter.cpp',
        'include/q4t/service/device_admission.h', 'src/service/device_admission.cpp',
        'src/service/device_control_main.cpp'}
    build = ROOT/'.q4t-work/guardian-build'
    review = engine.read(args.native_build)
    engine.require(changed <= allowed and review['exit_code'] == 0 and review['warnings'] == 0,
                   'native source/build scope changed')
    for name, digest in review['artifacts'].items():
        engine.require(engine.sha(build/name) == digest, 'native artifact changed')
    native.update(source_files={p:engine.sha(ROOT/p) for p in bundle.NATIVE_SOURCES},
        guardian_sha256=engine.sha(build/'q4t-guardian'),
        device_control_sha256=engine.sha(build/'q4t-device-control'))
    engine.save(out/'native-build.json', native)
    qualified = out/'qualified'; qualified.mkdir(mode=0o700)
    engine.save(qualified/'registry.json', old['recovery_profiles'])
    for name in ('primary','light'):
        (qualified/(name+'.json')).write_bytes((origin/('profile-'+name+'.json')).read_bytes())
    capacity = dict(version=1, **{k:v for k,v in settings['profile'].items() if k not in ('name','host')})
    application = {k:v for k,v in settings['application_state'].items() if k != 'profile'}
    receipt = bundle.prepare(origin/'engine/q4t', origin/'engine/q4t.release.json', base['model_dir'],
        build/'q4t-guardian', origin/'bin/q4t-cuda-probe', None, origin/'gateway-policy.json',
        origin/'engine/client-policy.json', out/'bundle', native_receipt=out/'native-build.json',
        native_ingress=origin/'bin/q4t-ingress', device_control=build/'q4t-device-control',
        capacity_config=capacity, application_state=application, model_names=settings.get('model_names'),
        embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=qualified/'registry.json', recovery_profile_policy=old['recovery_profile_policy'])
    previous = engine.read(args.registration)
    accepted = previous['fallback_acceptance']
    retained = previous['payloads']['fallback']['descriptor']
    adapter = resource_compat.prepare(retained['package'], retained['manifest_sha256'], out/'retained-fallback')
    root = dict(package=receipt['package'], manifest_sha256=receipt['manifest_sha256'])
    fallback = dict(package=adapter['payload'], manifest_sha256=adapter['payload_manifest_sha256'])
    registration = version_contract.prepare(root, root, fallback, str(uuid.uuid4()), fallback_acceptance=accepted)
    initialized = service.initialize(root['package'], root['manifest_sha256'], out/'state')
    version_registration.install(out/'state', registration,
        request_id=registration['registry']['cycle_id'], expected_cycle=None)
    outer_manager.initialize(out/'state', root['package'], root['manifest_sha256'])
    outer_manager.configure(out/'state', root['package'], root['manifest_sha256'], enabled=True)
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_host',
        'tools/serve_service','tools/serve_ops','tools/serve_gateway','tools/evalscope','tools/examples',
        'tools/release','src/service','include/q4t/service'], cwd=ROOT, text=True).splitlines()
    names.append('docs/SERVE_MEMORY_STOP_2026-11-03.md')
    primary = Path(args.protected_root).resolve(strict=True)
    plan = dict(version=1, scenario='outer_resource_version', limits=LIMITS, package=receipt,
        registration=registration, output=str(out), source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        sources={n:engine.sha(ROOT/n) for n in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(primary/n),sha256=engine.sha(primary/n)) for n in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json')],
        service_id=initialized['service_id'], runtime_uid=os.getuid(), runtime_gid=os.getgid(),
        lease_path=bundle.verify(root['package'],root['manifest_sha256'])['lock_path'],
        requests=engine.read(args.requests)['requests'], fallback_adapter=adapter,
        resource_limits=dict(high_mib=114688,max_mib=122880,tasks=4096),
        planned=dict(starts=3,self_checks=3,business_chats=4,probes=1),
        sequence=['primary real structured JSON and persistent Chat',
            'owned Guardian SIGKILL, complete original tree cleanup and native outer restart',
            'second primary real tool request and exactly-once read-only document execution',
            'owned gateway fatal signal, fixed second root probes and selects accepted retained payload',
            'retained payload real tool-result SSE and structured SSE; original stored Chat',
            'durable stop, original resource empty/removal, ports and shared lease release'],
        validation_wrappers=['count every model/self-check/probe before dispatch',
            'record first real resource sample only; observation/decisions unchanged',
            'owned gateway SIGUSR1 becomes SIGABRT with dump disabled'],
        physical_ram_limit=False, automatic_request_or_tool_replay=False,
        default_replacement=False, performance_acceptance=False, foreign_services_modified=False)
    helper = out/'counted-cuda-probe'
    helper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.argv='+repr([
        str(Path(__file__).resolve()),'probe','--plan',str(out/'plan.json')])+\
        '\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
    helper.chmod(0o500)
    plan['probe_wrapper'] = dict(path=str(helper),sha256=engine.sha(helper))
    save(out/'plan.json',plan);save(out/'receipt.json',receipt)
    print(json.dumps(dict(plan=str(out/'plan.json'),limits=LIMITS,service_id=plan['service_id'])))


def launcher(plan, path):
    _, _, _, _, _, engine = LEGACY['load_modules'](plan); LEGACY['verify'](plan)
    from tools.serve_recovery import outer_runtime
    guardian = str(Path(plan['package']['package'])/'bin/q4t-guardian')
    original = subprocess.Popen
    def spawn(argv, **options):
        if guardian in argv:
            index = argv.index(guardian)
            engine.require(argv[index+1] == '--config', 'unknown Guardian dispatch shape')
            config = Path(argv[index+2]); value = engine.read(config); recovery = value['version_recovery']
            engine.require(recovery['registry'] == plan['registration']['registry'], 'registry changed')
            evidence = copy.deepcopy(recovery)
            for name, commands in recovery['payloads'].items():
                for role in ('controller','gateway'):
                    commands[role] = [sys.executable,'-I','-B',str(Path(__file__).resolve()),role,
                        '--plan',str(path),'--payload',name,'--config',commands[role][-1]]
            wrapper = plan['probe_wrapper']
            engine.require(engine.sha(wrapper['path']) == wrapper['sha256'], 'probe wrapper changed')
            recovery['cuda_probe'] = wrapper['path']; engine.save(config,value)
            save(Path(plan['output'])/'execution'/('wrapper-'+config.parent.name+'.json'),
                 dict(production=evidence,validation=recovery))
        return original(argv, **options)
    subprocess.Popen = spawn
    try:
        result = outer_runtime.run(Path(plan['output'])/'state',Path(plan['package']['package']),
                                   plan['package']['manifest_sha256'],activate=True)
        save(Path(plan['output'])/'execution/launcher-result.json',result)
        return int(result['state'] != 'STOPPED' or not result['cleanup_confirmed'])
    finally: subprocess.Popen = original


def actor(plan, name, role, config):
    if role == 'controller':
        selected = VERSION['payload_plan'](plan,name); LEGACY['load_modules'](selected)
        from tools.serve_recovery import controller
        if (controller.ROOT/'tools/serve_recovery/resource_client.py').is_file():
            from tools.serve_recovery import resource_client as client
        else:
            from tools.serve_recovery import profile_memory as client
        tick = client.Observer.tick; recorded = False
        def sample(observer, **options):
            nonlocal recorded
            tick(observer, **options)
            value = observer.snapshot()
            if not recorded and value is not None:
                save(Path(plan['output'])/'execution'/('resource-'+str(os.getpid())+'.json'),value)
                recorded = True
        client.Observer.tick = sample
    return VERSION['actor'](plan,name,role,config)


class Stage(VERSION['Stage']):
    def initialize_state(self):
        self.scope = Path(self.plan['output'])/'state'
        value = self.service.descriptor(self.scope,self.package,self.plan['package']['manifest_sha256'])
        self.engine.require(value['service_id'] == self.plan['service_id'] and
            self.engine.read(self.scope/'ledger.json')['state']['desired'] == 'STOPPED',
            'private preinitialized stopped state changed')
        return dict(service_id=value['service_id'],state=str(self.scope))

    def family(self,name):
        from tools.serve_recovery import lifecycle, profile_store, usage_store
        writer = profile_store.Store if name == 'profiles' else usage_store.Store
        end = time.monotonic()+5
        while True:
            try: return lifecycle.read_family(self.scope,dict(service_id=self.service_id),writer)
            except BlockingIOError:
                if time.monotonic() >= end: raise
                time.sleep(.025)

    def resource_ready(self,generation,payload):
        from tools.serve_recovery import resource_install, resource_kernel, resource_client, ledger
        state = self.version_ready(generation,payload)
        state = self.wait(lambda s:s['model_generation'] == generation and s['business_ready'] and
            s['controller_health']['observation'] == 'AVAILABLE' and
            (self.output/('resource-'+str(s['controller_pid'])+'.json')).exists(),seconds=15)
        public = resource_install.read_public(Path(os.environ['Q4T_RESOURCE_DIRECTORY']))
        binding = dict(directory=os.environ['Q4T_RESOURCE_DIRECTORY'],publication=public)
        sample = resource_client.validate(self.engine.read(self.output/('resource-'+str(state['controller_pid'])+'.json')))
        self.engine.require(sample['binding_sha256'] == ledger.digest(binding), 'collector domain differs')
        leaf = Path(public['record']['root'])/'service'
        pids = {int(p) for p in (leaf/'cgroup.procs').read_text().split()}
        self.engine.require(all(state[r+'_pid'] in pids for r in ('guardian','controller','gateway','model')),
                            'actual roles escaped the original service domain')
        with resource_kernel.Scope(public['record']) as scope:
            scope.member('outer'); fd = scope.fds['service']
            counters = {n:resource_kernel.read(fd,n) for n in
                ('memory.current','memory.peak','memory.swap.current','memory.events','pids.current')}
        save(self.output/('actual-resource-'+str(generation)+'.json'),dict(binding=binding,roles=state,
            counters=counters,collector=sample,physical_ram_limit=False))
        return state

    def fatal_gateway(self,state):
        pid = state['gateway_pid']; fd = os.pidfd_open(pid)
        try:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            self.engine.require(self.journal.process_start(pid) == state['gateway_start_ticks'] and
                str(Path(__file__).resolve()).encode() in argv and b'gateway' in argv and b'primary' in argv,
                'not the owned validation gateway')
            save(self.output/'gateway-fault.json',dict(pid=pid,start_ticks=state['gateway_start_ticks'],
                trigger='SIGUSR1',actual_wait_signal='SIGABRT',monotonic=time.monotonic()))
            signal.pidfd_send_signal(fd,signal.SIGUSR1)
        finally: os.close(fd)

    def exercise(self):
        from tools.examples.chat_client import read_sse
        first = self.resource_ready(1,'primary')
        stored = self.chat('first-schema',dict(self.plan['requests']['schema'],store=True))
        self.engine.require(json.loads(stored['choices'][0]['message']['content']) == {'answer':42},'schema quality')
        self.kill(first,'guardian','owned-root-exit')
        second = self.resource_ready(2,'primary')
        self.engine.require(first['guardian_pid'] != second['guardian_pid'], 'outer did not replace root')
        before = self.family('profiles');save(self.output/'profiles-before-fallback.json',before)
        call = self.chat('second-tool',self.plan['requests']['tool'])
        message = call['choices'][0]['message'];calls = message['tool_calls']
        document = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'; digest = self.engine.sha(ROOT/document)
        self.engine.require(call['choices'][0]['finish_reason'] == 'tool_calls' and len(calls) == 1 and
            calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=document) and
            digest == self.plan['sources'][document], 'actual tool/source differs')
        reply = dict(role='tool',tool_call_id=calls[0]['id'],content=json.dumps(dict(
            path=document,sha256=digest,answer=digest[:12])))
        save(self.output/'tool-execution.json',dict(executions=1,
            source=dict(path=document,content=(ROOT/document).read_text()),reply=reply))
        self.fatal_gateway(second)
        third = self.resource_ready(3,'fallback')
        self.engine.require(third['guardian_pid'] == second['guardian_pid'] and
            third['version_recovery']['probe_count'] == 1, 'fallback root/probe differs')
        follow = copy.deepcopy(self.plan['requests']['tool'])
        follow.update(tool_choice='none',stream=True,stream_options=dict(include_usage=True))
        follow['messages'] += [message,reply]
        answer = read_sse(io.BytesIO(self.chat('fallback-tool-result-sse',follow,raw_result=True)))
        self.engine.require(answer.finish_reason == 'stop' and digest[:12] in answer.message['content'],
                            'actual tool result failed after fallback')
        answer = read_sse(io.BytesIO(self.chat('fallback-schema-sse',dict(self.plan['requests']['schema'],
            stream=True,stream_options=dict(include_usage=True)),raw_result=True)))
        self.engine.require(answer.finish_reason == 'stop' and
            json.loads(answer.message['content']) == {'answer':42}, 'retained structured SSE quality')
        retained = self.request('retained-chat',path='/v1/chat/completions/'+stored['id'])
        self.engine.require(retained['choices'] == stored['choices'], 'stored Chat lost across recovery')
        after = self.family('profiles');save(self.output/'profiles-after-fallback.json',after)
        self.engine.require(all(before[k] == after[k] for k in ('history','pending','selected',
            'not_before_ns','light_since_ns')), 'profile recovery budget changed')
        outer = self.engine.read(self.scope/'outer-recovery/policy.json')['state']['policy']
        counts = self.engine.read(self.output/'counts.json')['counts']
        self.engine.require(outer['total'] == 2 and counts['starts'] == 3 and counts['chats'] == 7 and
            counts['probes'] == 1, 'unexpected recovery dispatch count')
        save(self.output/'business-final.json',dict(status='PASS',outer=outer,counts=counts,
            real_tool_executions=1,stored_chat_retained=True,final_state=third,physical_ram_limit=False))

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            from tools.serve_recovery import outer_control
            save(self.output/'outer-stop.json',outer_control.request(self.service_id,'stop',timeout=2))
            self.process.wait(timeout=65)
        return LEGACY['Stage'].stop(self)

    def run(self):
        original = subprocess.Popen; fd = resource_fd()
        def spawn(argv, **options):
            if 'launcher' in argv and argv[3] == LEGACY['__file__']:
                argv = list(argv);argv[3] = str(Path(__file__).resolve())
                options['pass_fds'] = (*options['pass_fds'],fd)
            return original(argv,**options)
        subprocess.Popen = spawn
        try: return LEGACY['Stage'].run(self)
        finally: subprocess.Popen = original


def reap(pid,seconds):
    end = time.monotonic()+seconds
    while True:
        found,status = os.waitpid(pid,os.WNOHANG)
        if found: return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= end: return None
        time.sleep(min(.05,max(0,end-time.monotonic())))


def finish_child(pid):
    result = reap(pid,0)
    if result is not None: return result
    os.kill(pid,signal.SIGTERM);result = reap(pid,30)
    if result is not None: return result
    os.kill(pid,signal.SIGKILL)
    return reap(pid,1)


def finish_lease(fd,output,clean):
    raw = json.dumps(dict(state='released' if clean else 'cleanup_pending',
        output=str(output),host_result=str(Path(output)/'host-result.json'))).encode()+b'\n'
    os.lseek(fd,0,os.SEEK_SET);os.ftruncate(fd,0)
    if os.write(fd,raw) != len(raw):raise OSError('short lease cleanup receipt')
    os.fsync(fd)


def host(plan,path):
    LEGACY['verify'](plan)
    from tools.serve_recovery import ledger, resource_install, resource_kernel, resource_launch
    uid,gid = plan['runtime_uid'],plan['runtime_gid'];out = Path(plan['output'])
    ledger.require(os.geteuid() == 0 and uid == int(os.environ['SUDO_UID']) > 0 and
        out.stat().st_uid == uid and out.stat().st_gid == gid, 'explicit original administrator required')
    directory = Path('/var/lib')/('q4t-resource-validation-'+plan['service_id'])
    ledger.require(not directory.exists(), 'new private validation resource domain required')
    lock_path = Path(plan['lease_path']);fd = os.open(lock_path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
    pid = None;held = False
    result = dict(status='ENV_ABORT',foreign_services_modified=False,device_rules_installed=False)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and
            (info.st_dev,info.st_ino) == (lock_path.stat().st_dev,lock_path.stat().st_ino), 'shared lease replaced')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        held = True
        ledger.require(not lock_path.with_name(lock_path.name+'.recovery-required').exists(),
                       'unfinished shared GPU recovery')
        result['resource'] = resource_install.initialize(directory,plan['service_id'],uid,gid,
            resource_kernel.policy(**plan['resource_limits']))
        os.set_inheritable(fd,True);os.environ['Q4T_MANAGED_LEASE_FD'] = str(fd)
        pid = os.fork()
        if pid == 0:
            try:
                resource_launch.enter(directory,[sys.executable,'-I','-B',str(Path(__file__).resolve()),
                    'guard','--plan',str(path)])
            except BaseException:traceback.print_exc()
            os._exit(1)
        # This parent never enters either leaf or creates model work itself.
        code = reap(pid,LIMITS['seconds'])
        if code is None: raise TimeoutError('bounded outer business deadline')
        pid = None;result['child_exit'] = code
        result['status'] = 'PASS' if code == 0 else 'ENV_ABORT' if code == 75 else 'VALIDATION_FAILED'
    except BaseException:result['error'] = traceback.format_exc()
    finally:
        if pid is not None:
            try:
                result['child_exit'] = finish_child(pid)
            except BaseException:result['child_cleanup_error'] = traceback.format_exc()
        try:
            if directory.exists():result['resource_cleanup'] = resource_install.remove(directory)
        except BaseException:result.update(status='ENV_ABORT',resource_cleanup_error=traceback.format_exc())
        result['lease_held_through_cleanup'] = held
        report = out/'host-result.json';ledger.require(not report.exists(),'host result already exists')
        try:
            save(report,result);os.chown(report,uid,gid)
            if held:finish_lease(fd,out,'resource_cleanup_error' not in result and
                'child_cleanup_error' not in result and
                (not directory.exists() or result.get('resource_cleanup',{}).get('removed') is True))
        finally:os.close(fd)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 75


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','host','guard','run','launcher','controller','gateway','probe'))
    for name in ('package','registration','requests','output','plan','config','native-build','protected-root'):
        parser.add_argument('--'+name,type=Path)
    parser.add_argument('--payload',choices=('primary','fallback'));args = parser.parse_args()
    if args.mode == 'prepare':prepare(args);return 0
    plan = json.loads(args.plan.read_bytes());path = args.plan.resolve()
    if args.mode == 'host':
        def interrupted(*_):raise KeyboardInterrupt('outer validation interrupted; cleanup only')
        previous = signal.signal(signal.SIGTERM,interrupted)
        try:return host(plan,path)
        finally:signal.signal(signal.SIGTERM,previous)
    if args.mode == 'guard':return guard(plan,path)
    if args.mode == 'launcher':return launcher(plan,path)
    if args.mode in ('controller','gateway'):return actor(plan,args.payload,args.mode,args.config)
    if args.mode == 'probe':return VERSION['probe'](plan)
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan,path).run()


if __name__ == '__main__':sys.exit(main())
