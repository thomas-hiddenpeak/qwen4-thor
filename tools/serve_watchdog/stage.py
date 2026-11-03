"""One bounded lease: real heartbeat silence, observer restart and recovery."""
import argparse
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import ssl
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_watchdog import core as c, watchdog as w, cpu, receiver
from tools.serve_watchdog.storage import encoded
from tools.serve_notify import notify as n
from tools.serve_notify.test_notify import dependencies, private_inputs
import gpu_exclusive as guard
import nvml_process_query

BASE, OUT = cpu.BASE, cpu.BASE/'execution'
PLAN = BASE/'plan.json'
LIMITS = dict(seconds=420, business_seconds=360, model_starts=0,
    generations=0, monitor_starts=4, watchdog_starts=2,
    callback_attempts=64, explicit_http=16)
PORTS = dict(runner=18128, prometheus=19097, alertmanager=19098, receiver=19446)
EXTRA = ('tools/serve_watchdog/stage.py', 'tools/serve_watchdog/test_stage.py',
    'tools/serve_watchdog/receiver.py', 'tools/serve_watchdog/correction.py',
    'tools/serve_watchdog/review_cpu.py', 'tools/serve_notify/owned_exec.py',
    'docs/SERVE_NOTIFY_WATCHDOG_GPU_2026-11-04.md')
DAY = ROOT/'.q4t-work/serve-cuda-quota-integration-20261104/operator-soak/day'


def sources():
    # Bind the complete actually imported local dependency set, plus helper
    # entrypoints spawned in isolated Python processes.
    names = set((*cpu.SOURCES, *EXTRA, *n.SOURCES))
    for module in tuple(sys.modules.values()):
        file = getattr(module, '__file__', None)
        if file and Path(file).resolve().is_relative_to(ROOT/'tools'):
            names.add(str(Path(file).resolve().relative_to(ROOT)))
    return {name:w.file_sha(ROOT/name) for name in sorted(names)}


def save(path, value):
    n.pkg.save(path, value)


def live(process):
    fd = os.pidfd_open(process.pid)
    try:
        return not select.select([fd], [], [], 0)[0]
    finally:
        os.close(fd)


def waitable(process, timeout):
    fd = os.pidfd_open(process.pid)
    try:
        c.require(select.select([fd], [], [], timeout)[0], 'child_stop_timeout')
        value = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOWAIT)
        c.require(value.si_pid == process.pid, 'wait_identity')
        return value.si_status if value.si_code == os.CLD_EXITED else -value.si_status
    finally:
        os.close(fd)


def guard_check():
    lock = guard.common_lock()
    info = lock.stat()
    c.require((info.st_dev, info.st_ino) == (66305, 3294067), 'lock_identity')
    with lock.open('r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise c.ContractError('whole_stage_guard_required')
        lease = json.load(stream)
    c.require(lease['pid'] == os.getppid() and lease['state'] == 'held' and
              lease['output'] == str(OUT/'lease'), 'guard_owner')


def acknowledgement(after):
    events = (OUT/'lease/events.jsonl').read_text().split('\n')[:-1]
    queries = (OUT/'lease/nvml-queries.jsonl').read_text().split('\n')[:-1]
    for line in reversed(events):
        event = json.loads(line)
        if event.get('event') != 'monitor': continue
        c.require(not event['conflicts'] and not event['unreadable_processes'],
                  'guard_interference')
        for line in reversed(queries):
            query = json.loads(line)
            c.require(query['status'] == 'OK', 'guard_query_failed')
            rows = [json.loads(v) for v in query['stdout'].splitlines()]
            if (rows and rows[0]['event'] == 'begin' and
                    rows[-1]['event'] == 'result' and after <=
                    rows[0]['monotonic'] <= rows[-1]['monotonic'] <=
                    event['monotonic']):
                return dict(event=event, query_id=query['request_id'],
                    query_begin=rows[0]['monotonic'],
                    query_end=rows[-1]['monotonic'])
    return None


def driver_cpu():
    output = BASE/'driver-cpu'; output.mkdir(mode=0o700)
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName('tools.serve_watchdog.test_stage'))
    imports = []
    for file in ('stage.py',):
        check = subprocess.run([sys.executable, '-I', '-B',
            ROOT/'tools/serve_watchdog'/file, '--help'], capture_output=True,
            text=True, timeout=10)
        imports.append(dict(returncode=check.returncode,
                            stdout=check.stdout, stderr=check.stderr))
    (output/'tests.log').write_text(log.getvalue())
    source = sources()
    for name in source:
        dest = output/'source'/name; dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/name).read_bytes())
    value = dict(passed=result.wasSuccessful() and all(
        row['returncode'] == 0 for row in imports), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), imports=imports,
        sources=source, model_gpu_http_started=False)
    save(output/'result.json', value)
    print(log.getvalue()); c.require(value['passed'], 'driver_CPU_failed')
    return dict(passed=True, tests_run=result.testsRun)


def prepare():
    c.require(not PLAN.exists() and not OUT.exists(), 'preserve_originals')
    source = sources()
    for name in source:
        c.require(subprocess.check_output(['git', 'show', 'HEAD:'+name]) ==
                  (ROOT/name).read_bytes(), 'commit_driver_first:'+name)
    for name in ('review-cpu', 'driver-cpu'):
        result = n.pkg.read(BASE/name/'result.json')
        c.require(result['passed'], 'CPU_gate')
        for file, digest in result['sources'].items():
            c.require(w.file_sha(ROOT/file) == digest, 'CPU_source_changed:'+file)
    private = BASE/'private'; private.mkdir(mode=0o700)
    p = private_inputs(private, PORTS['receiver'])
    for role in ('runner', 'prometheus', 'alertmanager'):
        p[role+'_port'] = PORTS[role]
    save(private/'policy-private.json', p)
    notification = n.prepare(private/'policy-private.json', dependencies(), BASE/'package')
    n.init_state(notification['package'], notification['manifest_sha256'], BASE/'monitor-state')
    watch = w.initialize(BASE/'watchdog-state', p['deployment'], private/'token-private')
    value = dict(version=1, limits=LIMITS, ports=PORTS, sources=source,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        notification=notification, watchdog=watch,
        cpu={name:w.file_sha(BASE/name/'result.json') for name in
             ('cpu', 'cpu-correction', 'review-cpu', 'driver-cpu')},
        default_binary=w.file_sha(ROOT/'build/q4t'),
        default_sidecar=w.file_sha(ROOT/'build/q4t.release.json'),
        day_sha256=w.file_sha(DAY/'plan.json'),
        old_day_sha256=w.file_sha(ROOT/'.q4t-work/serve-soak-20261103/day/plan.json'),
        external_receiver_accepted=False, long_validation_owner='user')
    save(PLAN, value)
    return dict(prepared=True, model_http_started=False, limits=LIMITS)


def check():
    plan = n.pkg.read(PLAN)
    c.require(plan['limits'] == LIMITS and plan['ports'] == PORTS, 'budget_changed')
    for name, digest in plan['sources'].items():
        c.require(w.file_sha(ROOT/name) == digest, 'source_changed:'+name)
    for name, digest in plan['cpu'].items():
        c.require(w.file_sha(BASE/name/'result.json') == digest, 'CPU_record_changed')
    n.verify(plan['notification']['package'], plan['notification']['manifest_sha256'])
    with w.Directory(BASE/'watchdog-state') as directory:
        w.verify(directory, plan['watchdog']['policy_sha256'])
    c.require(w.file_sha(ROOT/'build/q4t') == plan['default_binary'] and
        w.file_sha(ROOT/'build/q4t.release.json') == plan['default_sidecar'] and
        w.file_sha(DAY/'plan.json') == plan['day_sha256'] and
        not (DAY/'execution').exists(), 'default_or_day_changed')
    old = ROOT/'.q4t-work/serve-soak-20261103/day'
    c.require(w.file_sha(old/'plan.json') == plan['old_day_sha256'] and
              not (old/'execution').exists(), 'old_day_changed')
    return plan


class Run:
    def __init__(self, plan):
        self.plan, self.started = plan, time.monotonic()
        self.children, self.logs, self.owners = {}, {}, {}
        self.started_at, self.role_runs = {}, {}
        self.receiver = None
        self.http = self.monitor_starts = self.watchdog_starts = 0
        self.parent = n.sup.process_identity(os.getpid())
        self.digest = plan['watchdog']['policy_sha256']
        self.state = BASE/'watchdog-state'

    def budget(self):
        c.require(time.monotonic()-self.started < LIMITS['business_seconds'],
                  'business_deadline')
        c.require(self.receiver is None or len(self.receiver.rows) <=
                  LIMITS['callback_attempts'], 'callback_budget')

    def wait(self, predicate, seconds, description):
        until = time.monotonic()+seconds
        while time.monotonic() < until:
            self.budget()
            if self.receiver: c.require(not self.receiver.errors, 'receiver_failure')
            value = predicate()
            if value: return value
            time.sleep(.2)
        raise c.ContractError('deadline:'+description)

    def spawn(self, label, argv):
        self.budget()
        c.require(label not in self.children, 'duplicate_spawn')
        argv = list(map(str, [sys.executable, '-I', '-B',
            ROOT/'tools/serve_notify/owned_exec.py', '--parent',
            json.dumps(self.parent), '--', *argv]))
        save(OUT/(label+'-command.json'), dict(argv=argv))
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, umask=0o077,
            env=dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', HOME=str(Path.home())))
        self.children[label] = child
        self.owners[label] = n.sup.process_identity(child.pid)
        self.started_at[label] = time.monotonic()
        logger = n.sup.RotatingLog(OUT/(label+'.log'), 1024**2)
        self.logs[label] = logger; logger.start(child.stdout)
        save(OUT/(label+'-owner.json'), self.owners[label])

    def monitor(self, role, number):
        c.require(self.monitor_starts < LIMITS['monitor_starts'], 'monitor_budget')
        self.monitor_starts += 1
        label = f'{role}-{number}'
        package = self.plan['notification']
        output = OUT/label; self.role_runs[label] = output
        self.spawn(label, [sys.executable, '-I', '-B', Path(package['package'])/
            'source/tools/serve_notify/notify.py', 'run', '--package', package['package'],
            '--manifest-sha256', package['manifest_sha256'], '--role', role,
            '--state', BASE/'monitor-state', '--output', output])

        def listening():
            c.require(live(self.children[label]), 'monitor_exited')
            path = output/'status.json'
            if path.exists():
                value = n.pkg.read(path)
                if value['state'] == 'LISTENING': return value
            return None
        value = self.wait(listening, 30, 'monitor_listening')
        save(OUT/(label+'-listening.json'), value)

    def observer(self, number):
        c.require(self.watchdog_starts < LIMITS['watchdog_starts'], 'watchdog_budget')
        self.watchdog_starts += 1
        before = time.time()
        label = f'watchdog-{number}'
        self.spawn(label, [sys.executable, '-I', '-B',
            self.state/'source/tools/serve_watchdog/watchdog.py', 'watch',
            '--state', self.state, '--policy-sha256', self.digest])

        def fresh():
            c.require(live(self.children[label]), 'watchdog_exited')
            path = self.state/'status.json'
            if path.exists():
                v = n.pkg.read(path)
                if v['observed']['unix_seconds'] >= before: return v
            return None
        value = self.wait(fresh, 10, 'watchdog_fresh')
        save(OUT/(label+'-fresh.json'), value)

    def stop(self, label):
        child = self.children[label]
        c.require(live(child) and n.sup.signal_owner(self.owners[label],
                  signal.SIGTERM), 'owned_stop')
        code = waitable(child, 25)
        save(OUT/(label+'-waitable.json'), dict(owner=self.owners[label],
            returncode=code, monotonic=time.monotonic()))
        c.require(code == 0, 'child_exit:'+label)
        self.logs[label].thread.join(3)
        c.require(self.logs[label].error is None, 'driver_log_failed')
        if label in self.role_runs:
            term = n.pkg.read(self.role_runs[label]/'terminal.json')
            c.require(term['state'] == 'STOPPED' and term['cleanup_confirmed']
                      and term['restarts'] == 0, 'monitor_cleanup')

    def rejected_http(self, payload, auth, expected, label):
        self.http += 1
        c.require(self.http <= LIMITS['explicit_http'], 'HTTP_budget')
        context = ssl.create_default_context(cafile=BASE/'private/certificate.pem')
        conn = http.client.HTTPSConnection('127.0.0.1', PORTS['receiver'],
                                           context=context, timeout=5)
        before = w.file_sha(self.state/'ledger.json')
        try:
            conn.request('POST', '/alerts', payload,
                {'Authorization':auth, 'Content-Type':'application/json'})
            response = conn.getresponse(); raw = response.read(c.MAXIMUM+1)
            save(OUT/(label+'-http.json'), dict(status=response.status,
                expected=expected, bytes=len(raw), before_sha256=before,
                after_sha256=w.file_sha(self.state/'ledger.json')))
            c.require(response.status == expected and not raw and
                w.file_sha(self.state/'ledger.json') == before,
                'rejected_input_refreshed_heartbeat')
        finally:
            conn.close()

    def business(self):
        for port in PORTS.values():
            with socket.socket() as probe: probe.bind(('127.0.0.1', port))
        token = (BASE/'private/token-private').read_text().strip()
        self.receiver = receiver.Receiver(OUT/'receiver', self.state, self.digest,
            token, BASE/'private/certificate.pem', BASE/'private/key.pem',
            PORTS['receiver'])
        self.observer(1)
        self.monitor('alertmanager', 1); self.monitor('prometheus', 1)
        first = self.wait(lambda: (v if (v := w.inspect(self.state, self.digest))[
            'healthy'] else None), 55, 'first_real_heartbeat')
        save(OUT/'first-present.json', first)
        accepted = next(row for row in self.receiver.rows if row['status'] == 204)
        raw = (OUT/'receiver'/f"{accepted['index']:03d}-body.json").read_bytes()
        self.rejected_http(raw, 'Bearer invalid', 401, 'unauthorized')
        changed = c.parse(raw)
        changed['alerts'][0]['labels']['deployment'] = 'other-deployment'
        self.rejected_http(encoded(changed), 'Bearer '+token, 422, 'wrong-deployment')
        changed = c.parse(raw)
        changed['alerts'][0]['labels']['alertname'] = 'Q4TUnrelatedFixture'
        self.rejected_http(encoded(changed), 'Bearer '+token, 202, 'ordinary-not-heartbeat')
        self.stop('prometheus-1'); self.stop('alertmanager-1')
        original = n.pkg.read(self.state/'ledger.json')['receipt']
        save(OUT/'monitors-stopped.json', dict(monotonic=time.monotonic(), receipt=original))
        samples = []

        def missing():
            value = w.inspect(self.state, self.digest)
            samples.append(value)
            c.require(len(samples) < 1050, 'silence_sample_budget')
            c.require(n.pkg.read(self.state/'ledger.json')['receipt'] == original,
                      'heartbeat_changed_during_silence')
            if value['state'] == 'MISSING': return value
            c.require(value['state'] == 'PRESENT', 'unexpected_silence_state')
            return None
        lost = self.wait(missing, 184, 'real_180_second_silence')
        c.require(c.TIMEOUT <= lost['age_seconds'] < c.TIMEOUT+3,
                  'silence_detection_bound')
        save(OUT/'silence-samples.json', samples)
        save(OUT/'missing.json', lost)
        projected = self.wait(lambda: (v if (v := n.pkg.read(
            self.state/'status.json'))['state'] == 'MISSING' else None),
            3, 'missing_projection')
        save(OUT/'missing-projection.json', projected)
        n.m.g.save(OUT/'missing.prom', (self.state/'metrics.prom').read_bytes())
        self.stop('watchdog-1')
        frozen = (self.state/'metrics.prom').read_bytes()
        stamp = n.pkg.read(self.state/'status.json')['observed']['unix_seconds']
        self.wait(lambda: time.time()-stamp > 20, 23, 'observer_staleness')
        c.require((self.state/'metrics.prom').read_bytes() == frozen,
                  'stopped_observer_refreshed')
        save(OUT/'observer-stale.json', dict(stamp=stamp, observed=time.time(),
            age_seconds=time.time()-stamp, file_sha256=w.file_sha(self.state/'metrics.prom')))
        self.observer(2)
        c.require(w.inspect(self.state, self.digest)['state'] == 'MISSING' and
            n.pkg.read(self.state/'ledger.json')['receipt'] == original,
            'observer_restart_reset_deadline')
        save(OUT/'observer-restarted.json', w.inspect(self.state, self.digest))
        self.monitor('alertmanager', 2); self.monitor('prometheus', 2)
        recovered = self.wait(lambda: (v if (v := w.inspect(self.state, self.digest))[
            'healthy'] else None), 80, 'real_pipeline_recovery')
        c.require(recovered['accepted'] > first['accepted'], 'no_new_delivery')
        save(OUT/'recovered.json', recovered)
        self.stop('prometheus-2'); self.stop('alertmanager-2')
        self.stop('watchdog-2')
        self.receiver.stop(); self.receiver = None
        for name in ('ledger.json', 'status.json', 'metrics.prom'):
            n.m.g.save(OUT/('final-'+name), (self.state/name).read_bytes())

    def execute(self):
        guard_check()
        n.sup.arm_parent_death(n.sup.process_identity(os.getppid()))
        OUT.mkdir(mode=0o700, exist_ok=True)
        save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time()))
        result = dict(status='FAILED', external_receiver_accepted=False,
            model_starts=0, generations=0, long_test_started=False)

        def interrupted(*_): raise c.ContractError('stage_interrupted')
        previous = {sig:signal.signal(sig, interrupted) for sig in
                    (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
        signal.setitimer(signal.ITIMER_REAL, LIMITS['business_seconds'])
        try:
            self.business()
            c.require(self.monitor_starts == 4 and self.watchdog_starts == 2,
                      'work_count')
            # Retain all direct process identities across a fresh guard scan.
            ack = self.wait(lambda: acknowledgement(max(self.started_at.values())),
                            8, 'guard_acknowledgement')
            save(OUT/'custody.json', dict(acknowledgement=ack, owners=self.owners))
            result['status'] = 'PASS_LOCAL_INDEPENDENT_HEARTBEAT'
        except BaseException as error:
            result['error'] = repr(error)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            errors = []
            if self.receiver is not None:
                try: self.receiver.stop()
                except BaseException as error: errors.append(repr(error))
            try:
                n.sup.processes.stop_children(self.children,
                    {k:v.thread for k,v in self.logs.items()}, grace=25, kill_grace=5)
                for name, process in self.children.items():
                    save(OUT/(name+'-exit.json'), dict(returncode=process.returncode))
                    c.require(not n.m.alive(self.owners[name]), 'child_remained')
                    c.require(self.logs[name].error is None, 'log_error')
                for run in self.role_runs.values():
                    c.require(n.pkg.read(run/'terminal.json')['cleanup_confirmed'],
                              'monitor_cleanup_unknown')
                for port in PORTS.values():
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind(('127.0.0.1', port))
                value = nvml_process_query.query(timeout=5)
                save(OUT/'gpu-final.json', value)
                c.require(value['status'] == 'OK' and not value['pids'], 'GPU_not_idle')
                guard_check()
                c.require(time.monotonic()-self.started < LIMITS['seconds'], 'whole_budget')
            except BaseException as error:
                errors.append(repr(error))
            result.update(cleanup_confirmed=not errors, cleanup_errors=errors,
                monitor_starts=self.monitor_starts, watchdog_starts=self.watchdog_starts,
                explicit_http=self.http, elapsed_seconds=time.monotonic()-self.started)
            if errors: result['status'] = 'FAILED'
            save(OUT/'result.json', result)
            for sig, previous_handler in previous.items():
                signal.signal(sig, previous_handler)
        c.require(result['status'].startswith('PASS_'), 'execution_failed_preserved')
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('cpu', 'prepare', 'check', 'execute'))
    args = parser.parse_args()
    if args.action == 'cpu': value = driver_cpu()
    elif args.action == 'prepare': value = prepare()
    else:
        value = check()
        if args.action == 'execute':
            with n.sup.processes.child_subreaper(): value = Run(value).execute()
    print(json.dumps(value))
