"""Standalone controller SIGKILL with active/queued real HTTP and lease custody."""
import http.client
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_service import acceptance as a, package as pkg, supervisor as sup
from tools.serve_service.lease_custody import Custody, EnvironmentAbort
from tools.serve_lifecycle.verify import terminal as stream_terminal
from jsonschema import Draft202012Validator

BASE = ROOT/'.q4t-work/serve-controller-death-20261103'
PLAN, OUT = BASE/'plan.json', BASE/'execution'
LIMITS = dict(starts=1, generations=6, negatives=1, observations=260, seconds=600)
CONTROL_LIMIT = 64
SOURCES = (*a.SOURCES, 'tools/serve_service/lease_custody.py',
    'tools/serve_service/controller_death.py',
    'tools/serve_service/test_controller_death.py',
    'tools/serve_lifecycle/verify.py', 'docs/serve-transport-v1.schema.json',
    'docs/SERVE_CONTROLLER_DEATH_2026-11-03.md')


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve previous attempt')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit reviewed validation first')
    cpu = pkg.read(BASE/'cpu-final/result.json')
    pkg.require(cpu['passed'], 'CPU checks incomplete')
    for name, digest in cpu['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU source changed')
    old = pkg.read(ROOT/'.q4t-work/serve-memory-stop-20261103/accepted-package.json')
    manifest = pkg.verify(old['package']['package'], old['package']['manifest_sha256'])
    for name in pkg.SOURCES:
        pkg.require(pkg.sha(ROOT/name) == manifest['files']['source/'+name],
                    'production service changed: '+name)
    release = pkg.read(ROOT/'build/q4t.release.json')
    for name, digest in pkg.read(release['tested_source_manifest']).items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'C++ runtime changed: '+name)
    receipt = pkg.prepare(ROOT/'build/q4t', ROOT/'build/q4t.release.json',
        manifest['model_dir'], BASE/'package', port=18122, restart_limit=3)
    pkg.save(PLAN, dict(version=1, limits=LIMITS, gpu_window_seconds=a.GPU_WINDOW,
        package=receipt, unit='unused-foreground-only.service', token='foreground',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                              text=True).strip(),
        source_files={p:pkg.sha(ROOT/p) for p in SOURCES},
        cpu_result_sha256=pkg.sha(BASE/'cpu-final/result.json'),
        binary_sha256=release['binary_sha256'],
        default_release_sha256=pkg.sha(ROOT/'build/q4t.release.json'),
        production_service_reused_from=old['package'],
        planned=dict(starts=1, generations=6, successes=4, shutdown_errors=2),
        explicit_control_limit=CONTROL_LIMIT, supervisor_http_upper_bound=188,
        original_controller_guard_intentionally_interrupted=True,
        independent_same_description_custody=True, performance_acceptance=False))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['gpu_window_seconds'] == a.GPU_WINDOW,
                'budget changed')
    for name, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'source changed: '+name)
    pkg.require(pkg.sha(BASE/'cpu-final/result.json') == plan['cpu_result_sha256'],
                'CPU receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['binary_sha256'] and
        pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'],
        'default pair changed')
    return plan


class StreamCall:
    def __init__(self, port, label, body):
        self.body, self.label = body, label
        self.content = threading.Event()
        self.value = self.error = None
        self.thread = threading.Thread(target=self.run, args=(port,), daemon=True)
        pkg.save(OUT/(label+'-request.json'), body)
        self.thread.start()

    def run(self, port):
        raw, receipt = bytearray(), dict(eof=False)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=45)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(self.body),
                {'Content-Type':'application/json', 'X-Request-Id':self.body['request_id']})
            response = conn.getresponse()
            receipt.update(status=response.status, headers=dict(response.getheaders()))
            with (OUT/(self.label+'-wire.bin')).open('xb', buffering=0) as wire:
                while line := response.readline(65537):
                    raw.extend(line)
                    wire.write(line)
                    pkg.require(len(raw) <= 8*1024**2 and len(line) <= 65536,
                                'stream capture byte budget')
                    if line.startswith(b'data: {'):
                        event = json.loads(line[6:])
                        if any(c.get('delta', {}).get('content')
                               for c in event.get('choices', [])):
                            self.content.set()
            receipt['eof'] = True
            pkg.require(response.status == 200 and
                response.headers.get_content_type() == 'text/event-stream',
                'interrupted stream encoding/status')
            self.value = a.read_sse(io.BytesIO(raw))
        except BaseException as error:
            self.error = error
            receipt['error'] = repr(error)
        finally:
            conn.close()
            pkg.save(OUT/(self.label+'-http.json'), receipt)


class Run(a.Run):
    def __init__(self, plan):
        super().__init__(plan)
        self.process = self.log = self.custody = None
        self.calls = []
        self.guard_interrupted = False
        self.custody_done = False
        self.helper_signals = 0

    def budget(self, kind=None):
        if self.custody is not None:
            self.custody.check()
        super().budget(kind)

    def launch(self):
        self.log = (OUT/'controller.log').open('xb')
        command = [self.manifest['python']['path'], '-I', '-B', __file__,
                   '_controller', str(PLAN), str(os.getpid())]
        self.gpu_started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, a.GPU_WINDOW)
        self.process = subprocess.Popen(command, cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
        state = self.wait_state('READY', 1)
        self.controller = pkg.read(self.run_dir/'controller.json')
        pkg.require(self.controller['owner'] == sup.process_identity(self.process.pid),
                    'not the directly launched controller')
        lock = Path(self.controller['lock_path'])
        lease = pkg.read(lock)
        pkg.require(lease['state'] == 'held' and lease['pid'] == self.process.pid and
            lease['output'] == str(self.run_dir/'lease'), 'original guard lacks lease')
        for owner in (self.controller['owner'], state['owner'], state['backend']):
            pkg.require(sup.process_identity(owner['pid']) == owner, 'owner changed')
            self.owners.append(owner)
        custody_out = OUT/'custody'; custody_out.mkdir()
        def failed():
            if not self.cleaning:
                os.kill(os.getpid(), signal.SIGUSR1)
        self.custody = Custody(self.controller['owner'], lock, custody_out,
                               on_failure=failed)
        self.custody.start()
        self.bind_descriptors('first-ready')

    def bind_descriptors(self, label):
        lock = Path(self.controller['lock_path'])
        st = lock.stat()
        rows = []
        for owner in self.owners:
            pkg.require(sup.process_identity(owner['pid']) == owner, 'owner changed')
            descriptors = []
            for entry in Path(f"/proc/{owner['pid']}/fd").iterdir():
                try:
                    value = entry.stat()
                except FileNotFoundError:
                    continue
                if (value.st_dev, value.st_ino) == (st.st_dev, st.st_ino):
                    descriptors.append(dict(fd=entry.name, info=Path(
                        f"/proc/{owner['pid']}/fdinfo/{entry.name}").read_text()))
            pkg.require(descriptors, 'owned process lacks inherited lease')
            rows.append(dict(owner=owner, descriptors=descriptors))
        pkg.save(OUT/(label+'-ownership.json'), dict(owners=rows,
            lock_device=st.st_dev, lock_inode=st.st_ino,
            external_duplicate=self.custody.fd))

    def rows(self):
        self.budget('controls')
        response = sup.transport.get(self.port, '/metrics')
        pkg.require(response['http_status'] == 200, 'metrics unavailable')
        values, _ = a.contracts.parse_metrics(response['body'], version=4)
        with (OUT/'busy-observations.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(monotonic=time.monotonic(),
                                         response=response, metrics=values))+'\n')
        return values

    def interrupted(self):
        body = a.make_request([dict(role='user', content=
            '从1开始逐行写出整数直到10000，每行一个，不要省略，不要解释，持续输出。')],
            [], stream=True, max_tokens=4096, timeout_ms=180000)
        self.budget('generations')
        active = StreamCall(self.port, 'active', body); self.calls.append(active)
        until = time.monotonic()+20
        while not active.content.is_set():
            self.budget()
            pkg.require(active.thread.is_alive() and time.monotonic() < until,
                        'active generation did not produce content')
            time.sleep(.05)
        body = a.strict_json()
        body.update(stream=True, stream_options=dict(include_usage=True))
        self.budget('generations')
        queued = StreamCall(self.port, 'queued', body); self.calls.append(queued)
        until = time.monotonic()+8
        while True:
            values = self.rows()
            if (values['q4t_queued_requests'] == 1 and
                values['q4t_structured_workers_active'] == 1 and
                values['q4t_structured_preparing'] == 0 and
                values['q4t_seq_slots_free'] == 0):
                break
            pkg.require(time.monotonic() < until, 'prepared queued request missing')
            time.sleep(.2)
        pkg.require(all(c.thread.is_alive() for c in self.calls),
                    'request completed before controller crash')
        self.bind_descriptors('before-crash')
        self.custody.sample()
        before = self.custody.identities()
        pkg.save(OUT/'before-crash.json', dict(metrics=values, owned=before,
            controller=self.controller['owner'], monotonic=time.monotonic()))
        self.budget('negatives')
        pkg.require(sup.signal_owner(self.controller['owner'], signal.SIGKILL),
                    'controller absent before intentional SIGKILL')
        self.guard_interrupted = True
        pkg.require(self.process.wait(5) == -signal.SIGKILL, 'controller exit differs')
        # No helper signals to worker/backend on the success path. Only wait
        # and reap already-dead adopted children; production parent-death and
        # group cleanup must do the work.
        deadline = time.monotonic()+40
        while True:
            self.budget()
            alive = []
            for owner in self.owners[1:]:
                try:
                    if sup.process_identity(owner['pid']) != owner:
                        continue
                    fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(')', 1)[1].split()
                    if fields[0] == 'Z' and int(fields[1]) == os.getpid():
                        os.waitpid(owner['pid'], os.WNOHANG)
                    else:
                        alive.append(owner)
                except (FileNotFoundError, ProcessLookupError, ChildProcessError):
                    pass
            if not alive:
                break
            pkg.require(time.monotonic() < deadline, 'parent-death cleanup timed out')
            time.sleep(.05)
        for call in self.calls:
            call.thread.join(5)
            pkg.require(not call.thread.is_alive() and call.value is None and
                        call.error is not None, 'interrupted call accepted or stuck')
            checked = stream_terminal((OUT/(call.label+'-wire.bin')).read_bytes(),
                graceful=True, validator=Draft202012Validator(pkg.read(
                    ROOT/'docs/serve-transport-v1.schema.json')))
            pkg.save(OUT/(call.label+'-verified.json'), checked)
        worker = pkg.read(self.run_dir/'status.json')
        pkg.require(worker['state'] == 'STOPPED' and worker['cleanup_confirmed'] and
                    worker['attempts'] == 1 and worker['restarts'] == 0,
                    'worker cleanup/restart mismatch')
        pkg.require(not (self.run_dir/'terminal.json').exists() and
                    not (self.run_dir/'lease/exit.json').exists(),
                    'dead controller unexpectedly finalized its own guard')
        report = self.service_cli('status-after-crash', 'status')
        value = json.loads(report.stdout)
        pkg.require(value['state'] == 'OWNER_ABSENT' and not value['cleanup_confirmed'],
                    'status falsely claimed controller-confirmed cleanup')
        log = (self.run_dir/'backend.log').read_text()
        pkg.require('shutdown complete (0 in-flight remaining)' in log,
                    'backend shutdown incomplete')
        for call in self.calls:
            ends = [line for line in log.splitlines() if
                f'id={call.body["request_id"]} ' in line and 'event=end ' in line]
            pkg.require(len(ends) == 1 and 'reason=server_shutdown' in ends[0],
                        'shutdown request accounting differs')
        reaped = re.findall(r'grammar worker reaped=(\d+) ', log)
        pkg.require(len(reaped) == 4 and all(n == '1' for n in reaped),
                    'grammar shutdown reaping incomplete')
        pkg.save(OUT/'controller-death-verified.json', dict(passed=True,
            controller_returncode=self.process.returncode, worker=worker,
            active_shutdown_errors=2, grammar_reaped=4,
            helper_cleanup_signals=0, original_guard_terminal_missing=True))

    def cleanup(self):
        self.cleaning = True
        signal.setitimer(signal.ITIMER_REAL, 0)
        confirmed = False
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self.service_cli('fallback-stop', 'stop', expected=None)
                    try:
                        self.process.wait(40)
                    except subprocess.TimeoutExpired:
                        self.process.kill(); self.process.wait(5)
                # Confirm all adopted/known descendants gone before dropping
                # the custody reference; failure cleanup uses exact identities.
                history = self.custody.identities() if self.custody else {}
                remaining = a.guard.processes()
                for pid, row in history.items():
                    current = remaining.get(pid, {})
                    if current.get('start_ticks') != row.get('start_ticks'):
                        continue
                    if current.get('state') != 'Z':
                        self.helper_signals += 1
                        a.guard.signal_identity(pid, row, signal.SIGTERM)
                    elif current.get('ppid') == os.getpid():
                        os.waitpid(pid, os.WNOHANG)
                deadline = time.monotonic()+6
                while True:
                    remaining = a.guard.processes()
                    owned = [pid for pid,row in history.items() if
                        remaining.get(pid, {}).get('start_ticks') == row.get('start_ticks')]
                    if not owned:
                        break
                    pkg.require(time.monotonic() < deadline, 'owned processes survived cleanup')
                    for pid in owned:
                        if remaining[pid]['state'] == 'Z' and remaining[pid]['ppid'] == os.getpid():
                            os.waitpid(pid, os.WNOHANG)
                    time.sleep(.05)
            a.require_idle(OUT/'gpu-final.json')
            with socket.socket() as client:
                pkg.require(client.connect_ex(('127.0.0.1', self.port)) != 0,
                            'listener remains')
            confirmed = True
        finally:
            for call in self.calls:
                call.thread.join(5)
            if self.log is not None:
                self.log.close()
            if self.custody is not None:
                self.custody.finish(cleanup_confirmed=confirmed)
                self.custody_done = True
        pkg.require(a.lock_available(a.guard.common_lock()), 'lease remains held')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(plan=self.plan, unix_seconds=time.time()))
        result = dict(status='FAILED', helper_cleanup_signals=0,
                      persistent_service_installed=False, performance_acceptance=False)
        def expired(*_): raise RuntimeError('bounded GPU window expired')
        def monitor_failed(*_): raise EnvironmentAbort('independent GPU monitor failed')
        previous = {sig:signal.signal(sig, handler) for sig,handler in
                    ((signal.SIGALRM, expired), (signal.SIGUSR1, monitor_failed))}
        with sup.processes.child_subreaper():
            try:
                a.require_idle(OUT/'gpu-before.json')
                self.launch()
                self.plain('ordinary-json')
                structured = self.complete('strict-json', a.strict_json())
                pkg.require(json.loads(structured.message['content']) == {'ok':True}
                    and structured.finish_reason == 'stop', 'strict JSON quality')
                self.tools('project')
                self.drain('idle-before-crash', 4)
                self.interrupted()
                time.sleep(10)
                pkg.require(len(list(self.run_dir.glob('attempt-*.json'))) == 1,
                            'backend reactivated')
                self.gpu_finished = time.monotonic()
                pkg.require(self.gpu_finished-self.gpu_started <= a.GPU_WINDOW,
                            'GPU window exceeded')
                result.update(status='PASS_CONTROLLER_DEATH_ACTIVE_QUEUE',
                    starts=1, successes=4, shutdown_errors=2, real_tool_executions=1)
            except BaseException as error:
                result.update(status='ENV_ABORT' if isinstance(error, EnvironmentAbort)
                              else 'FAILED', error=repr(error))
                raise
            finally:
                try:
                    self.cleanup()
                    result['cleanup_confirmed'] = True
                    original_exit = self.run_dir/'lease/exit.json'
                    if original_exit.exists() and pkg.read(original_exit)['status'] == 'ENV_ABORT':
                        raise EnvironmentAbort('original guard reported environment abort')
                    if result['status'].startswith('PASS'):
                        pkg.require(self.helper_signals == 0,
                                    'natural parent-death cleanup needed helper signals')
                    pkg.require(time.monotonic()-self.started < LIMITS['seconds'], 'phase deadline')
                except BaseException as error:
                    result.update(status='ENV_ABORT', cleanup_error=repr(error))
                    raise
                finally:
                    for sig, handler in previous.items(): signal.signal(sig, handler)
                    result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                        gpu_window_elapsed=None if self.gpu_finished is None else
                            self.gpu_finished-self.gpu_started,
                        original_guard_intentionally_interrupted=self.guard_interrupted,
                        custody_completed=self.custody_done,
                        helper_cleanup_signals=self.helper_signals)
                    pkg.save(OUT/'result.json', result)


def controller_entry(plan_path, parent_pid):
    # Fresh single-threaded helper; protects the acceptance runner's own
    # lifetime, then execs the unmodified pinned production entrypoint.
    sup.arm_parent_death(sup.process_identity(parent_pid))
    plan = pkg.read(plan_path)
    package = Path(plan['package']['package'])
    manifest = pkg.verify(package, plan['package']['manifest_sha256'])
    command = [manifest['python']['path'], '-I', '-B',
        str(package/'source/tools/serve_service/service.py'), 'run',
        '--package', str(package), '--manifest-sha256', plan['package']['manifest_sha256'],
        '--output', str(OUT/'managed-run')]
    environment = {k:v for k,v in os.environ.items() if
        k not in ('LD_PRELOAD', 'PYTHONPATH', 'PYTHONHOME') and not k.startswith('Q4T_')}
    os.execve(command[0], command, environment)


if __name__ == '__main__':
    a.BASE, a.PLAN, a.OUT, a.LIMITS = BASE, PLAN, OUT, LIMITS
    a.CONTROL_LIMIT = CONTROL_LIMIT
    if sys.argv[1:] == ['prepare']: prepare()
    elif sys.argv[1:] == ['execute']: Run(check()).execute()
    elif len(sys.argv) == 4 and sys.argv[1] == '_controller':
        controller_entry(Path(sys.argv[2]), int(sys.argv[3]))
    else: raise SystemExit('prepare | execute')
