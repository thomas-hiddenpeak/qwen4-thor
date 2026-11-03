"""Two real early startup refusals, five business turns and four fixed probes."""
import json
import os
import signal
import socket
import subprocess
import time

from tools.serve_startup_memory import stage, accounting
from tools.serve_admission import verify as base
from tools.serve_lifecycle import observe
from tools.serve_pressure.pressure import memory
from isolated_scope import IsolatedScope

h, a, ROOT = stage.h, stage.a, stage.ROOT
POINTS = ('before_metadata', 'before_allocations', 'before_ready')


def startup_records(log):
    prefix = '[q4t][startup-memory] '
    return [json.loads(line[len(prefix):]) for line in log.splitlines() if line.startswith(prefix)]


def validate_records(records, headroom, failed=None):
    expected = POINTS[:POINTS.index(failed)+1] if failed else POINTS
    h.require([r['checkpoint'] for r in records] == list(expected), 'startup checkpoint order/count')
    checks = {}
    for row in records:
        checkpoint, value = row['checkpoint'], row['admission']
        floor = headroom * 1024**2
        estimate = value['estimate_bytes']
        if checkpoint == 'before_allocations':
            h.require(type(estimate) is int and estimate > 0, 'missing complete allocation estimate')
        else:
            h.require(estimate is None, 'floor-only checkpoint invented an estimate')
        h.require(value['headroom_bytes'] == floor and
                  value['required_bytes'] == floor+(estimate or 0), 'startup required budget')
        available, total = value['observed_available_bytes'], value['observed_total_bytes']
        h.require(type(available) is int and type(total) is int and
                  0 <= available <= total and total > 0, 'startup real observation missing')
        expected_state = 'startup_memory_insufficient' if checkpoint == failed else 'admitted'
        h.require(value['state'] == expected_state, 'wrong startup decision')
        h.require((available >= value['required_bytes']) == (checkpoint != failed),
                  'startup decision contradicts observation')
        checks[checkpoint] = value
    return checks


class Run(base.Run):
    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.counts['probes'] = 0
        self.deadline = plan['started']+stage.LIMITS['seconds']-90
        self.controls = 0
        self.monitor = None

    def budget(self, kind=None):
        h.require(time.time() < self.deadline, 'frozen startup stage deadline')
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'frozen attempts: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        h.require(self.controls < stage.LIMITS['observations'], 'HTTP observation budget')
        self.controls += 1
        return super().http(method, path, body, timeout=timeout)

    def wait(self, label, predicate, *, timeout=10):
        end, samples = time.monotonic()+timeout, []
        try:
            while True:
                rows = self.rows()
                samples.append(dict(monotonic=time.monotonic(), metrics=rows))
                if predicate(rows): return rows
                h.require(self.server.poll() is None and time.monotonic() < end, 'wait: '+label)
                time.sleep(.1)
        finally:
            h.save(self.folder/(label+'-samples.json'), samples)

    def argv(self):
        return [self.plan['binary']['path'], 'serve', '--model-dir', self.plan['model_dir'],
            '--host', '127.0.0.1', '--port', str(self.port), '--max-prefill', '2048',
            '--max-len', '8192', '--max-seq', '1', '--max-inflight-requests', '8',
            '--max-queued-requests', '2']

    @staticmethod
    def environment():
        return {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}

    def rejection(self, headroom, checkpoint):
        self.budget('starts')
        self.budget('negatives')
        folder = self.out/checkpoint
        folder.mkdir()
        a.require_idle_gpu(folder/'gpu-before.json')
        before = memory()
        if checkpoint == 'before_allocations' and before['MemAvailable'] <= 64*1024**3:
            raise a.EnvironmentAbort('64GiB startup-negative precondition unavailable')
        # A direct child is intentional: before_metadata exits too quickly for
        # asynchronous scope identity publication. It never loads model weights.
        argv = self.argv()+['--startup-memory-headroom-mib', str(headroom)]
        h.save(folder/'command.json', dict(argv=argv, binary=self.plan['binary'], before_memory=before))
        process, descriptor = None, None
        try:
            with (folder/'server.log').open('xb') as log:
                process = subprocess.Popen(argv, cwd=ROOT, env=self.environment(), stdout=log, stderr=log)
                descriptor = os.pidfd_open(process.pid)
                h.save(folder/'pid.json', dict(pid=process.pid,
                    stat=open(f'/proc/{process.pid}/stat').read()))
                rc = process.wait(timeout=90)
            text = (folder/'server.log').read_text()
            checks = validate_records(startup_records(text), headroom, checkpoint)
            h.require(rc == 1 and f'startup_memory_insufficient: {checkpoint}' in text,
                      'expected early failure absent')
            h.require('serving on port' not in text and '[q4t][startup] model_load' not in text and
                      '[q4t] model loaded' not in text, 'rejected startup entered serving/model load')
            if checkpoint == 'before_metadata':
                h.require('[q4t][capabilities]' not in text, 'metadata rejection was late')
            with socket.socket() as sock:
                sock.settimeout(1)
                h.require(sock.connect_ex(('127.0.0.1', self.port)) != 0, 'unexpected listener after startup failure')
            h.save(folder/'result.json', dict(passed=True, returncode=rc, checks=checks,
                after_memory=memory(), no_http_listener=True, model_weights_not_loaded=True))
        finally:
            if process is not None and process.poll() is None:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    process.wait(timeout=10)
            if descriptor is not None: os.close(descriptor)
            a.require_idle_gpu(folder/'gpu-after.json')

    def model(self):
        self.budget('starts')
        self.folder = self.out/'model'
        self.folder.mkdir()
        a.require_idle_gpu(self.folder/'gpu-before.json')
        argv = self.argv()
        h.save(self.folder/'command.json', dict(argv=argv, binary=self.plan['binary']))
        try:
            self.server = IsolatedScope(argv, cwd=ROOT, env=self.environment(),
                log_path=self.folder/'server.log', unit=f'q4t-startup-model-{os.getpid()}.scope',
                memory_max=None, evidence=self.folder/'scope')
            start = h.read(self.folder/'scope/identity.json')['start_ticks']
            self.monitor = observe.Monitor(self.server, start, self.folder)
            end = time.monotonic()+240
            while 'serving on port' not in (self.folder/'server.log').read_text():
                self.budget(); self.monitor.check()
                h.require(self.server.poll() is None and time.monotonic() < end, 'normal startup failed')
                time.sleep(.25)
            records = startup_records((self.folder/'server.log').read_text())
            checks = validate_records(records, 2048)
            response = self.http('GET', '/v1/config')
            h.save(self.folder/'configuration.json', response)
            config = json.loads(response['raw'])
            self.config_schema.validate(config)
            h.require(response['status'] == 200 and config['startup_memory']['checks'] == checks and
                      config['startup_memory']['enabled'] is True and
                      config['protocol_source_sha256'] == self.plan['protocol_sha256'] and
                      config['runtime']['mtp_enabled'] is True, 'actual startup config/identity')
            response = self.http('GET', '/v1/capabilities')
            h.save(self.folder/'capabilities.json', response)
            caps = json.loads(response['raw'])
            h.require(response['status'] == 200 and caps['startup_memory']['checkpoints'] == list(POINTS),
                      'startup capability missing')
            self.drain('before-business')
            self.normal(self.submit('ordinary-recovery', base.ordinary()))
            self.normal(self.submit('ordinary-sse', base.ordinary(stream=True)))
            call = self.submit('strict-json', base.constrained())
            value = call.finish(); self.check_wire(call)
            h.require(value.finish_reason == 'stop' and json.loads(value.message['content']) == {'ok':True},
                      'strict JSON actual task quality')
            # JsSdkClient makes one capability GET in addition to these model turns.
            h.require(self.controls < stage.LIMITS['observations'], 'SDK discovery reserve')
            self.controls += 1
            self.project_tool()
            after = self.drain('after-business')
            h.require(after['q4t_requests_total'] == after['q4t_requests_success_total'] == 5 and
                      after['q4t_requests_error_total'] == after['q4t_requests_aborted_total'] == 0 and
                      after['q4t_structured_workers_started_total'] ==
                      after['q4t_structured_workers_reaped_total'] == 2, 'business accounting')
            self.health('final')
            h.save(self.folder/'final-observation.json', observe.sample(self.server, start))
        finally:
            try:
                if self.monitor is not None: self.monitor.close()
            finally:
                if self.server is not None:
                    try:
                        if self.server.poll() is None: self.server.terminate()
                        rc = self.server.wait(timeout=45)
                        log = (self.folder/'server.log').read_text()
                        clean = rc == 0 and 'shutdown complete (0 in-flight remaining)' in log
                        h.save(self.folder/'shutdown.json', dict(clean=clean, returncode=rc))
                        h.require(clean, 'owned model shutdown')
                        end = self.server.snapshot('payload_exited')
                        observe.prior.check_oom(end['resources']['cgroup']['observations']['memory.events']['value'])
                    finally:
                        self.server.close()
                for call in self.calls:
                    call.thread.join(timeout=10)
                    h.require(not call.thread.is_alive(), 'owned client did not exit')
                a.require_idle_gpu(self.folder/'gpu-after.json')

    def run(self):
        result = dict(status='FAILED')
        try:
            # Verify the selected local port is available before any candidate.
            with socket.socket() as sock: sock.bind(('127.0.0.1', self.port))
            self.rejection(1048576, 'before_metadata')
            self.rejection(65536, 'before_allocations')
            self.model()
            accounting.run(self.plan, self.out/'accounting', self.budget)
            h.require(self.counts == stage.PLANNED, 'frozen real attempt counts')
            h.save(self.out/'wire-schema-result.json', dict(passed=True, records=self.wires))
            result.update(status='PASS_BOUNDED_STARTUP_MEMORY', physical_ram_hard_limit_proved=False,
                performance_acceptance=False, no_retry=True)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (a.EnvironmentAbort, observe.ObservationAbort))
                          else 'FAILED', error=repr(error))
            raise
        finally:
            result.update(counts=self.counts, control_requests=self.controls,
                          elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json', result)
