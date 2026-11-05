"""Current release, optional host envelope and active memory-stop qualification."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_deploy import profile
from tools.serve_operations_managed import stage as base
from tools.serve_managed_profile import stage as host
from tools.serve_service import package as pkg, resources, inspection
from tools.serve_service.memory_acceptance import protected_terminal
from tools.serve_pressure import pressure
from tools.serve_lifecycle.verify import terminal as stream_terminal
from jsonschema import Draft202012Validator

BASE = ROOT / '.q4t-work/serve-bounded-deploy-20261105'
PLAN, OUT = BASE / 'plan.json', BASE / 'execution'
OLD = ROOT / '.q4t-work/serve-current-managed-20261105/plan.json'
LIMITS = dict(starts=1, generation_attempts=6, controls=120, seconds=420)
GPU_SECONDS, FLOOR_MIB = 300, 30720
CPU = 'cpu-02'
POLICY = resources.policy(16384, 20480, 512)
ORIGINAL_CHECK = base.check


def configure():
    base.BASE, base.PLAN, base.OUT = BASE, PLAN, OUT
    base.LIMITS, base.GPU_SECONDS = LIMITS, GPU_SECONDS
    base.HERE = 'tools/serve_deploy/'
    base.check = check
    host.OUT = OUT


def sources():
    values = base.sources()
    for name in ('docs/SERVE_BOUNDED_DELIVERY_2026-11-05.md',
                 'tools/serve_deploy/README.md'):
        values[name] = pkg.sha(ROOT / name)
    return values


def cpu():
    configure()
    target = BASE / CPU
    target.mkdir(parents=True, mode=0o700)
    before = sources()
    suite = unittest.defaultTestLoader.loadTestsFromName('tools.serve_deploy.test_profile')
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (target / 'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful() and not result.skipped,
                 tests_run=result.testsRun, sources=before, models=0, http=0)
    pkg.save(target / 'result.json', value)
    pkg.require(value['passed'] and before == sources(), log.getvalue())
    return {key: value[key] for key in ('passed', 'tests_run', 'models', 'http')}


def ref(row):
    return profile.reference(row['package'], row['manifest_sha256'])


def prepare():
    configure()
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve previous attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed sources first')
    tested = pkg.read(BASE / CPU / 'result.json')
    pkg.require(tested['passed'] and tested['sources'] == sources(), 'CPU gate')
    old = pkg.read(OLD)
    previous = pkg.verify(**dict(package=old['package']['package'],
                                digest=old['package']['manifest_sha256']))
    private = BASE / 'private'
    private.mkdir(mode=0o700)
    policy = pkg.read(old['policy'])
    policy['clients'][0]['max_output_tokens'] = 4096
    pkg.save(private / 'clients.json', policy)
    front = pkg.read(old['frontend'])
    pkg.save(private / 'gateway.json', front)
    cert, key = base.client.certificate(private)
    backend = pkg.prepare(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
        old['model_dir'], BASE / 'backend-package', port=base.PORT,
        operations_port=base.OPS_PORT, restart_limit=0,
        critical_memory_mib=FLOOR_MIB, client_policy=private / 'clients.json',
        host_resources=POLICY)
    manifest = pkg.verify(backend['package'], backend['manifest_sha256'])
    pkg.require(all(d == previous['files'].get(n) for n, d in manifest['files'].items()
        if n != 'client-policy.json'), 'unexpected backend file delta')
    frontend = base.gateway.prepare(private / 'gateway.json',
        pkg.read(base.client.VENDOR)['binary'], cert, key,
        BASE / 'gateway-package', private / 'clients.json', trusted_ca=cert)
    outer, _ = base.gateway.verify(frontend['package'], frontend['manifest_sha256'])
    bundle = profile.prepare(BASE / 'deployment', ref(backend), ref(frontend),
                             ref(old['package']), ref(old['gateways'][0]))
    release = pkg.read(ROOT / 'build/q4t.release.json')
    preserved = {str(p): pkg.sha(p) for p in
        (ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', OLD, *base.DAYS)}
    value = dict(package=backend, gateways=[frontend], rejected_gateways=[],
        gateway=outer['gateway'], frontend=str(private / 'gateway.json'),
        policy=str(private / 'clients.json'), protocol=release['chat_protocol']['protocol_sha256'],
        limits=LIMITS, gpu_seconds=GPU_SECONDS, sources=sources(), preserved=preserved,
        runtime=pkg.read(release['tested_source_manifest']), model_dir=old['model_dir'],
        model_inputs=old['model_inputs'], cpu_sha256=pkg.sha(BASE / 'cpu-01/result.json'),
        final_cpu_sha256=pkg.sha(BASE / CPU / 'result.json'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                             text=True).strip(),
        deployment=bundle, rollback=old['package'], host_resources=POLICY,
        planned=dict(models=1, responses=4, real_tools=2, interrupted_streams=2),
        long_run=False, actual_lan=False, performance_acceptance=False)
    pkg.save(PLAN, value)
    check()
    return dict(prepared=True, package=backend, deployment=bundle, models=0)


def check():
    value = ORIGINAL_CHECK()
    pkg.require(pkg.sha(BASE / CPU / 'result.json') == value['final_cpu_sha256'], 'final CPU changed')
    manifest = pkg.verify(value['package']['package'], value['package']['manifest_sha256'])
    pkg.require(manifest['host_resources'] == POLICY and
        manifest['settings']['critical_memory_mib'] == FLOOR_MIB and
        manifest['settings']['restart_limit'] == 0, 'combined resource policy')
    profile.check(value['deployment']['profile'], value['deployment']['sha256'])
    return value


def failed_status(previous, error):
    return 'ENV_ABORT' if previous == 'ENV_ABORT' or 'ENV_ABORT' in str(error) else 'FAILED'


class Stream:
    def __init__(self, run, label, body, client):
        self.label, self.body = label, body
        self.content = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.capture,
            args=(run, client), daemon=True)
        self.thread.start()

    def capture(self, run, client):
        connection = None
        try:
            connection, response = run.begin(self.label, self.body, client=client)
            pkg.require(response.status == 200 and
                response.headers.get_content_type() == 'text/event-stream', 'SSE HTTP status')
            size = 0
            with (base.client.OUT / (self.label + '-wire.bin')).open('xb', buffering=0) as out:
                while line := response.readline(65537):
                    size += len(line)
                    pkg.require(size <= 8*1024**2 and len(line) <= 65536, 'SSE size bound')
                    out.write(line)
                    if line.startswith(b'data: {'):
                        value = json.loads(line[6:])
                        if any(c.get('delta', {}).get('content') for c in value.get('choices', [])):
                            self.content.set()
        except BaseException as error:
            self.error = repr(error)
        finally:
            if connection is not None:
                connection.close()


class Run(host.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.calls = []
        self.protected = False

    def wait_active(self, call):
        until = time.monotonic() + 20
        while not call.content.is_set():
            self.custody.check()
            pkg.require(call.thread.is_alive() and time.monotonic() < until,
                        'active output missing: ' + str(call.error))
            time.sleep(.05)

    def trip(self):
        a = base.client.a
        body = a.make_request([dict(role='user', content=
            '从1开始逐行写出整数直到10000，每行一个，不要省略，不要解释，持续输出。')],
            [], stream=True, max_tokens=4096, timeout_ms=180000)
        active = Stream(self, 'active', body, 0)
        self.calls.append(active)
        self.wait_active(active)
        body = a.strict_json()
        body.update(stream=True, stream_options=dict(include_usage=True), max_tokens=64)
        queued = Stream(self, 'queued', body, 1)
        self.calls.append(queued)
        until = time.monotonic() + 8
        while True:
            values = self.get_metrics('queue-' + str(self.counts['controls']))[0]
            if (values['q4t_queued_requests'] == 1 and
                values['q4t_structured_workers_active'] == 1 and
                values['q4t_structured_preparing'] == 0 and values['q4t_seq_slots_free'] == 0):
                break
            pkg.require(time.monotonic() < until and all(c.thread.is_alive() for c in self.calls),
                        'active/grammar queue boundary missing')
            time.sleep(.2)
        record = pkg.read(self.run_dir / 'resources.json')
        pkg.save(OUT / 'before-pressure.json', dict(metrics=values,
            host=resources.sample(record), monotonic=time.monotonic(), memory=pressure.memory()))
        self.capture.close()
        self.capture = None
        with_pressure = pressure.Pressure(OUT)
        try:
            floor = FLOOR_MIB * pressure.MIB
            row = with_pressure.command('status')
            pkg.require(row['after']['MemAvailable'] >= floor, 'already below stop floor')
            for _ in range(pressure.MAX_BYTES // pressure.CHUNK):
                self.custody.check()
                pkg.require(all(c.thread.is_alive() for c in self.calls), 'requests ended before pressure')
                row = with_pressure.command('add')
                if row['after']['MemAvailable'] < floor - pressure.CHUNK:
                    break
            pkg.require(row['after']['MemAvailable'] < floor - pressure.CHUNK,
                        'bounded pressure did not cross stop floor')
            pkg.save(OUT / 'pressure-trigger.json', row)
            until = time.monotonic() + 45
            while not (self.run_dir / 'terminal.json').exists():
                self.custody.check()
                pkg.require(time.monotonic() < until, 'protected stop deadline')
                with_pressure.command('status')
                time.sleep(.2)
        finally:
            with_pressure.close()
        terminal = pkg.read(self.run_dir / 'terminal.json')
        protected_terminal(terminal)
        self.protected = True
        validator = Draft202012Validator(pkg.read(ROOT / 'docs/serve-transport-v1.schema.json'))
        for call in self.calls:
            call.thread.join(5)
            pkg.require(not call.thread.is_alive() and call.error is None, 'stream capture failed')
            value = stream_terminal((base.client.OUT / (call.label + '-wire.bin')).read_bytes(),
                                    graceful=True, validator=validator)
            pkg.save(OUT / (call.label + '-verified.json'), value)
        log = (self.run_dir / 'backend.log').read_text()
        pkg.require('shutdown complete (0 in-flight remaining)' in log, 'backend shutdown incomplete')
        for call in self.calls:
            ends = [s for s in log.splitlines() if 'event=end ' in s and
                    'id=' + call.body['request_id'] + ' ' in s]
            pkg.require(len(ends) == 1 and 'reason=server_shutdown' in ends[0], 'shutdown accounting')
        reaped = re.findall(r'grammar worker reaped=(\d+) ', log)
        pkg.require(len(reaped) == 5 and all(n == '1' for n in reaped), 'grammar not fully reaped')
        final = pkg.read(self.run_dir / 'resources-final.json')
        pkg.require(final['payload_removed'] and final['violation'] is None and
                    terminal['host_resource_cleanup']['scope_removed'], 'host cleanup')
        pkg.save(OUT / 'protected-stop.json', dict(passed=True, terminal=terminal, grammar_reaped=5))

    def stop_front(self, index):
        name = 'front-' + str(index)
        if name not in self.processes:
            return
        process = self.processes[name]
        folder = OUT / name
        if not self.protected:
            return super().stop_front(index)
        # Dependency loss is an expected gateway failure, never a normal stop.
        pkg.require(process.wait(50) == 1, 'gateway did not fail on backend loss')
        value = pkg.read(folder / 'terminal.json')
        pkg.require(value['state'] == 'FAILED' and value['cleanup_confirmed'] and
                    not value['automatic_reactivation'], 'gateway dependency cleanup')
        with base.gateway.claim(self.plan['gateways'][index-1]['package']):
            pass
        pkg.require(not inspection.listeners(base.TLS_PORT), 'TLS listener remains')

    def stop_backend(self):
        process = self.processes.get('controller')
        if process is None:
            return
        terminal = self.run_dir / 'terminal.json'
        if terminal.exists() and pkg.read(terminal).get('state') == 'FAILED':
            pkg.require(process.wait(50) == 1, 'protected controller return code')
            protected_terminal(pkg.read(terminal))
        else:
            super().stop_backend()

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            for call in self.calls:
                call.thread.join(5)
            pkg.require(not any(c.thread.is_alive() for c in self.calls), 'client thread retained')

    def execute(self):
        result = dict(status='FAILED', physical_ram_hard_limit=False, long_run=False)
        pkg.save(OUT / 'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        def interrupted(signum, _):
            raise RuntimeError('ENV_ABORT: driver interrupted: ' + str(signum))
        handlers = {sig: signal.signal(sig, interrupted) for sig in
                    (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        try:
            base.client.a.require_idle(OUT / 'initial-gpu.json')
            self.launch()
            self.frontend(1)
            for index, label in enumerate(('alpha', 'beta')):
                self.current_client = index
                self.tools('project-' + label)
                self.idle(label + '-idle')
            metrics = self.get_metrics('business-final')[0]
            pkg.require(metrics['q4t_requests_success_total'] == 4 and
                metrics['q4t_structured_workers_reaped_total'] == 4 and
                metrics['q4t_cuda_quota_live_bytes'] > 0, 'business/resource accounting')
            pkg.save(OUT / 'business-final.json', metrics)
            self.trip()
            result.update(status='PASS_BOUNDED_DEPLOYMENT', responses=4, real_tools=2,
                          shutdown_streams=2, grammar_reaped=5)
        except BaseException as error:
            result.update(error=repr(error), status=failed_status(result['status'], error))
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                self.cleanup()
                time.sleep(10)
                observed = inspection.inspect(self.run_dir)
                pkg.save(OUT / 'stopped-inspection.json', observed)
                st = base.service.guard.common_lock().stat()
                pkg.require(observed['cleanup_observed'] and
                    observed['lock']['state'] == 'AVAILABLE' and
                    (st.st_dev, st.st_ino) == (66305, 3294067), 'final resource release')
                pkg.require(time.monotonic() - self.started < LIMITS['seconds'], 'phase wall budget')
                check()
                result['cleanup_confirmed'] = True
            except BaseException as error:
                result.update(status=failed_status(result['status'], error), cleanup_error=repr(error))
                raise
            finally:
                lease = self.run_dir / 'lease/exit.json'
                if lease.exists() and pkg.read(lease)['status'] in (
                        'ENV_ABORT', 'LEASE_BUSY', 'RECOVERY_REQUIRED'):
                    result['status'] = 'ENV_ABORT'
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                for stream in self.logs:
                    stream.close()
                result.update(counts=self.counts, seconds=time.monotonic() - self.started)
                pkg.save(OUT / 'result.json', result)
        return result


def run():
    configure()
    plan = check()
    OUT.mkdir(mode=0o700)
    with base.sup.processes.child_subreaper():
        return Run(plan).execute()


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'check', 'run'))
    configure()
    print(json.dumps(globals()[parser.parse_args().action]()))
