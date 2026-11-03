"""One exclusive lease: real alert delivery, state reuse and model recovery."""
import argparse
import datetime
import http.client
import io
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_notify import notify as n, cpu, receiver
from tools.serve_notify.test_notify import dependencies, private_inputs
from tools.serve_service import acceptance as a

BASE = cpu.BASE
PLAN, OUT = BASE/'plan.json', BASE/'execution'
LIMITS = dict(starts=2, monitor_starts=3, generations=4, negatives=0,
              observations=256, seconds=420)
GPU_SECONDS = 360
SOURCES = sorted(set((*cpu.SOURCES, *a.SOURCES, 'tools/serve_notify/stage.py',
    'tools/serve_notify/receiver.py', 'tools/serve_notify/owned_exec.py',
    'tools/serve_notify/test_stage.py', 'docs/SERVE_NOTIFY_GPU_2026-11-04.md')))


def configure():
    a.BASE, a.PLAN, a.OUT = BASE, PLAN, OUT
    a.LIMITS, a.CONTROL_LIMIT, a.GPU_WINDOW = LIMITS, 256, GPU_SECONDS


def driver_cpu():
    output = n.pkg.private_directory(BASE/'driver-cpu')
    sources = {name:n.pkg.sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        dest = output/'source'/name; dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    log = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromName('tools.serve_notify.test_stage')
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (output/'tests.log').write_text(log.getvalue())
    imports = []
    for name in ('stage.py', 'owned_exec.py'):
        check = subprocess.run([sys.executable, '-I', '-B',
            ROOT/'tools/serve_notify'/name, '--help'], capture_output=True, text=True, timeout=15)
        value = dict(name=name, returncode=check.returncode, stdout=check.stdout, stderr=check.stderr)
        imports.append(value); n.pkg.save(output/(name+'.json'), value)
    value = dict(passed=result.wasSuccessful() and all(v['returncode'] == 0 for v in imports),
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        isolated_imports=len(imports), sources=sources, model_gpu_http_started=False)
    n.pkg.save(output/'result.json', value); print(log.getvalue())
    n.require(value['passed'], 'driver CPU failed; preserve originals')
    return {k:v for k,v in value.items() if k != 'sources'}


def prepare():
    n.require(not PLAN.exists() and not OUT.exists(), 'preserve original execution')
    for name in SOURCES:
        n.require(subprocess.check_output(['git', 'show', 'HEAD:'+name]) ==
                  (ROOT/name).read_bytes(), 'commit reviewed source first: '+name)
    for folder in ('cpu', 'driver-cpu'):
        value = n.pkg.read(BASE/folder/'result.json')
        n.require(value['passed'], 'CPU gate')
        for name, digest in value['sources'].items():
            n.require(n.pkg.sha(ROOT/name) == digest, 'CPU source changed: '+name)
    private = n.pkg.private_directory(BASE/'private')
    private_inputs(private)
    notification = n.prepare(private/'policy-private.json', dependencies(), BASE/'package')
    n.init_state(notification['package'], notification['manifest_sha256'], BASE/'state')
    backend = n.pkg.read(ROOT/'.q4t-work/serve-inspection-20261103/accepted-package.json')['package']
    n.pkg.verify(backend['package'], backend['manifest_sha256'])
    release = n.pkg.read(ROOT/'build/q4t.release.json')
    for name, digest in n.pkg.read(release['tested_source_manifest']).items():
        n.require(n.pkg.sha(ROOT/name) == digest, 'runtime source changed')
    n.pkg.save(PLAN, dict(version=1, limits=LIMITS, gpu_seconds=GPU_SECONDS,
        package=backend, notification=notification,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        sources={name:n.pkg.sha(ROOT/name) for name in SOURCES},
        cpu={name:n.pkg.sha(BASE/name/'result.json') for name in ('cpu', 'driver-cpu')},
        unit='unused-direct-notification-acceptance.service',
        default_sidecar_sha256=n.pkg.sha(ROOT/'build/q4t.release.json'),
        planned_model_requests=4, planned_model_starts=2, receiver_attempt_limit=64,
        performance_acceptance=False, external_recipient=False, long_test_started=False))
    return dict(prepared=True, model_http_started=False, limits=LIMITS)


def check():
    value = n.pkg.read(PLAN)
    n.require(value['limits'] == LIMITS and value['gpu_seconds'] == GPU_SECONDS,
              'frozen budget changed')
    for name, digest in value['sources'].items():
        n.require(n.pkg.sha(ROOT/name) == digest, 'source changed: '+name)
    for name, digest in value['cpu'].items():
        n.require(n.pkg.sha(BASE/name/'result.json') == digest, 'CPU record changed')
    n.pkg.verify(value['package']['package'], value['package']['manifest_sha256'])
    n.verify(value['notification']['package'], value['notification']['manifest_sha256'])
    n.require(n.pkg.sha(ROOT/'build/q4t') == value['package']['binary_sha256'] and
              n.pkg.sha(ROOT/'build/q4t.release.json') == value['default_sidecar_sha256'],
              'default pair changed')
    return value


def require_guard():
    lock = a.guard.common_lock()
    current = n.pkg.read(lock)
    n.require(not a.lock_available(lock) and current['state'] == 'held' and
              current['output'] == str(OUT/'lease') and
              current['pid'] == os.getppid(), 'whole-stage guard required')


def utc(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


class Run(a.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.children, self.logs, self.owners = {}, {}, {}
        self.receiver = None
        self.model_starts = self.monitor_starts = self.api_index = 0
        self.notify = Path(plan['notification']['package'])
        self.notify_digest = plan['notification']['manifest_sha256']
        self.notify_manifest = n.verify(self.notify, self.notify_digest)
        self.parent = n.sup.process_identity(os.getpid())
        self.role_runs = {}

    def budget(self, kind=None):
        if not self.cleaning:
            n.require(time.monotonic()-self.started < 300, 'business window exceeded')
        super().budget(kind)

    def spawn(self, label, argv):
        self.budget()
        n.require(label not in self.children, 'duplicate child label')
        command = [sys.executable, '-I', '-B', ROOT/'tools/serve_notify/owned_exec.py',
                   '--parent', json.dumps(self.parent), '--', *argv]
        n.pkg.save(OUT/(label+'-command.json'), dict(argv=list(map(str, command))))
        child = subprocess.Popen(list(map(str, command)), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            env=dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', HOME=str(Path.home())), umask=0o077)
        self.children[label] = child
        self.owners[label] = n.sup.process_identity(child.pid)
        log = n.sup.RotatingLog(OUT/(label+'.log'), 4*1024**2)
        self.logs[label] = log; log.start(child.stdout)
        n.pkg.save(OUT/(label+'-owner.json'), self.owners[label])

    def wait(self, predicate, timeout, label):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            self.budget()
            value = predicate()
            if value: return value
            time.sleep(.25)
        raise RuntimeError('deadline: '+label)

    def api(self, label, port, path, body=None, *, raw=False):
        self.budget('controls')
        self.api_index += 1
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        receipt = dict(label=label, port=port, path=path, method='GET' if body is None else 'POST')
        try:
            conn.request(receipt['method'], path,
                None if body is None else json.dumps(body),
                {} if body is None else {'Content-Type':'application/json'})
            response = conn.getresponse(); data = response.read(4*1024**2+1)
            n.require(len(data) <= 4*1024**2, 'API response bound')
            receipt.update(status=response.status, body=data.decode())
            n.require(response.status in (200, 202), 'API status: '+label)
            return data.decode() if raw else json.loads(data)
        except BaseException as error:
            receipt['error'] = repr(error); raise
        finally:
            conn.close(); n.pkg.save(OUT/f'api-{self.api_index:03d}.json', receipt)

    def start_monitor(self, role, index):
        n.require(self.monitor_starts < LIMITS['monitor_starts'], 'monitor start bound')
        self.monitor_starts += 1; label = role+'-'+str(index)
        output = OUT/label; self.role_runs[label] = output
        argv = [sys.executable, '-I', '-B',
            self.notify/'source/tools/serve_notify/notify.py', 'run',
            '--package', self.notify, '--manifest-sha256', self.notify_digest,
            '--role', role, '--state', BASE/'state', '--output', output]
        self.spawn(label, argv)
        def listening():
            n.require(self.children[label].poll() is None, 'monitor stopped before listening')
            path = output/'status.json'
            return n.m.read(path) if path.exists() else None
        value = self.wait(listening, 30, 'monitor listening')
        control = n.m.read(output/'controller.json'); child = value['child']
        n.require(control['owner'] == self.owners[label], 'monitor controller identity')
        inherited = []
        for path in Path(f"/proc/{child['pid']}/fd").iterdir():
            try: info = path.stat()
            except FileNotFoundError: continue
            if [info.st_dev, info.st_ino] == control['claim_identity']: inherited.append(path.name)
        n.require(inherited, 'monitor child lost state claim')
        n.pkg.save(OUT/(label+'-claim.json'), dict(child=child, descriptors=inherited,
                                                  claim_identity=control['claim_identity']))
        # The LISTENING observer is the actual child, not its Python manager.
        end = time.monotonic()+25
        while True:
            self.budget()
            try: self.api(label+'-ready', self.notify_manifest['ports'][role], '/-/ready', raw=True); break
            except (OSError, RuntimeError):
                n.require(time.monotonic() < end and self.children[label].poll() is None,
                          'monitor ready failed')
                time.sleep(1)

    def stop_child(self, label):
        child = self.children[label]
        if child.poll() is None:
            n.require(n.sup.signal_owner(self.owners[label], signal.SIGTERM), 'owned stop failed')
        n.sup.processes.stop_children({label:child}, {label:self.logs[label].thread},
                                     grace=25, kill_grace=5)
        n.require(self.logs[label].error is None, 'driver log failure')
        if label in self.role_runs:
            terminal = n.m.read(self.role_runs[label]/'terminal.json')
            n.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
                      terminal['restarts'] == 0 and terminal['child_returncode'] == 0,
                      'monitor did not stop cleanly')
        n.pkg.save(OUT/(label+'-exit.json'), dict(returncode=child.returncode,
                                                group_gone=True))

    def start_model(self):
        n.require(self.model_starts < 2, 'model start budget')
        self.model_starts += 1; label = 'model-'+str(self.model_starts)
        self.spawn(label, n.pkg.command(self.package, self.manifest))
        def ready():
            n.require(self.children[label].poll() is None, 'model exited before readiness')
            return ((OUT/(label+'.log')).exists() and
                    'serving on port' in (OUT/(label+'.log')).read_text())
        self.wait(ready, 90, 'model ready')
        self.drain(label+'-ready', 0)
        n.pkg.save(OUT/(label+'-listener.json'), n.identity.process(self.children[label].pid, self.port))
        return label

    def callback(self, name, status, code=204, timeout=30):
        return self.wait(lambda:self.receiver.selected(name, status, code), timeout,
                         name+'/'+status+'/'+str(code))

    def probe(self):
        now = time.time()
        alert = dict(labels=dict(alertname='Q4TDeliveryProbe', severity='information',
            component='q4t-monitor', deployment='thor-acceptance',
            job='q4t-validation', instance='synthetic-local'),
            annotations=dict(summary='Bounded synthetic delivery validation.'),
            startsAt=utc(now), endsAt=utc(now+240), generatorURL='http://127.0.0.1/validation')
        n.pkg.save(OUT/'probe-alert.json', alert)
        port = self.notify_manifest['ports']['alertmanager']
        self.api('probe-firing', port, '/api/v2/alerts', [alert], raw=True)
        self.callback('Q4TDeliveryProbe', 'firing', 307)
        self.receiver.redirect = False
        accepted = self.callback('Q4TDeliveryProbe', 'firing')
        n.require(not any(r['path'] == '/redirected' for r in self.receiver.rows), 'redirect followed')
        self.stop_child('alertmanager-1')
        data = BASE/'state/alertmanager/nflog'
        n.require(data.is_file() and data.stat().st_size > 0, 'notification state absent')
        n.pkg.save(OUT/'notification-state.json', dict(path=str(data), sha256=n.pkg.sha(data), bytes=data.stat().st_size))
        self.start_monitor('alertmanager', 2)
        self.api('probe-resend', port, '/api/v2/alerts', [alert], raw=True)
        start = time.monotonic()
        while time.monotonic()-start < 20:
            self.budget(); time.sleep(.25)
        n.require(len(self.receiver.selected('Q4TDeliveryProbe', 'firing')) == len(accepted),
                  'acknowledged alert repeated after clean restart')
        n.pkg.save(OUT/'dedup-window.json', dict(seconds=time.monotonic()-start,
            firing_acknowledgments=len(accepted), state_reused=True))
        alert['endsAt'] = utc(time.time()-1)
        self.api('probe-resolved', port, '/api/v2/alerts', [alert], raw=True)
        self.callback('Q4TDeliveryProbe', 'resolved')

    def business(self):
        label = self.start_model()
        folder = OUT/'receiver'; folder.mkdir(mode=0o700)
        self.receiver = receiver.Receiver(folder, BASE/'private/certificate.pem',
            BASE/'private/key.pem', (BASE/'private/token-private').read_text())
        self.start_monitor('alertmanager', 1)
        self.start_monitor('prometheus', 1)
        self.callback('Q4TNotifyHeartbeat', 'firing', 503, timeout=45)
        self.callback('Q4TNotifyHeartbeat', 'firing')
        self.plain('ordinary'); self.tools('project'); self.drain('before-down', 3)
        value = self.api('healthy-alerts', self.notify_manifest['ports']['prometheus'], '/api/v1/alerts')
        n.require(not any(v['state'] == 'firing' and v['labels'].get('component') == 'q4t-serve'
                          for v in value['data']['alerts']), 'healthy business alarm')
        self.probe()
        self.stop_child(label)
        self.callback('Q4TTargetDown', 'firing', 503, timeout=100)
        self.callback('Q4TTargetDown', 'firing')
        recovered = self.start_model()
        self.plain('recovery'); self.drain('recovered', 1)
        self.callback('Q4TTargetDown', 'resolved', timeout=40)
        for role, names in (
            ('alertmanager', ['alertmanager_notifications_failed_total', 'alertmanager_notifications_total']),
            ('prometheus', ['prometheus_notifications_dropped_total', 'prometheus_notifications_errors_total'])):
            data = self.api(role+'-final-metrics', self.notify_manifest['ports'][role], '/metrics', raw=True)
            n.require(all(name in data for name in names), 'notification metric contract unavailable')
        self.stop_child('prometheus-1'); self.stop_child('alertmanager-2')
        self.receiver.stop()
        n.pkg.save(OUT/'receiver-result.json', dict(attempts=len(self.receiver.rows),
            errors=self.receiver.errors, all_authorized=all(r['authorization_valid'] for r in self.receiver.rows),
            redirected_requests=sum(r['path'] != '/alerts' for r in self.receiver.rows)))
        n.require(not self.receiver.errors and all(r['authorization_valid'] for r in self.receiver.rows),
                  'receiver authorization or parsing failed')
        self.receiver = None
        self.stop_child(recovered)

    def execute(self):
        OUT.mkdir(mode=0o700, exist_ok=True)
        require_guard()
        n.pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', model_starts=0, monitor_starts=0,
            external_receiver_accepted=False, performance_acceptance=False, long_test_started=False)
        def interrupted(*_): raise RuntimeError('notification acceptance interrupted')
        previous = {s:signal.signal(s, interrupted) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
        signal.setitimer(signal.ITIMER_REAL, 305)
        try:
            self.business()
            n.require(self.counts['generations'] == 4 and self.model_starts == 2 and
                      self.monitor_starts == 3, 'unexpected work counts')
            result.update(status='PASS_LOCAL_NOTIFICATION_AND_RECOVERY', successful_requests=4,
                          real_tool_executions=1)
        except BaseException as error:
            result['error'] = repr(error); raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0); self.cleaning = True
            errors = []
            if self.receiver is not None:
                try: self.receiver.stop()
                except BaseException as error: errors.append(repr(error))
            # Stop remaining owned groups together: one cleanup deadline,
            # not N sequential 30-second waits after a business failure.
            try:
                n.sup.processes.stop_children(self.children,
                    {k:v.thread for k,v in self.logs.items()}, grace=25, kill_grace=5)
                for label, child in self.children.items():
                    n.require(self.logs[label].error is None, 'driver log capture failed')
                    n.pkg.save(OUT/(label+'-final-exit.json'), dict(returncode=child.returncode))
                for output in self.role_runs.values():
                    n.require(n.m.read(output/'terminal.json')['cleanup_confirmed'],
                              'monitor cleanup not confirmed')
            except BaseException as error: errors.append(repr(error))
            try:
                a.require_idle(OUT/'gpu-final.json')
                n.require(all(not n.m.alive(owner) for owner in self.owners.values()), 'owner remained')
                for port in (18123, 19095, 19096, 19445):
                    with socket.socket() as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        sock.bind(('127.0.0.1', port))
                require_guard()
                n.require(time.monotonic()-self.started < GPU_SECONDS, 'stage time exceeded')
            except BaseException as error: errors.append(repr(error))
            if errors: result.update(status='FAILED', cleanup_errors=errors)
            result.update(cleanup_confirmed=not errors, counts=self.counts,
                model_starts=self.model_starts, monitor_starts=self.monitor_starts,
                elapsed_seconds=time.monotonic()-self.started,
                scrape_http_upper_bound=3*(math.ceil(GPU_SECONDS/15)+2))
            n.pkg.save(OUT/'result.json', result)
            for sig, handler in previous.items(): signal.signal(sig, handler)
            n.require(not errors, 'owned notification cleanup incomplete')
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'check', 'execute'))
    args = parser.parse_args(); configure()
    value = driver_cpu() if args.action == 'cpu' else prepare() if args.action == 'prepare' else check()
    if args.action == 'execute':
        with n.sup.processes.child_subreaper(): value = Run(value).execute()
    print(json.dumps(value))
