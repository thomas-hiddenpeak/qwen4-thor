"""Bounded short validation and a separate operator-run 24-hour text soak."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import acceptance as a, package as pkg, service, supervisor as sup
from tools.serve_soak import evidence

PHASE = ROOT/'.q4t-work/serve-soak-20261103'
CPU = PHASE/'cpu'
PROFILES = dict(short=dict(seconds=120, cycles=8, soak_max=240,
                          gpu_max=600, total_max=720, controls=96),
                day=dict(seconds=86400, cycles=5760, soak_max=87300,
                         gpu_max=87720, total_max=87900, controls=24000))
INTERVAL = 15
SOURCES = sorted(set((*a.SOURCES, 'tools/serve_soak/stage.py',
    'tools/serve_soak/evidence.py', 'tools/serve_soak/test_soak.py',
    'tools/serve_pressure/observe.py', 'tools/serve_pressure/pressure.py',
    'tools/serve_backpressure/verify.py', 'tools/evalscope/resource_metrics.py',
    'docs/serve-preflight-v1.schema.json', 'docs/SERVE_SOAK_2026-11-03.md')))


def configure(profile='short', *, apply=True):
    global PROFILE, BASE, PLAN, OUT, SECONDS, CYCLES, SOAK_MAX
    global GPU_MAX, TOTAL_MAX, CONTROL_LIMIT, LIMITS, SUPERVISOR_HTTP_BOUND
    pkg.require(profile in PROFILES, 'unknown duration profile')
    PROFILE, config = profile, PROFILES[profile]
    BASE = PHASE/profile
    PLAN, OUT = BASE/'plan.json', BASE/'execution'
    SECONDS, CYCLES = config['seconds'], config['cycles']
    SOAK_MAX, GPU_MAX, TOTAL_MAX = config['soak_max'], config['gpu_max'], config['total_max']
    CONTROL_LIMIT = config['controls']
    LIMITS = dict(starts=1, generations=CYCLES*5, negatives=0,
                  observations=CONTROL_LIMIT, seconds=TOTAL_MAX)
    SUPERVISOR_HTTP_BOUND = 4*(GPU_MAX//5+3)
    if apply:
        a.BASE, a.PLAN, a.OUT = BASE, PLAN, OUT
        a.LIMITS, a.CONTROL_LIMIT, a.GPU_WINDOW = LIMITS, CONTROL_LIMIT, GPU_MAX


configure(apply=False)


def cpu():
    output = CPU
    output.mkdir(parents=True, exist_ok=False)
    sources = {name:pkg.sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        dest = output/'source'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    log = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromName('tools.serve_soak.test_soak')
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
                 failures=len(result.failures), errors=len(result.errors),
                 sources=sources, model_or_gpu_or_http_started=False)
    pkg.save(output/'result.json', value)
    print(log.getvalue(), flush=True)
    pkg.require(value['passed'], 'CPU failure: preserve originals and fix affected items')
    return {key:value[key] for key in ('passed', 'tests_run', 'failures', 'errors')}


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve previous phase')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed sources before freezing')
    tested = pkg.read(CPU/'result.json')
    pkg.require(tested['passed'], 'CPU acceptance incomplete')
    for name, digest in tested['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU dependency changed: '+name)
    accepted = pkg.read(ROOT/'.q4t-work/serve-inspection-20261103/accepted-package.json')
    receipt = accepted['package']
    manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
    pkg.require(manifest['version'] == 3 and manifest['settings']['restart_limit'] == 0,
                'requires accepted v3 no-restart package')
    release = pkg.read(ROOT/'build/q4t.release.json')
    for name, digest in pkg.read(release['tested_source_manifest']).items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'runtime source changed: '+name)
    token = secrets.token_hex(10)
    BASE.mkdir(parents=True, exist_ok=True)
    pkg.save(PLAN, dict(version=1, profile=PROFILE, source_commit=subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_files=tested['sources'], cpu_result_sha256=pkg.sha(CPU/'result.json'),
        package=receipt, accepted_package_sha256=pkg.sha(
            ROOT/'.q4t-work/serve-inspection-20261103/accepted-package.json'),
        default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        unit='q4t-soak-'+token+'.service', token=token, limits=LIMITS,
        soak_seconds=SECONDS, cycles=CYCLES, interval_seconds=INTERVAL,
        soak_max_seconds=SOAK_MAX, gpu_max_seconds=GPU_MAX,
        resource_envelope=evidence.ENVELOPE, warmup=evidence.WARMUP,
        first_hour_checkpoint_cycles=240, supervisor_http_upper_bound=SUPERVISOR_HTTP_BOUND,
        planned_tool_executions=CYCLES, no_automatic_retry_or_restart=True,
        performance_acceptance=False,
        execution_note='Short validation by agent; long-duration execution reserved for user.'))
    return dict(prepared=True, model_or_gpu_or_http_started=False, plan=str(PLAN))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['profile'] == PROFILE and plan['limits'] == LIMITS and plan['soak_seconds'] == SECONDS and
        plan['cycles'] == CYCLES and plan['interval_seconds'] == INTERVAL and
        plan['soak_max_seconds'] == SOAK_MAX and plan['gpu_max_seconds'] == GPU_MAX and
        plan['resource_envelope'] == evidence.ENVELOPE and plan['warmup'] == evidence.WARMUP,
        'frozen contract differs')
    for name, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'phase source changed: '+name)
    pkg.require(pkg.sha(CPU/'result.json') == plan['cpu_result_sha256'], 'CPU receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['package']['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'],
        'default pair changed')
    return plan


def hourly_checkpoint(cycle, elapsed, rows):
    pkg.require(cycle == len(rows) == 240 and 3600 <= elapsed <= 4500,
                'incomplete first hour')
    return dict(passed=True, cycles=240, successful_requests=1200,
        real_tool_executions=240, seconds=elapsed, envelope=evidence.envelope(rows),
        day_scale_stability_proved=False)


class Run(a.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.mutex = threading.RLock()
        self.capture = self.server = None
        self.resources = []
        self.soak_started = None
        self.request_ids, self.cancel_tokens = set(), set()

    def budget(self, kind=None):
        with self.mutex:
            if self.capture is not None and not self.cleaning:
                self.capture.check()
            super().budget(kind)

    def complete(self, label, body):
        with self.mutex:
            pkg.require(body['request_id'] not in self.request_ids and
                        body['cancel_token'] not in self.cancel_tokens,
                        'request credentials reused')
            self.request_ids.add(body['request_id'])
            self.cancel_tokens.add(body['cancel_token'])
        return super().complete(label, body)

    def launch(self):
        before = self.properties('unit-before')
        pkg.require(before['LoadState'] == 'not-found' and
                    not os.path.lexists(self.runtime_link), 'unit already exists')
        text = service.unit_text(self.package, self.digest, self.run_dir).replace(
            'Description=Qwen4 Thor explicit managed serving generation',
            'Description=q4t-srv14-'+self.plan['token'])
        # Even loss of the validation driver cannot leave an unbounded service.
        text = text.replace('Restart=no\n', f'Restart=no\nRuntimeMaxSec={GPU_MAX}s\n')
        self.unit_file.write_text(text)
        self.command('unit-verify', ['systemd-analyze', '--user', 'verify', self.unit_file])
        self.command('unit-link', ['systemctl', '--user', 'link', '--runtime', self.unit_file])
        pkg.require(self.runtime_link.is_symlink() and self.runtime_link.resolve() == self.unit_file,
                    'unit link ownership changed')
        self.link_owned = True
        self.owned_properties('unit-linked')
        self.unit_owned = True
        self.capture = evidence.Capture(self.run_dir/'lease', OUT/'capture',
                                        lambda: os.kill(os.getpid(), signal.SIGUSR1))
        self.gpu_started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, GPU_MAX)
        self.command('unit-start', ['systemctl', '--user', 'start', self.plan['unit']])
        state = self.wait_state('READY', 1)
        self.bind(state, 'ready')
        props = self.owned_properties('ready-scope')
        self.server = SimpleNamespace(pid=state['backend']['pid'], cgroup=props['ControlGroup'])
        self.expected_start = state['backend']['start_ticks']
        self.capture.attach(self.server, self.expected_start)

    def control_get(self, label, path):
        self.budget('controls')
        response = sup.transport.get(self.port, path)
        pkg.require(response['http_status'] == 200, 'control response failed: '+path)
        a.contracts.readiness(a.contracts.json_object(response['body']),
            response['http_status'], liveness=path == '/livez')
        pkg.save(OUT/(label+'.json'), response)

    def preflight(self, label):
        import http.client
        from jsonschema import Draft202012Validator
        self.budget('controls')
        body = dict(request=a.ordinary(), include_token_ids=True)
        pkg.save(OUT/(label+'-request.json'), body)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request('POST', '/v1/chat/preflight', json.dumps(body),
                               {'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read(1024**2+1)
            pkg.require(len(raw) <= 1024**2, 'preflight byte limit')
            pkg.save(OUT/(label+'-response.json'), dict(status=response.status,
                                                      raw=raw.decode()))
            pkg.require(response.status == 200, 'preflight failed')
            Draft202012Validator(pkg.read(ROOT/'docs/serve-preflight-v1.schema.json')).validate(
                json.loads(raw))
        finally:
            connection.close()

    def sleep_until(self, until):
        while time.monotonic() < until:
            self.budget()
            pkg.require(not (self.run_dir/'terminal.json').exists(), 'service terminated')
            pkg.require(sup.process_identity(self.server.pid)['start_ticks'] == self.expected_start,
                        'backend changed during soak')
            time.sleep(min(.25, max(0, until-time.monotonic())))

    def cycle(self, number):
        label = f'cycle-{number:04d}'
        folder = OUT/label
        folder.mkdir()
        self.preflight(label+'/preflight')
        strict = a.strict_json()
        strict['stream'] = bool(number % 2)
        if strict['stream']:
            strict['stream_options'] = dict(include_usage=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = [pool.submit(self.complete, label+'/'+name, body) for name, body in (
                ('plain', a.ordinary()), ('stream', a.ordinary(True)), ('strict', strict))]
            results = [future.result() for future in pending]
        for result in results[:2]:
            pkg.require(result.finish_reason == 'stop' and
                        result.message['content'].strip() == '710003', 'ordinary quality failure')
        pkg.require(results[2].finish_reason == 'stop' and
                    json.loads(results[2].message['content']) == {'ok':True}, 'strict quality failure')
        self.tools(label+'/project')
        metrics = self.drain(label+'/idle', number*5)
        pkg.require(metrics['q4t_structured_workers_started_total'] == number*3,
                    'unexpected grammar worker accounting')
        self.control_get(label+'/live', '/livez')
        self.control_get(label+'/ready', '/readyz')
        sample = evidence.observe.sample(self.server, self.expected_start)
        pkg.save(folder/'quiescent.json', sample)
        self.resources.append(evidence.values(sample))
        if number > evidence.WARMUP:
            evidence.envelope(self.resources)

    def soak(self):
        self.soak_started = time.monotonic()
        next_cycle = self.soak_started
        for number in range(1, CYCLES+1):
            self.sleep_until(next_cycle)
            started = time.monotonic()
            next_cycle = started+INTERVAL
            self.cycle(number)
            elapsed = time.monotonic()-self.soak_started
            pkg.require(elapsed <= SOAK_MAX, 'fixed soak duration exceeded')
            pkg.save(OUT/f'cycle-{number:04d}/timing.json', dict(
                started=started, ended=time.monotonic(), elapsed_seconds=elapsed))
            pkg.save(OUT/'progress.json', dict(cycle=number, cycles=CYCLES,
                successful_requests=number*5, real_tool_executions=number,
                elapsed_seconds=elapsed, backend_pid=self.server.pid,
                backend_start_ticks=self.expected_start, finished=False))
            if number == 240:
                self.sleep_until(self.soak_started+3600)
                pkg.save(OUT/'first-hour.json', hourly_checkpoint(number,
                    time.monotonic()-self.soak_started, self.resources))
            print(f'cycle {number}/{CYCLES} PASS; {elapsed:.1f}s', flush=True)
        self.sleep_until(self.soak_started+SECONDS)
        elapsed = time.monotonic()-self.soak_started
        pkg.require(SECONDS <= elapsed <= SOAK_MAX, 'fixed duration window incomplete')
        pkg.save(OUT/'soak.json', dict(passed=True, profile=PROFILE, cycles=CYCLES,
            seconds=elapsed, successful_requests=CYCLES*5, real_tool_executions=CYCLES,
            same_backend=dict(pid=self.server.pid, start_ticks=self.expected_start),
            envelope=evidence.envelope(self.resources) if PROFILE == 'day' else None,
            day_scale_stability_proved=PROFILE == 'day', performance_acceptance=False))

    def stop_owned(self):
        if self.capture is not None:
            self.capture.detach()
        self.service_cli('explicit-stop', 'stop')
        deadline = time.monotonic()+50
        while not (self.run_dir/'terminal.json').exists() and time.monotonic() < deadline:
            self.budget()
            time.sleep(.1)
        terminal = pkg.read(self.run_dir/'terminal.json')
        pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
            terminal['guard']['status'] == 'COMPLETED' and terminal['worker']['attempts'] == 1,
            'managed stop did not finish cleanly')
        self.gpu_finished = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, 0)
        time.sleep(10)
        props = self.owned_properties('unit-stopped')
        pkg.require(props['ActiveState'] == 'inactive' and props['MainPID'] == '0' and
                    props['NRestarts'] == '0', 'service restarted')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', performance_acceptance=False,
                      automatically_started=False, persistent_service_installed=False)
        def interrupted(signum, _frame):
            raise evidence.EvidenceAbort('bounded driver interrupted: '+str(signum))
        previous = {sig:signal.signal(sig, interrupted) for sig in
                    (signal.SIGALRM, signal.SIGUSR1, signal.SIGTERM, signal.SIGINT)}
        try:
            a.require_idle(OUT/'gpu-before.json')
            self.launch()
            self.soak()
            self.stop_owned()
            pkg.require(self.gpu_finished-self.gpu_started <= GPU_MAX, 'GPU window exceeded')
            result.update(status='PASS_CURRENT_MANAGED_'+PROFILE.upper()+'_SOAK',
                profile=PROFILE, day_scale_stability_proved=PROFILE == 'day', starts=1,
                successful_requests=CYCLES*5, real_tool_executions=CYCLES)
        except evidence.EvidenceAbort as error:
            result.update(status='ENV_ABORT', error=repr(error))
            raise
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            self.cleaning = True
            signal.signal(signal.SIGUSR1, signal.SIG_IGN)
            cleanup_errors = []
            try:
                if self.capture is not None:
                    self.capture.detach()
                try:
                    self.cleanup()
                    a.require_idle(OUT/'gpu-final.json')
                    pkg.require(a.lock_available(a.guard.common_lock()), 'GPU lease still held')
                except BaseException as error:
                    cleanup_errors.append(repr(error))
                if self.capture is not None:
                    try:
                        self.capture.close()
                    except BaseException as error:
                        cleanup_errors.append(repr(error))
                pkg.require(not cleanup_errors, 'cleanup errors: '+repr(cleanup_errors))
                if result['status'].startswith('PASS'):
                    verified = evidence.guard_archive(OUT/'capture/events.jsonl',
                        pkg.read(self.run_dir/'lease/exit.json'))
                    pkg.save(OUT/'guard-archive.json', verified)
                result['cleanup_confirmed'] = True
                pkg.require(time.monotonic()-self.started <= TOTAL_MAX, 'total window exceeded')
            except BaseException as error:
                result.update(status='ENV_ABORT', cleanup_error=repr(error))
                raise
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                    gpu_window_elapsed=None if self.gpu_finished is None else
                        self.gpu_finished-self.gpu_started,
                    supervisor_http_upper_bound=SUPERVISOR_HTTP_BOUND)
                pkg.save(OUT/'result.json', result)
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'check', 'run'))
    parser.add_argument('--profile', choices=tuple(PROFILES), default='short')
    args = parser.parse_args()
    configure(args.profile)
    value = cpu() if args.action == 'cpu' else prepare() if args.action == 'prepare' else check()
    if args.action == 'run':
        value = Run(value).execute()
    print(json.dumps(value))
