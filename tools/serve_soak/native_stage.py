"""Explicit native short/day soak; no systemd, nginx or background scheduler."""
import argparse
import copy
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CONTRACT = SimpleNamespace(**runpy.run_path(
    str(ROOT/'tools/serve_soak/native_contract.py')))
LEGACY = runpy.run_path(str(ROOT/'tools/serve_recovery/acceptance.py'))
save = LEGACY['save']
old_limits = LEGACY['verify'].__globals__['limits_for']
LEGACY['verify'].__globals__['limits_for'] = lambda name: (
    CONTRACT.configuration(name.removeprefix('native_soak_audio_'), audio=True)['limits']
    if name.startswith('native_soak_audio_') else
    CONTRACT.configuration(name.removeprefix('native_soak_'))['limits']
    if name.startswith('native_soak_') else old_limits(name))


class Budget(LEGACY['Budget']):
    """A bounded counter and last 32 events; original wires are the full log."""
    def reserve(self, kind, label):
        CONTRACT.require(kind in self.limits and kind != 'seconds',
                         'unknown native soak budget')
        with (self.directory/'budget.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = json.loads((self.directory/'counts.json').read_text())
            CONTRACT.require(value['counts'][kind] < self.limits[kind],
                             kind+' native soak budget exhausted')
            value['counts'][kind] += 1
            value['event_tail_only'] = True
            if kind != 'controls':
                value['events'] = (value['events']+[dict(kind=kind,
                    label=label, ordinal=value['counts'][kind],
                    monotonic_ns=time.monotonic_ns())])[-32:]
            save(self.directory/'counts.json', value)
            return value['counts'][kind]


LEGACY['Stage'].__init__.__globals__['Budget'] = Budget


def check(plan):
    CONTRACT.validate_plan(plan)
    manifest = LEGACY['verify'](plan)
    CONTRACT.require(manifest['ingress_mode'] == 'native' and not manifest['tls'],
                     'this soak freezes the native loopback entry')
    if plan['version'] == 2:
        from tools.serve_soak import native_audio
        CONTRACT.require(plan['audio_roundtrip'] == native_audio.binding(plan['package']['package'], manifest),
                         'audio workload differs from sealed package')
    return manifest


def prepare(args):
    bundle, _, _, _, service, engine = LEGACY['load_modules']()
    from tools.serve_recovery import outer_manager
    engine.require(not subprocess.check_output(
        ['git', 'status', '--porcelain'], cwd=ROOT), 'commit reviewed sources')
    out = args.output.resolve()
    engine.require(out.is_relative_to(ROOT/'.q4t-work') and not out.exists(),
                   'new private output required; preserve prior evidence')
    package = args.package.resolve(strict=True)
    refresh = runpy.run_path(str(ROOT/'tools/serve_recovery/cold_acceptance.py'))
    manifest, receipt = refresh['reviewed_package'](package, bundle, engine)
    policy = engine.read(package/'gateway-policy.json')
    clients = engine.read(package/'engine/client-policy.json')['clients']
    alpha = next((c for c in clients if c['id'] == 'alpha'), None)
    engine.require(manifest['ingress_mode'] == 'native' and not manifest['tls']
        and policy['listen_host'] == '127.0.0.1' and alpha is not None and
        alpha['requests_per_minute'] >= 30 and alpha['tokens_per_minute'] >= 6000,
        'qualified loopback alpha quota required; do not raise it for validation')
    audio = {}
    if args.audio:
        from tools.serve_soak import native_audio
        audio['audio_roundtrip'] = native_audio.binding(package, manifest)
    out.mkdir(parents=True, mode=0o700)
    initialized = service.initialize(receipt['package'],
                                     receipt['manifest_sha256'], out/'state')
    outer_manager.initialize(out/'state', package, receipt['manifest_sha256'])
    outer_manager.configure(out/'state', package, receipt['manifest_sha256'],
                            enabled=True)
    sources = subprocess.check_output(['git', 'ls-files', 'tools/serve_soak',
        'tools/serve_recovery', 'tools/serve_service', 'tools/serve_host',
        'tools/serve_ops', 'tools/serve_gateway', 'tools/evalscope',
        'tools/examples', 'tools/serve_pressure', 'tools/serve_backpressure'],
        cwd=ROOT, text=True).splitlines()
    sources = sorted(set(sources) | set(bundle.NATIVE_SOURCES) |
                     {CONTRACT.DOCUMENT})
    prefix = 'native_soak_audio_' if args.audio else 'native_soak_'
    plan = dict(version=2 if args.audio else 1, scenario=prefix+args.profile,
        **CONTRACT.configuration(args.profile, audio=args.audio), **audio, package=receipt,
        output=str(out), service_id=initialized['service_id'],
        sources={n: engine.sha(ROOT/n) for n in sources},
        protected=[dict(path=str(ROOT/n), sha256=engine.sha(ROOT/n)) for n in
            ('build/q4t', 'build/q4t.release.json', 'build/service/manifest.json',
             'build/service.release.json')],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                              cwd=ROOT, text=True).strip(),
        requests=CONTRACT.requests(args.model), automatic_request_replay=False,
        day_run_owner='user', performance_acceptance=False,
        package_reused_unchanged=True, default_changed=False)
    save(out/'plan.json', plan)
    save(out/'handoff.json', dict(plan=str(out/'plan.json'),
        status=[str(package/'bin/q4t'), 'service', 'status', '--state',
                str(out/'state')],
        stop=[str(package/'bin/q4t'), 'service', 'stop', '--state',
              str(out/'state')], persistent_service_installed=False,
        automatically_started=False, user_runs_day=True))
    print(json.dumps(dict(plan=str(out/'plan.json'), model_actions=0,
                          package=receipt, profile=args.profile)))


def controller(plan, config):
    check(plan)
    from tools.serve_recovery import controller as production, core_http
    out = Path(plan['output'])/'execution'
    budget = Budget(out, limits=plan['limits'])
    original_spawn, original_request = subprocess.Popen, core_http.request

    def spawn(argv, **options):
        if str(production.ROOT/'tools/serve_recovery/child.py') in argv:
            budget.reserve('starts', 'native-soak-model')
        elif str(Path(plan['package']['package'])/'bin/q4t-cuda-probe') in argv:
            budget.reserve('probes', 'unexpected-device-probe')
        return original_spawn(argv, **options)

    def request(port, method, path, **options):
        if method == 'POST':
            number = budget.reserve('chats', 'native-soak-self-check')
            result = original_request(port, method, path, **options)
            save(out/('self-check-'+str(number)+'.json'), result)
            return result
        return original_request(port, method, path, **options)

    subprocess.Popen, core_http.request = spawn, request
    sys.argv = [sys.argv[0], '--config', str(config)]
    production.main()


def launcher(plan, path):
    check(plan)
    from tools.serve_recovery import outer_runtime
    from tools.serve_service import package as engine
    package = Path(plan['package']['package'])
    guardian = str(package/'bin/q4t-guardian')
    original = subprocess.Popen

    def spawn(argv, **options):
        if argv[0] == guardian:
            config = Path(argv[argv.index('--config')+1])
            value = engine.read(config)
            production = value['controller']
            value['controller'] = [sys.executable, '-I', '-B', str(Path(
                __file__).resolve()), 'controller', '--plan', str(path),
                '--config', production[-1]]
            engine.save(config, value)
            save(Path(plan['output'])/'execution/wrapper.json',
                 dict(production=production, validation=value['controller']))
        return original(argv, **options)

    subprocess.Popen = spawn
    result = outer_runtime.run(Path(plan['output'])/'state', package,
                               plan['package']['manifest_sha256'], activate=True)
    save(Path(plan['output'])/'execution/launcher-result.json', result)
    return int(result['state'] != 'STOPPED' or not result['cleanup_confirmed'])


class Stage(LEGACY['Stage']):
    def __init__(self, plan, path):
        super().__init__(plan, path)
        self.scope = Path(plan['output'])/'state'
        self.identifiers = set()
        self.first = None

    def initialize_state(self):
        desc = self.service.descriptor(self.scope, self.package,
                                       self.plan['package']['manifest_sha256'])
        self.engine.require(desc['service_id'] == self.plan['service_id'] and
            self.engine.read(self.scope/'ledger.json')['state']['desired'] ==
            'STOPPED', 'private initial service changed')
        return dict(service_id=desc['service_id'], state=str(self.scope))

    def same_model(self):
        value = self.sample()
        self.engine.require(value['business_ready'] and all(value[k] ==
            self.first[k] for k in ('model_pid', 'model_start_ticks',
                                   'model_generation')),
            'soak model became unavailable or changed identity')
        return value

    def pause_until(self, target):
        while time.monotonic() < target:
            self.same_model()
            time.sleep(min(1, max(0, target-time.monotonic())))

    def complete(self, label, body, digest=None):
        body = copy.deepcopy(body)
        body.update(request_id='native-soak-'+uuid.uuid4().hex,
                    cancel_token=uuid.uuid4().hex+uuid.uuid4().hex)
        self.engine.require(body['request_id'] not in self.identifiers,
                            'soak request identity reused')
        self.identifiers.add(body['request_id'])
        value = self.request(label, body)
        result = asdict(value)
        CONTRACT.validate_completion(label.rsplit('-', 1)[1], result, digest)
        return result

    def resources(self):
        state = self.same_model()
        pid = state['model_pid']
        directory = Path('/proc')/str(pid)
        fields = {}
        for line in (directory/'status').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('VmRSS', 'RssAnon', 'Threads'):
                fields[key] = int(value.split()[0])*(1 if key == 'Threads' else 1024)
        result = dict(rss=fields['VmRSS'], anon=fields['RssAnon'],
                      threads=fields['Threads'],
                      fds=len(list((directory/'fd').iterdir())))
        self.engine.require(self.journal.process_start(pid) ==
                            state['model_start_ticks'], 'resource PID changed')
        return result

    def exercise(self):
        self.first = self.wait(lambda s: s['business_ready'] and
                               s['model_generation'] == 1)
        save(self.output/'initial-ready.json', self.first)
        started = time.monotonic()
        rows = []
        for number in range(1, self.plan['cycles']+1):
            prefix = 'cycle-'+str(number).zfill(4)
            self.pause_until(started+(number-1)*self.plan['interval'])
            for label in ('json', 'sse', 'schema'):
                self.complete(prefix+'-'+label, self.plan['requests'][label])
            tool = self.complete(prefix+'-tool', self.plan['requests']['tool'])
            document = ROOT/CONTRACT.DOCUMENT
            content = document.read_bytes()
            body, reply, digest = CONTRACT.answer_request(
                self.plan['requests']['tool'], tool['message'], content)
            self.engine.require(digest == self.plan['sources'][CONTRACT.DOCUMENT],
                                'actual project document changed')
            save(self.output/(prefix+'-tool-executed.json'), dict(executions=1,
                source=dict(path=CONTRACT.DOCUMENT, sha256=digest), reply=reply))
            self.complete(prefix+'-answer', body, digest)
            if number in self.plan.get('audio_cycles', ()):
                from tools.serve_soak import native_audio
                native_audio.exercise(self, prefix, self.plan['audio_roundtrip'])
            count = CONTRACT.business_count(self.plan, number)
            metrics = self.metrics(prefix+'-idle', count+1)
            row = self.resources()
            rows.append(row)
            save(self.output/(prefix+'-idle.json'), dict(metrics=metrics,
                resource=row, model_pid=self.first['model_pid'],
                model_start_ticks=self.first['model_start_ticks']))
            self.pause_until(started+number*self.plan['interval'])
            elapsed = time.monotonic()-started
            save(self.output/'progress.json', dict(cycles=number,
                business_requests=count, seconds=elapsed,
                model_pid=self.first['model_pid'], live_status_required=True))
            if self.plan['profile'] == 'day' and number == 3600//self.plan['interval']:
                hour = CONTRACT.checkpoint('day', number, elapsed, audio=self.plan['version'] == 2)
                hour['envelope'] = CONTRACT.resource_envelope(rows)
                save(self.output/'first-hour.json', hour)
        result = CONTRACT.checkpoint(self.plan['profile'], len(rows),
                                     time.monotonic()-started, final=True, audio=self.plan['version'] == 2)
        result['envelope'] = (CONTRACT.resource_envelope(rows)
                             if self.plan['profile'] == 'day' else None)
        save(self.output/'soak.json', result)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            from tools.serve_recovery import outer_control
            result = outer_control.request(self.service_id, 'stop', timeout=2)
            save(self.output/'outer-stop.json', result)
            self.process.wait(timeout=65)
        super().stop()

    def run(self):
        original = subprocess.Popen

        def spawn(argv, **options):
            if len(argv) > 4 and argv[3] == LEGACY['__file__'] and argv[4] == 'launcher':
                argv = list(argv)
                argv[3] = str(Path(__file__).resolve())
            return original(argv, **options)

        subprocess.Popen = spawn
        try:
            return super().run()
        finally:
            subprocess.Popen = original


def run_guard(plan, path):
    check(plan)
    from tools.release.release import child_subreaper
    with child_subreaper():
        return guarded_run(plan, path)


def guarded_run(plan, path):
    from tools.serve_soak import evidence
    out = Path(plan['output'])
    CONTRACT.require(not (out/'execution').exists() and not (out/'lease').exists(),
                     'preserve original execution; no automatic retry')
    evidence.require_disk_reserve(out)
    command = [sys.executable, '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
        '--output', str(out/'lease'), '--compute-backend', 'nvml',
        '--log-max-bytes', str(evidence.ROTATION_BYTES),
        '--inherit-lease', '--', sys.executable, '-I', '-B',
        str(Path(__file__).resolve()), 'actor', '--plan', str(path)]
    child = None
    capture = evidence.Capture(out/'lease', out/'capture', lambda: None)
    failure = None
    started = time.monotonic()

    def interrupted(*_args):
        raise KeyboardInterrupt('native soak interrupted; owned cleanup only')

    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        with (out/'guard.log').open('xb') as log:
            child = subprocess.Popen(command, stdout=log, stderr=log,
                                     start_new_session=True)
            while child.poll() is None:
                capture.check()
                CONTRACT.require(time.monotonic()-started <=
                    plan['wall_seconds'], 'native soak wall-clock deadline')
                time.sleep(.5)
            CONTRACT.require(child.returncode == 0, 'strict guard did not complete')
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if child is not None and child.poll() is None:
            # Only our unreaped direct guard; it owns its subtree's cleanup.
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=75)
            except subprocess.TimeoutExpired:
                failure = 'strict guard cleanup deadline; preserve ownership'
        capture_error = None
        try:
            capture.close()
        except BaseException as error:
            capture_error = repr(error)
        lease = out/'lease/exit.json'
        environment_abort = capture_error is not None or (lease.exists() and
            json.loads(lease.read_text()).get('status') == 'ENV_ABORT')
        status = ('ENV_ABORT' if environment_abort else
                  'FAILED' if failure is not None else 'PASS')
        save(out/'run.json', dict(status=status, failure=failure,
            capture_error=capture_error, seconds=time.monotonic()-started,
            exit_code=child.returncode if child is not None else None,
            profile=plan['profile'], default_changed=False))
        signal.signal(signal.SIGTERM, previous_term)
        CONTRACT.require(failure is None and capture_error is None,
                         'native soak execution or evidence incomplete')
    from tools.serve_soak import native_report
    native_report.seal(out)
    try:
        verified = native_report.check(out)
    except BaseException as error:
        save(out/'verification-failure.json', dict(error=repr(error)))
        raise
    save(out/'verified.json', verified)
    print(json.dumps(verified, ensure_ascii=False))
    return 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'check', 'run', 'actor',
                                        'launcher', 'controller'))
    parser.add_argument('--package', type=Path, default=ROOT/'build/service')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', choices=tuple(CONTRACT.PROFILES), default='short')
    parser.add_argument('--audio', action='store_true',
                        help='include native speech synthesis followed by transcription during the soak')
    parser.add_argument('--model', default='qwen3.8-flash-next')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    if args.audio and args.mode != 'prepare':
        parser.error('--audio selects the workload during prepare')
    if args.mode == 'prepare':
        prepare(args)
        return 0
    path = args.plan.resolve(strict=True)
    plan = json.loads(path.read_text())
    check(plan)
    if args.mode == 'check':
        # Verify the capture/report import path with the actual sealed
        # production namespaces before any GPU operation.
        from tools.serve_soak import evidence, native_report
        evidence.require_disk_reserve(Path(plan['output']))
        CONTRACT.require(callable(evidence.Capture) and
                         callable(native_report.check), 'handoff helpers missing')
        print(json.dumps(dict(package=plan['package'], profile=plan['profile'],
                              source_verified=True, model_actions=0)))
        return 0
    if args.mode == 'run':
        return run_guard(plan, path)
    if args.mode == 'launcher':
        return launcher(plan, path)
    if args.mode == 'controller':
        controller(plan, args.config)
        return 0
    from tools.release.release import child_subreaper
    with child_subreaper():
        return Stage(plan, path).run()


if __name__ == '__main__':
    raise SystemExit(main())
