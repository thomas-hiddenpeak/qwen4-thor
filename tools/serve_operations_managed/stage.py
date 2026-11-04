"""Bounded production v6 controller and scoped TLS lifecycle acceptance."""
import argparse
import copy
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_operations_listener import cpu
from tools.serve_clients import short as client
from tools.serve_gateway import managed as gateway
from tools.serve_ops import contracts, diagnose
from tools.serve_service import package as pkg, service, supervisor as sup
from tools.serve_service import inspection, lease_custody
from tools.serve_error_params.stage import DAYS

BASE = ROOT / '.q4t-work/serve-operations-managed-20261104'
PLAN, OUT = BASE / 'plan.json', BASE / 'execution'
PREVIOUS = cpu.BASE
PORT, OPS_PORT, TLS_PORT = 18132, 18133, 18452
LIMITS = dict(starts=1, generation_attempts=4, controls=80, seconds=300)
GPU_SECONDS = 240
HERE = 'tools/serve_operations_managed/'


def sources():
    paths = subprocess.check_output(['git', 'ls-files', 'tools'], cwd=ROOT,
                                    text=True).splitlines()
    paths += [str(p.relative_to(ROOT)) for p in (ROOT / HERE).glob('*.py')]
    paths += ['docs/MODEL.md', 'docs/SERVE_OPERATIONS_MANAGED_2026-11-04.md']
    return {name: pkg.sha(ROOT / name) for name in sorted(set(paths))}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior run')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'],
                                           cwd=ROOT), 'commit reviewed sources')
    tested = pkg.read(BASE / 'cpu-01/result.json')
    pkg.require(tested['passed'], 'driver CPU gate')
    for name, digest in tested['sources'].items():
        pkg.require(pkg.sha(ROOT / name) == digest, 'tested driver changed')
    receipt = pkg.read(PREVIOUS / 'accepted-package.json')['package']
    backend = pkg.verify(receipt['package'], receipt['manifest_sha256'])
    pkg.require(backend['version'] == 6 and
                backend['settings']['operations_port'] == OPS_PORT and
                backend['settings']['port'] == PORT and
                backend['settings']['restart_limit'] == 0, 'v6 fixed profile')
    prior = pkg.read(PREVIOUS / 'package-business-fix/plan.json')
    private = BASE / 'private'
    private.mkdir(mode=0o700)
    front = pkg.read(prior['frontend'])
    pkg.require(front['listen_port'] == TLS_PORT and
                front['upstream_operations_port'] == OPS_PORT, 'fixed frontend')
    good = private / 'gateway.json'
    pkg.save(good, front)
    nginx = pkg.read(client.VENDOR)['binary']
    tls_dir = Path(prior['gateway']['package'])
    gateways = []
    rejected = []
    for label, value in (('good-1', front), ('good-2', front),
                         ('missing-operations', {k: v for k, v in front.items()
                                                 if k != 'upstream_operations_port'}),
                         ('wrong-operations', dict(front, upstream_operations_port=18134))):
        policy = private / (label + '.json')
        pkg.save(policy, value)
        selected = gateway.prepare(policy, nginx, tls_dir / 'certificate.pem',
            tls_dir / 'private-key.pem', BASE / ('gateway-' + label), prior['policy'],
            trusted_ca=tls_dir / 'certificate.pem')
        (gateways if label.startswith('good') else rejected).append(selected)
    manifest, _ = gateway.verify(**dict(package=gateways[0]['package'],
                                       digest=gateways[0]['manifest_sha256']))
    preserved = dict(prior['preserved'])
    for path in (ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                 PREVIOUS / 'accepted-package.json', PREVIOUS / 'postflight/result.json',
                 Path(prior['frontend']), Path(prior['policy']), *DAYS):
        preserved[str(path)] = pkg.sha(path)
    release = pkg.read(ROOT / 'build/q4t.release.json')
    plan = dict(package=receipt, gateways=gateways, rejected_gateways=rejected,
        gateway=manifest['gateway'], frontend=str(good), policy=prior['policy'],
        protocol=release['chat_protocol']['protocol_sha256'], limits=LIMITS,
        gpu_seconds=GPU_SECONDS, sources=sources(), preserved=preserved,
        runtime=pkg.read(release['tested_source_manifest']), model_dir=prior['model_dir'],
        model_inputs=prior['model_inputs'], cpu_sha256=pkg.sha(BASE / 'cpu-01/result.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                             text=True).strip(),
        planned=dict(models=1, gateway_starts=2, rejected_gateways=2,
                     responses=4, real_tools=2, public_slow_headers=136),
        supervisor_http_upper_bound=4 * (GPU_SECONDS // 5 + 3),
        long_run=False, actual_lan=False, persistent_installation=False,
        performance_acceptance=False)
    pkg.save(PLAN, plan)
    check()
    return dict(prepared=True, models_started=0, listeners_started=0)


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['gpu_seconds'] == GPU_SECONDS,
                'acceptance bounds changed')
    for name, digest in {**plan['sources'], **plan['runtime']}.items():
        pkg.require(pkg.sha(ROOT / name) == digest, 'source changed: ' + name)
    for name, digest in plan['preserved'].items():
        pkg.require(pkg.sha(name) == digest, 'preserved identity changed: ' + name)
    for name, digest in plan['model_inputs'].items():
        pkg.require(pkg.sha(Path(plan['model_dir']) / name) == digest, 'model changed')
    for path in DAYS:
        pkg.require(not (path.parent / 'execution').exists(), 'user day executed')
    pkg.require(pkg.sha(BASE / 'cpu-01/result.json') == plan['cpu_sha256'], 'CPU changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    for row in plan['gateways'] + plan['rejected_gateways']:
        gateway.verify(row['package'], row['manifest_sha256'])
    return plan


def expected_saturation(state, minimum_update):
    """A current production supervisor sample, not a retained READY label."""
    return (state['state'] == 'READY' and state['updated_unix'] > minimum_update and
            state['attempts'] == 1 and state['restarts'] == 0 and
            state['observation']['operations_port'] == OPS_PORT and
            state['observation']['metrics']['q4t_public_connections'] == 136)


class Run(client.Run):
    def __init__(self, plan):
        client.OUT, client.PORT, client.TLS_PORT, client.LIMITS = (
            OUT / 'business', PORT, TLS_PORT, LIMITS)
        client.OUT.mkdir(mode=0o700)
        super().__init__(plan)
        self.run_dir = OUT / 'managed-run'
        self.package = Path(plan['package']['package'])
        self.processes, self.logs, self.held = {}, [], []
        self.custody = None
        self.current_client = 0
        self.front_index = 0
        self.cleaning = False

    def budget(self, kind):
        if self.custody is not None and not self.cleaning:
            self.custody.check()
        super().budget(kind)

    def spawn(self, name, argv):
        log = (OUT / (name + '.log')).open('xb')
        self.logs.append(log)
        process = subprocess.Popen(list(map(str, argv)), cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        # Registration must precede fallible identity observation.
        self.processes[name] = process
        pkg.save(OUT / (name + '-identity.json'), sup.process_identity(process.pid))
        return process

    def wait(self, predicate, seconds, description):
        until = time.monotonic() + seconds
        while True:
            if self.custody is not None and not self.cleaning:
                self.custody.check()
            value = predicate()
            if value:
                return value
            pkg.require(time.monotonic() < until, description)
            time.sleep(.05)

    def launch(self):
        self.budget('starts')
        signal.setitimer(signal.ITIMER_REAL, GPU_SECONDS)
        self.spawn('controller', [sys.executable, '-I', '-B',
            ROOT / 'tools/serve_service/controller_exec.py', '--plan', PLAN,
            '--output', self.run_dir, '--parent', os.getpid()])
        def ready():
            pkg.require(self.processes['controller'].poll() is None, 'controller exited before READY')
            path = self.run_dir / 'status.json'
            if not path.exists():
                return None
            value = pkg.read(path)
            pkg.require(value['state'] not in ('FAILED', 'STOPPED'), 'supervisor failed startup')
            return value if value['state'] == 'READY' else None
        state = self.wait(ready, 160, 'managed startup deadline')
        control = pkg.read(self.run_dir / 'controller.json')
        pkg.require(control['owner'] == sup.process_identity(self.processes['controller'].pid),
                    'controller owner differs')
        pkg.save(OUT / 'first-ready.json', state)
        target = OUT / 'custody'
        target.mkdir(mode=0o700)
        self.custody = lease_custody.Custody(control['owner'], service.guard.common_lock(),
            target, on_failure=lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.custody.start()
        self.model_owner = state['backend']
        pkg.require(state['observation']['operations_port'] == OPS_PORT, 'operations probe missing')
        live = inspection.inspect(self.run_dir)
        pkg.save(OUT / 'live-inspection.json', live)
        pkg.require(live['run_state'] == 'OWNER_PRESENT' and len(live['listeners']) == 2 and
            any(p.get('port') == OPS_PORT for p in live['listeners']) and
            set(live['errors']) <= {'resources_changed_during_observation',
                                    'run_records_changed_during_observation'}, 'dual live inspection')

    def frontend(self, index):
        row = self.plan['gateways'][index - 1]
        directory = OUT / ('front-' + str(index))
        self.spawn('front-' + str(index), [sys.executable, '-I', '-B',
            Path(row['package']) / 'source/tools/serve_gateway/managed.py', 'run',
            '--package', row['package'], '--manifest-sha256', row['manifest_sha256'],
            '--backend-run', self.run_dir, '--output', directory])
        def ready():
            pkg.require(self.processes['front-' + str(index)].poll() is None, 'gateway exited')
            if not (directory / 'controller.json').exists():
                return None
            state = gateway.status(directory)
            pkg.require(state['state'] not in ('FAILED', 'STOPPED'), 'gateway startup failed')
            return state if state['state'] == 'READY' else None
        state = self.wait(ready, 15, 'gateway readiness deadline')
        control = pkg.read(directory / 'controller.json')
        pkg.require(control['binding']['backend'] == self.model_owner and
                    control['binding']['operations_port'] == OPS_PORT, 'gateway binding differs')
        pkg.save(OUT / ('front-' + str(index) + '-ready.json'), dict(control=control, state=state))
        self.front_index, self.tls = index, True

    def rejected_frontends(self):
        for index, row in enumerate(self.plan['rejected_gateways']):
            self.budget('controls')
            name = 'rejected-' + str(index)
            directory = OUT / name
            process = self.spawn(name, [sys.executable, '-I', '-B',
                Path(row['package']) / 'source/tools/serve_gateway/managed.py', 'run',
                '--package', row['package'], '--manifest-sha256', row['manifest_sha256'],
                '--backend-run', self.run_dir, '--output', directory])
            pkg.require(process.wait(15) == 1, 'mismatched gateway accepted')
            terminal = pkg.read(directory / 'terminal.json')
            pkg.require(terminal['state'] == 'FAILED' and terminal['cleanup_confirmed'] and
                'backend operations port differs' in terminal['error'] and
                not (directory / 'status.json').exists(), 'mismatch reached child startup')
            with gateway.claim(row['package']):
                pass
            pkg.require(pkg.read(self.run_dir / 'status.json')['backend'] == self.model_owner,
                        'rejected gateway affected backend')

    def get_metrics(self, label):
        self.budget('controls')
        value = sup.transport.get(OPS_PORT, '/metrics')
        pkg.save(client.OUT / (label + '-http.json'), value)
        pkg.require(value['http_status'] == 200, 'operations metrics unavailable')
        values, _ = contracts.parse_metrics(value['body'], version=5)
        return values, value['body']

    def complete(self, label, body, client_id=None):
        selected = self.current_client if client_id is None else client_id
        body = copy.deepcopy(body)
        ceiling = 512 if selected == 0 else 64
        body['max_tokens'] = min(body['max_tokens'], ceiling)
        result = super().complete(label, body, selected)
        pkg.require(result.usage['completion_tokens'] <= ceiling, 'client budget exceeded')
        return result

    def pressure(self):
        for _ in range(136):
            sock = socket.create_connection(('127.0.0.1', PORT), 1)
            self.held.append(sock)
            sock.sendall(b'GET /metrics HTTP/1.1\r\nHost:')
        started = time.time()
        def sampled():
            state = pkg.read(self.run_dir / 'status.json')
            pkg.require(state['backend'] == self.model_owner, 'backend replaced under pressure')
            return state if expected_saturation(state, started) else None
        state = self.wait(sampled, 12, 'no new READY sample at public capacity')
        pkg.save(OUT / 'saturated-supervisor.json', state)
        for _ in range(5):
            self.budget('controls')
        diagnostic = diagnose.snapshot(PORT, self.model_owner['pid'],
            self.package / 'q4t.release.json', operations_port=OPS_PORT,
            client_policy_path=self.plan['policy'])
        pkg.save(OUT / 'saturated-diagnostic.json', diagnostic)
        pkg.require(diagnostic['state'] == 'HEALTHY' and
                    diagnostic['identity']['state'] == 'verified', 'diagnostic unavailable')
        for label, path in (('models', '/v1/models'), ('capabilities', '/v1/capabilities')):
            self.request('saturated-' + label, path=path, method='GET')
        pkg.require(self.get_metrics('saturated-final')[0]['q4t_public_connections'] == 136,
                    'pressure disappeared before observations')
        self.close_sockets()
        self.idle('pressure-recovery')

    def close_sockets(self):
        for sock in self.held:
            sock.close()
        self.held.clear()
        for connection in self.sockets:
            connection.close()

    def stop_front(self, index):
        name = 'front-' + str(index)
        if name not in self.processes:
            return
        process = self.processes[name]
        directory = OUT / name
        if (directory / 'controller.json').exists():
            gateway.stop(directory)
        elif process.poll() is None:
            process.terminate()
        code = process.wait(50)
        terminal = pkg.read(directory / 'terminal.json')
        pkg.require(code == 0 and terminal['state'] == 'STOPPED' and
                    terminal['cleanup_confirmed'], 'gateway cleanup failed')
        with gateway.claim(self.plan['gateways'][index - 1]['package']):
            pass
        pkg.require(not inspection.listeners(TLS_PORT), 'TLS listener remains')

    def stop_backend(self):
        process = self.processes.get('controller')
        if process is None:
            return
        if (self.run_dir / 'controller.json').exists():
            service.stop(self.run_dir)
        elif process.poll() is None:
            process.terminate()
        code = process.wait(50)
        terminal = pkg.read(self.run_dir / 'terminal.json')
        pkg.require(code == 0 and terminal['state'] == 'STOPPED' and
            terminal['cleanup_confirmed'] and terminal['guard']['status'] == 'COMPLETED' and
            terminal['worker']['attempts'] == 1 and terminal['worker']['restarts'] == 0,
            'managed terminal not clean')

    def cleanup(self):
        self.cleaning = True
        self.close_sockets()
        if not self.processes:
            return
        errors = []
        for index in (2, 1):
            try:
                self.stop_front(index)
            except BaseException as error:
                errors.append(repr(error))
        for name, process in self.processes.items():
            if name.startswith('rejected-') and process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except BaseException as error:
                    errors.append(repr(error))
        try:
            self.stop_backend()
        except BaseException as error:
            errors.append(repr(error))
        try:
            client.a.require_idle(OUT / 'final-gpu.json')
            pkg.require(all(p.poll() is not None for p in self.processes.values()),
                        'owned direct process remains')
            pkg.require(not any(inspection.listeners(p) for p in (PORT, OPS_PORT, TLS_PORT)),
                        'owned listener remains')
        except BaseException as error:
            errors.append(repr(error))
        if self.custody is not None:
            try:
                self.custody.finish(cleanup_confirmed=not errors)
            except BaseException as error:
                errors.append(repr(error))
        pkg.require(not errors, 'owned cleanup errors: ' + repr(errors))

    def execute(self):
        pkg.save(OUT / 'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', long_run=False, performance_acceptance=False)
        def interrupted(signum, _):
            raise RuntimeError('ENV_ABORT: driver interrupted: ' + str(signum))
        handlers = {sig: signal.signal(sig, interrupted) for sig in
                    (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        try:
            client.a.require_idle(OUT / 'initial-gpu.json')
            self.launch()
            self.rejected_frontends()
            self.frontend(1)
            self.pressure()
            self.current_client = 0
            self.tools('project-alpha')
            self.idle('alpha-idle')
            self.stop_front(1)
            pkg.require(pkg.read(self.run_dir / 'status.json')['backend'] == self.model_owner,
                        'gateway stop changed backend')
            self.frontend(2)
            self.current_client = 1
            self.tools('project-beta')
            metrics = self.idle('final-idle')
            pkg.save(OUT / 'final-metrics.json', metrics)
            pkg.require(self.successes == metrics['q4t_requests_success_total'] == 4 and
                metrics['q4t_requests_error_total'] == metrics['q4t_requests_aborted_total'] == 0 and
                metrics['q4t_structured_workers_started_total'] ==
                metrics['q4t_structured_workers_reaped_total'] == 4, 'business accounting')
            result.update(status='PASS_MANAGED_OPERATIONS_SHORT', successful_responses=4,
                          real_tools=2, gateway_starts=2, rejected_gateways=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED',
                          error=repr(error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                self.cleanup()
                if self.processes:
                    time.sleep(10)
                    stopped = inspection.inspect(self.run_dir)
                    pkg.save(OUT / 'stopped-inspection.json', stopped)
                    pkg.require(stopped['cleanup_observed'] and
                                len(list(self.run_dir.glob('attempt-*.json'))) == 1,
                                'stopped inspection or no-reactivation failed')
                pkg.require(time.monotonic() - self.started < LIMITS['seconds'], 'wall budget')
                check()
                result['cleanup_confirmed'] = True
            except BaseException as error:
                result.update(status='ENV_ABORT' if result['status'] == 'ENV_ABORT' or
                              'ENV_ABORT' in str(error) else 'FAILED', cleanup_error=repr(error))
                raise
            finally:
                lease = self.run_dir / 'lease/exit.json'
                if lease.exists() and pkg.read(lease)['status'] in ('ENV_ABORT', 'LEASE_BUSY', 'RECOVERY_REQUIRED'):
                    result['status'] = 'ENV_ABORT'
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                for stream in self.logs:
                    stream.close()
                result.update(counts=self.counts, seconds=time.monotonic() - self.started)
                pkg.save(OUT / 'result.json', result)
        return result


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'run'))
    action = parser.parse_args().action
    if action == 'prepare':
        value = prepare()
    elif action == 'check':
        check()
        value = dict(checked=True)
    else:
        plan = check()
        OUT.mkdir(mode=0o700)
        with sup.processes.child_subreaper():
            value = Run(plan).execute()
    print(json.dumps(value))
