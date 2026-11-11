"""One real cold model launch, explicit inner-NVML fault, strict outer lease.

Preparation never starts a model or changes the host device policy. Runtime
requires root-issued admission descriptors supplied by device-domain enter.
Only the inner NVML worker is replaced with a failing CPU worker; the actual
CUDA probe, Guardian, model, HTTP path and outer NVML guard remain unchanged.
"""
import argparse
import copy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
MANAGED = runpy.run_path(str(ROOT/'tools/serve_recovery/profile_managed_acceptance.py'))
LEGACY = MANAGED['LEGACY']
LIMITS = dict(starts=1, chats=3, probes=1, controls=2000, seconds=600)
OLD_LIMITS = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    LIMITS if name == 'cold_device_domain' else OLD_LIMITS(name))
save = LEGACY['save']


def descriptors():
    raw = os.environ['Q4T_DEVICE_ADMISSION_FDS']
    values = tuple(int(part) for part in raw.split(','))
    if len(values) != 4 or len(set(values)) != 4 or any(fd <= 2 for fd in values):
        raise ValueError('four explicit admission descriptors required')
    for fd in values: os.fstat(fd)
    return values


def guard(plan, path):
    """Keep the real outer guard strict while carrying explicit root objects."""
    LEGACY['verify'](plan)
    from tools.serve_recovery import child
    fds = descriptors()
    lease_fd = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    lock_path = Path(plan['lease_path'])
    child.verify_lease(lease_fd, lock_path)
    command = [sys.executable, '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
        '--output', str(Path(plan['output'])/'lease'), '--compute-backend', 'nvml',
        '--lease-fd', str(lease_fd), '--inherit-lease']
    for fd in fds: command.extend(('--inherit-fd', str(fd)))
    command.extend(('--', sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                    'run', '--plan', str(path)))
    os.execve(command[0], command, dict(os.environ))


def prepare(args):
    bundle, _, _, _, service, engine = LEGACY['load_modules']()
    engine.require(not subprocess.check_output(['git','status','--porcelain'], cwd=ROOT),
                   'commit reviewed cold-start sources first')
    out = args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work') and not out.exists(),
                   'new private cold acceptance directory required')
    out.mkdir(mode=0o700, parents=True)
    origin = args.package.resolve(strict=True)
    old = bundle.verify(origin, engine.sha(origin/'manifest.json'))
    base = engine.read(origin/'engine/manifest.json'); settings = base['settings']
    gateway = engine.read(origin/'gateway-policy.json')
    engine.require(old['ingress_mode'] == 'native' and not old['tls'] and
        gateway['listen_host'] == '127.0.0.1', 'loopback cold business scope')
    build = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
    native = engine.read(origin/'native-build.json')
    allowed = {'cmake/GuardianProcess.cmake', 'src/service/guardian_main.cpp',
        'include/q4t/service/device_filter.h', 'src/service/device_filter.cpp',
        'include/q4t/service/device_admission.h', 'src/service/device_admission.cpp',
        'src/service/device_control_main.cpp'}
    changed = {p for p in bundle.NATIVE_SOURCES
               if native['source_files'].get(p) != engine.sha(ROOT/p)}
    engine.require(changed <= allowed, 'cold native rebuild scope changed')
    build_record = engine.read(ROOT/'docs/evidence/serve-device-domain-cpu-20261112/build.json')
    engine.require(build_record['exit_code'] == 0, 'device control build did not pass')
    reviewed = engine.read(ROOT/'docs/evidence/serve-cold-driver-cpu-20261112/review.json')
    for name, digest in reviewed['native_artifacts'].items():
        engine.require(engine.sha(build/name) == digest, 'reviewed cold artifact changed: '+name)
    engine.require(reviewed['native_sources'] ==
        {p:engine.sha(ROOT/p) for p in bundle.NATIVE_SOURCES}, 'reviewed cold native sources changed')
    native.update(source_files={p:engine.sha(ROOT/p) for p in bundle.NATIVE_SOURCES},
        guardian_sha256=engine.sha(build/'q4t-guardian'),
        device_control_sha256=engine.sha(build/'q4t-device-control'))
    engine.save(out/'native-build.json', native)
    qualified = out/'qualified'; qualified.mkdir(mode=0o700)
    engine.save(qualified/'registry.json', old['recovery_profiles'])
    for name in ('primary', 'light'):
        (qualified/(name+'.json')).write_bytes((origin/('profile-'+name+'.json')).read_bytes())
    receipt = bundle.prepare(origin/'engine/q4t', origin/'engine/q4t.release.json',
        base['model_dir'], build/'q4t-guardian', origin/'bin/q4t-cuda-probe', None,
        origin/'gateway-policy.json', origin/'engine/client-policy.json', out/'bundle',
        native_receipt=out/'native-build.json', native_ingress=origin/'bin/q4t-ingress',
        device_control=build/'q4t-device-control',
        capacity_config=dict(version=1, **{k:v for k,v in settings['profile'].items()
                                          if k not in ('name','host')}),
        application_state={k:v for k,v in settings['application_state'].items() if k != 'profile'},
        model_names=settings.get('model_names'), embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=qualified/'registry.json', recovery_profile_policy=old['recovery_profile_policy'])
    # Preinitialize only this disposable service so its root device-domain
    # identity can be bound before entering the kernel-controlled domain.
    initialized = service.initialize(receipt['package'], receipt['manifest_sha256'], out/'state')
    engine.save(out/'receipt.json', receipt)
    requests = engine.read(ROOT/'docs/evidence/serve-version-business-20261112/plan.json')['requests']
    names = subprocess.check_output(['git','ls-files','tools/serve_recovery','tools/serve_service',
        'tools/serve_host','tools/serve_ops','tools/serve_gateway','tools/evalscope','tools/examples',
        'src/service','include/q4t/service'], cwd=ROOT, text=True).splitlines()
    document = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'; names.append(document)
    plan = dict(version=1, scenario='cold_device_domain', limits=LIMITS,
        package=receipt, output=str(out), service_id=initialized['service_id'],
        runtime_uid=out.stat().st_uid, runtime_gid=out.stat().st_gid,
        lease_path=str(Path(subprocess.check_output(['git','rev-parse',
            '--path-format=absolute','--git-common-dir'], cwd=ROOT, text=True).strip()).parent/
            '.q4t-work/gpu-exclusive.lock'),
        sources={p:engine.sha(ROOT/p) for p in sorted(set(names)|bundle.NATIVE_SOURCES)},
        protected=[dict(path=str(ROOT/p), sha256=engine.sha(ROOT/p)) for p in
            ('build/q4t','build/q4t.release.json','build/service/manifest.json','build/service.release.json')],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        requests=dict(tool=requests['tool']),
        planned=dict(starts=1, self_checks=1, business_chats=2, probes=1),
        sequence=['independent empty-domain admission outside strict guard',
            'strict outer NVML guard inside allowed domain',
            'inner NVML failure; one authorized actual CUDA probe and model self-check',
            'real project tool JSON then actual result SSE, same healthy model',
            'durable stop, owned cleanup, strict guard success, explicit domain disarm'],
        validation_wrappers=['inner NVML returns injected timeout from a bounded CPU worker',
            'count actual model/probe/self-check dispatch before work',
            'inherit only the four root admission FDs into launcher/Guardian'],
        default_replacement=False, performance_acceptance=False,
        host_root_filter_installed=False, other_services_modified=False)
    save(out/'plan.json', plan)
    print(json.dumps(dict(plan=str(out/'plan.json'), service_id=plan['service_id'], limits=LIMITS,
                         manifest_sha256=receipt['manifest_sha256'], model_actions=False)))


def launcher(plan, path):
    _, _, _, _, service, engine = LEGACY['load_modules'](plan)
    LEGACY['verify'](plan); descriptors()
    original = subprocess.Popen
    guardian = str(Path(plan['package']['package'])/'bin/q4t-guardian')
    def spawn(argv, **options):
        if argv[0] == guardian:
            config = Path(argv[argv.index('--config')+1]); value = engine.read(config)
            engine.require(value['version'] == 4 and value['cold_admission'] is not None,
                           'real cold admission absent from Guardian config')
            production = value['controller']
            value['controller'] = [sys.executable,'-I','-B',str(Path(__file__).resolve()),
                'controller','--plan',str(path),'--config',production[-1]]
            engine.save(config, value)
            save(Path(plan['output'])/'execution/wrapper.json',
                 dict(production=production, validation=value['controller']))
        return original(argv, **options)
    subprocess.Popen = spawn
    result = service.launch(plan['package']['package'], plan['package']['manifest_sha256'],
                            Path(plan['output'])/'state')
    save(Path(plan['output'])/'execution/launcher-result.json', result)
    return int(result['guardian_exit_code'] != 0)


def controller_actor(plan, config):
    LEGACY['load_modules'](plan); LEGACY['verify'](plan)
    from tools.serve_recovery import controller, core_http, monitor, jobs
    out = Path(plan['output'])/'execution'; budget = LEGACY['Budget'](out, limits=LIMITS)
    original_spawn, original_request, original_monitor = subprocess.Popen, core_http.request, monitor.Monitor
    def spawn(argv, **options):
        if str(controller.ROOT/'tools/serve_recovery/child.py') in argv:
            budget.reserve('starts', 'cold-model')
        elif str(Path(plan['package']['package'])/'bin/q4t-cuda-probe') in argv:
            budget.reserve('probes', 'real-cold-CUDA-probe')
        return original_spawn(argv, **options)
    def request(port, method, path, **options):
        if method == 'POST':
            number = budget.reserve('chats', 'cold-self-check')
            result = original_request(port, method, path, **options)
            save(out/('self-check-'+str(number)+'.json'), result)
            return result
        return original_request(port, method, path, **options)
    def factory(command, **options):
        value = dict(event='result', request_id=command[-1], status='ERROR',
            device_count=0, pids=None, error=dict(type='NVMLError',
                message='explicit bounded cold-start NVML timeout injection', nvml_code=10))
        code = 'import sys; print('+repr(json.dumps(value))+',flush=True); sys.exit(1)'
        return jobs.Job([sys.executable, '-I', '-B', '-c', code], **options)
    controller.monitor.Monitor = lambda fd: original_monitor(fd, factory=factory)
    subprocess.Popen, core_http.request = spawn, request
    save(out/'controller-entry.json', dict(
        continuous_model_hint=os.environ['Q4T_CONTINUOUS_MODEL_RECOVERY'],
        cold_device_hint=os.environ['Q4T_COLD_DEVICE_RECOVERY'], source=controller.__file__))
    sys.argv = [sys.argv[0], '--config', str(config)]
    controller.main()


class Stage(MANAGED['Stage']):
    def initialize_state(self):
        self.scope = Path(self.plan['output'])/'state'
        value = self.service.descriptor(self.scope, self.package, self.plan['package']['manifest_sha256'])
        self.engine.require(value['service_id'] == self.plan['service_id'] and
            self.engine.read(self.scope/'ledger.json')['state']['desired'] == 'STOPPED',
            'cold validation state is not the preinitialized stopped service')
        return dict(service_id=value['service_id'], state=str(self.scope))

    def family(self, name):
        from tools.serve_recovery import lifecycle, profile_store, usage_store
        writer = profile_store.Store if name == 'profiles' else usage_store.Store
        end = time.monotonic()+5
        while True:
            try:
                return lifecycle.read_family(self.scope, dict(service_id=self.service_id), writer)
            except BlockingIOError:
                if time.monotonic() >= end: raise
                time.sleep(.025)

    def exercise(self):
        from tools.examples import chat_client
        first = self.ready(1, 'primary')
        self.engine.require(first['cold_probe_used'] and
            first['controller_health']['observation'] == 'UNKNOWN',
            'cold business did not use independent probe with failed monitoring')
        hints = self.engine.read(self.output/'controller-entry.json')
        self.engine.require(hints['continuous_model_hint'] == '0' and hints['cold_device_hint'] == '1',
                            'cold start fabricated prior model ownership')
        body = copy.deepcopy(self.plan['requests']['tool'])
        call = self.chat('cold-tool-call', body)
        message = call['choices'][0]['message']; calls = message['tool_calls']
        document = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
        self.engine.require(call['choices'][0]['finish_reason'] == 'tool_calls' and len(calls) == 1 and
            calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=document), 'actual tool arguments')
        digest = self.engine.sha(ROOT/document)
        self.engine.require(digest == self.plan['sources'][document], 'tool source changed')
        reply = dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(dict(
            path=document, sha256=digest, answer=digest[:12])))
        save(self.output/'tool-execution.json', dict(executions=1,
            source=dict(path=document, content=(ROOT/document).read_text()), reply=reply))
        follow = copy.deepcopy(body)
        follow.update(tool_choice='none', stream=True, stream_options=dict(include_usage=True))
        follow['messages'] += [message, reply]
        raw = self.chat('cold-tool-result-sse', follow, raw_result=True)
        answer = chat_client.read_sse(io.BytesIO(raw))
        self.engine.require(answer.finish_reason == 'stop' and
            answer.message['content'].strip() == digest[:12] and
            not answer.message.get('tool_calls'), 'actual cold tool closure quality')
        save(self.output/'tool-result-completion.json', asdict(answer))
        last = self.sample()
        self.engine.require(all(last[k] == first[k] for k in ('guardian_pid','controller_pid',
            'gateway_pid','model_pid','model_generation')) and last['business_ready'],
            'failed monitoring replaced or stopped healthy business')
        counts = self.engine.read(self.output/'counts.json')['counts']
        self.engine.require(counts['starts'] == 1 and counts['chats'] == 3 and counts['probes'] == 1,
                            'unexpected cold start/probe/business count')
        save(self.output/'business-final.json', dict(status='PASS', actual_tool_executions=1,
            same_model=True, cold_probe_used=True, initial_continuous_model=False,
            final_state=last, counts=counts))

    def run(self):
        original = subprocess.Popen; inherited = descriptors()
        def spawn(argv, **options):
            if 'launcher' in argv and argv[3] == LEGACY['__file__']:
                argv = list(argv); argv[3] = str(Path(__file__).resolve())
                options['pass_fds'] = (*options['pass_fds'], *inherited)
            return original(argv, **options)
        subprocess.Popen = spawn
        try: return LEGACY['Stage'].run(self)
        finally: subprocess.Popen = original


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare','guard','run','launcher','controller'))
    for name in ('package','output','plan','config'): parser.add_argument('--'+name, type=Path)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args); return 0
    plan = json.loads(args.plan.read_text())
    if args.mode == 'guard': return guard(plan, args.plan)
    if args.mode == 'launcher': return launcher(plan, args.plan)
    if args.mode == 'controller': controller_actor(plan, args.config); return 0
    return Stage(plan, args.plan).run()


if __name__ == '__main__': sys.exit(main())
