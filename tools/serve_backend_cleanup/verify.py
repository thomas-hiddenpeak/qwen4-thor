"""Frozen real business, internal teardown and tiny CUDA status contracts."""
import json
import os
import socket
import subprocess
import time

from tools.serve_backend_cleanup import stage
from tools.serve_device_memory import verify as prior
from tools.serve_device_memory.correction import Run as FullWireRun
from tools.serve_lifecycle import observe
from tools.serve_observation_capacity.cpu import command
from isolated_scope import IsolatedScope

h, a, ROOT = stage.h, stage.a, stage.ROOT
base = prior.base
APIS = ('cudaFree', 'cudaFreeAsync', 'cudaFreeHost', 'cudaEventDestroy',
        'cudaStreamDestroy', 'cudaStreamSynchronize', 'cudaDeviceSynchronize',
        'cudaEventSynchronize')
# name, API, phase, expected exit. Only legacy MTP uses the existing load hook.
SCENARIOS = (
    ('model', None, None, 0),
    ('legacy-mtp', None, None, 1),
    ('mtp-release', 'cudaFree', 'mtp.release', 1),
    ('model-release', 'cudaFree', 'model.release', 1),
    ('model-host-release', 'cudaFreeHost', 'model.release', 1),
    ('model-event-release', 'cudaEventDestroy', 'model.release', 1),
    ('handled-release', 'cudaFree', 'request.prompt_trunk_free', 1),
)


def environment(api=None, phase=None, *, legacy=False):
    env = prior.fault_environment(None, None)
    if api:
        env.update(Q4T_CLEANUP_FAULT_API=api, Q4T_CLEANUP_FAULT_PHASE=phase)
    if legacy:
        env['Q4T_SERVE_DEVICE_FAULT'] = 'startup.mtp_load'
    return env


def probe_cases():
    rows = [(api, 'all', api, 'probe', 1, False) for api in APIS]
    rows += [
        ('forward', 'all', None, None, 0, False),
        ('production-hook-absent', 'all', 'cudaFree', 'probe', 0, True),
        ('handled', 'handled-release', 'cudaFree', 'probe', 0, False),
        ('scratch-fatal', 'mtp-scratch', 'cudaFree', 'mtp.scratch', 1, False),
        ('load-drain-fatal', 'mtp-load', 'cudaStreamSynchronize', 'mtp.load', 1, False),
        ('unhanded-fatal', 'reservation-release', 'cudaFree', 'probe', 1, False),
        ('scratch-success', 'mtp-scratch', None, None, 0, False),
        ('load-cleanup-success', 'mtp-load', None, None, 0, False),
        ('unhanded-success', 'reservation-release', None, None, 0, False),
    ]
    return rows


def probe_outcome(code, stdout, stderr, *, expected, api, phase, production):
    h.require(code == expected, 'probe exit code')
    injected = api is not None and not production
    h.require(stderr.count('[cleanup-test]') == int(injected), 'probe injection count')
    if injected:
        h.require(f'api={api} phase={phase} error=1' in stderr, 'probe injection target')
    h.require(('[q4t][cleanup-fatal]' in stderr) == (expected == 1), 'fatal propagation')
    h.require(('cleanup probe PASS:' in stdout) == (expected == 0), 'post-failure continuation')


def native(plan, folder):
    folder.mkdir()
    a.require_idle_gpu(folder/'gpu-before.json')
    passed = False
    try:
        ok = command(folder, 'mtp-lifecycle', [plan['mtp_lifecycle_probe']['path'],
            str(folder/'lifecycle-fixture'), plan['model_dir']], timeout=180)
        log = (folder/'mtp-lifecycle.stdout').read_text()
        h.require(ok and 'MTP lifecycle: PASS' in log and 'real load: success' in log,
                  'MTP lifecycle (including actual load)')
        for name, mode, api, phase, expected, production in probe_cases():
            case = folder/name
            case.mkdir()
            binary = plan['cleanup_probe' if production else 'cleanup_validation_probe']
            argv = [binary['path'], mode]
            if mode == 'mtp-load': argv.append(str(case/'fixture'))
            h.save(case/'command.json', dict(argv=argv, binary=binary, api=api,
                phase=phase, expected_exit=expected, production=production))
            completed = subprocess.run(argv, cwd=ROOT, env=environment(api, phase),
                capture_output=True, text=True, timeout=45)
            (case/'stdout').write_text(completed.stdout)
            (case/'stderr').write_text(completed.stderr)
            h.save(case/'exit.json', dict(returncode=completed.returncode))
            probe_outcome(completed.returncode, completed.stdout, completed.stderr,
                expected=expected, api=api, phase=phase, production=production)
        passed = True
    finally:
        a.require_idle_gpu(folder/'gpu-after.json')
        h.save(folder/'result.json', dict(passed=passed, planned_probes=len(probe_cases()),
            mtp_lifecycle_processes=1, actual_mtp_load_attempts_with_weights=3,
            negative_status_only=True, physical_oom_or_gpu_damage=False))


class Run(prior.Run):
    # Capture to HTTP EOF before parsing. The older streaming client stops at
    # the error envelope, which is insufficient evidence for the DONE tail.
    failure = FullWireRun.failure

    def __init__(self, plan, output):
        super().__init__(plan, output)
        self.deadline = plan['started'] + stage.LIMITS['seconds'] - 90

    def budget(self, kind=None):
        h.require(time.time() < self.deadline, 'frozen cleanup stage deadline')
        if kind:
            h.require(self.counts[kind] < stage.LIMITS[kind], 'attempt limit: '+kind)
            self.counts[kind] += 1
            h.save(self.out/f'attempt-{kind}-{self.counts[kind]:02d}.json',
                   dict(kind=kind, counts=self.counts.copy(), time=time.time()))

    def http(self, method, path, body=None, *, timeout=5):
        h.require(self.controls < stage.LIMITS['observations'], 'observation limit')
        return super().http(method, path, body, timeout=timeout)

    def scenario(self, name, api, phase, expected_exit):
        self.budget('starts')
        self.folder = self.out/name
        self.folder.mkdir()
        self.server = None
        a.require_idle_gpu(self.folder/'gpu-before.json')
        binary = self.plan['binary' if name == 'model' else 'validation_binary']
        argv = [binary['path'], 'serve', '--model-dir', self.plan['model_dir'],
                '--host', '127.0.0.1', '--port', str(self.port),
                '--max-prefill', '2048', '--max-len', '8192', '--max-seq', '1',
                '--max-inflight-requests', '8', '--max-queued-requests', '2']
        if name == 'legacy-mtp': argv += ['--startup-plan', 'off']
        h.save(self.folder/'command.json', dict(argv=argv, binary=binary, api=api,
            phase=phase, expected_exit=expected_exit, simulated_status_only=name != 'model'))
        passed = False
        try:
            self.server = IsolatedScope(argv, cwd=ROOT,
                env=environment(api, phase, legacy=name == 'legacy-mtp'),
                log_path=self.folder/'server.log',
                unit=f'q4t-cleanup-{name}-{os.getpid()}.scope',
                memory_max=None, evidence=self.folder/'scope')
            start = h.read(self.folder/'scope/identity.json')['start_ticks']
            if name == 'legacy-mtp':
                self.budget('negatives')
                rc = self.server.wait(timeout=240)
                log = (self.folder/'server.log').read_text()
                h.require(rc == 1 and 'serving on port' not in log and
                    'requested MTP load failed' in log and
                    'plain decode only' not in log and
                    log.count('[serve-test-device]') == 1,
                    'requested legacy MTP did not refuse startup')
            else:
                self.monitor = observe.Monitor(self.server, start, self.folder)
                end = time.monotonic()+240
                while 'serving on port' not in (self.folder/'server.log').read_text():
                    self.budget(); self.monitor.check()
                    h.require(self.server.poll() is None and time.monotonic() < end,
                              'startup failed: '+name)
                    time.sleep(.25)
                prior.validate_records(prior.startup_records(
                    (self.folder/'server.log').read_text()), 2048)
                response = self.http('GET', '/v1/capabilities')
                h.save(self.folder/'capabilities.json', response)
                caps = json.loads(response['raw'])
                h.require(response['status'] == 200 and caps['backend_cleanup']['scope'] ==
                    'production_q4t_direct_cuda_calls' and not
                    caps['backend_cleanup']['error_response_guaranteed'], 'cleanup discovery scope')
                response = self.http('GET', '/v1/config')
                h.save(self.folder/'configuration.json', response)
                self.config_schema.validate(json.loads(response['raw']))
                h.require(response['status'] == 200 and json.loads(response['raw'])[
                    'protocol_source_sha256'] == self.plan['protocol_sha256'], 'protocol identity')
                self.drain('before-business')
                if name == 'model':
                    self.production()
                elif name == 'handled-release':
                    h.save(self.folder/'before-fatal-observation.json', observe.sample(self.server, start))
                    self.stop_monitor()
                    self.failure(base.ordinary(stream=True), 'gpu_unhealthy')
                    h.require(self.server.wait(timeout=45) == 1, 'handled release did not stop')
                else:
                    self.success('before-shutdown', base.ordinary())
                    self.drain('before-shutdown')
                    self.budget('negatives')
                if name != 'handled-release':
                    self.health('final')
                    h.save(self.folder/'final-observation.json', observe.sample(self.server, start))
                self.stop_monitor()
            if self.server.poll() is None: self.server.terminate()
            rc = self.server.wait(timeout=45)
            log = (self.folder/'server.log').read_text()
            h.require(rc == expected_exit, 'unexpected exit: '+name)
            if api:
                h.require(log.count('[cleanup-test]') == 1 and
                    f'api={api} phase={phase} error=1' in log, 'missing exact internal failure')
                if name == 'handled-release':
                    h.require('[q4t][cleanup-fatal]' not in log and
                        '[q4t][device-memory]' in log, 'handled release bypassed response owner')
                else:
                    h.require(log.count('[q4t][cleanup-fatal]') == 1,
                              'cleanup failure did not exit immediately')
            else:
                h.require('[cleanup-test]' not in log, 'unexpected cleanup injection')
            if expected_exit == 0:
                h.require('shutdown complete (0 in-flight remaining)' in log and
                          '[serve-test-device]' not in log, 'normal stop/hook contract')
            end = self.server.snapshot('payload_exited')
            observe.prior.check_oom(end['resources']['cgroup']['observations']['memory.events']['value'])
            h.save(self.folder/'result.json', dict(passed=True, returncode=rc,
                api=api, phase=phase, simulated_status_only=name != 'model'))
            passed = True
        finally:
            try:
                self.stop_monitor()
            finally:
                if self.server:
                    try:
                        if self.server.poll() is None:
                            self.server.terminate()
                            try: self.server.wait(timeout=45)
                            except subprocess.TimeoutExpired:
                                self.server.kill(); self.server.wait(timeout=10)
                        if not passed:
                            h.save(self.folder/'cleanup.json', dict(returncode=self.server.poll(), accepted=False))
                    finally: self.server.close()
                for call in self.calls:
                    call.thread.join(timeout=10)
                    h.require(not call.thread.is_alive(), 'owned client still running')
                a.require_idle_gpu(self.folder/'gpu-after.json')

    def run(self):
        result = dict(status='FAILED')
        try:
            with socket.socket() as sock: sock.bind(('127.0.0.1', self.port))
            for row in SCENARIOS: self.scenario(*row)
            h.require(self.counts == stage.PLANNED, 'frozen attempt count changed')
            h.save(self.out/'wire-schema-result.json', dict(passed=True, records=self.wires))
            result.update(status='PASS_BOUNDED_BACKEND_CLEANUP',
                physical_oom_or_gpu_damage=False, performance_acceptance=False, no_retry=True)
        except BaseException as error:
            result.update(status='ENV_ABORT' if isinstance(error, (a.EnvironmentAbort,
                observe.ObservationAbort)) else 'FAILED', error=repr(error))
            raise
        finally:
            result.update(counts=self.counts, control_requests=self.controls,
                          elapsed_seconds=time.monotonic()-self.started)
            h.save(self.out/'result.json', result)
