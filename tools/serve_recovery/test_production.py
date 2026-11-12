"""CPU-only production-controller dependencies and public service contracts."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import controller, core_http as http, jobs, ledger, monitor, ownership, policy, service


class Clock:
    def __init__(self):
        self.now = 10.0
    def __call__(self):
        return self.now


class FakeJob:
    def __init__(self, request, result, reason=None):
        self.output = (ledger.canonical(dict(request_id=request, event='result', **result))+b'\n'
                       if result else b'')
        self.process = mock.Mock(pid=123, returncode=0 if result and result['status'] == 'OK' else -9)
        self.process.poll.return_value = self.process.returncode
        self.reason = reason
        self.unreaped = False
        self.done = True
        self.closed = False
    def tick(self):
        return self.done
    def close(self):
        self.closed = True
    def kill(self, reason):
        self.reason = reason


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.jobs = []
        self.result = dict(status='OK', device_count=1, pids=[], error=None, shutdown_error=None)
        self.reason = None
        def factory(command, **_kwargs):
            self.assertTrue(not self.jobs or self.jobs[-1].closed)
            job = FakeJob(command[-1], self.result, self.reason)
            self.jobs.append(job)
            return job
        self.monitor = monitor.Monitor(50, factory=factory, clock=self.clock)

    def query(self):
        self.monitor.tick()
        self.monitor.tick(allow_query=False)

    def test_success_is_fresh_and_empty_is_only_from_valid_query(self):
        self.query()
        self.assertEqual(self.monitor.visibility, 'AVAILABLE')
        self.assertEqual(self.monitor.pids, [])
        self.assertEqual(self.monitor.successes, 1)
        self.monitor.refresh_after_cleanup()
        self.assertEqual(self.monitor.visibility, 'UNKNOWN')
        self.assertIsNone(self.monitor.pids)

    def test_timeout_backoff_then_single_half_open_query(self):
        self.result, self.reason = None, 'timeout'
        for delay in (5, 10, 20, 40, 60, 300, 300):
            self.query()
            self.assertEqual(self.monitor.visibility, 'UNKNOWN')
            self.assertIsNone(self.monitor.pids)
            self.assertEqual(self.monitor.next_at-self.clock.now, delay)
            count = len(self.jobs)
            self.monitor.tick()
            self.assertEqual(len(self.jobs), count)
            self.clock.now = self.monitor.next_at
        self.assertTrue(self.monitor.half_open)
        self.result, self.reason = dict(status='OK', device_count=1, pids=[], error=None), None
        self.query()
        self.assertEqual(self.monitor.visibility, 'AVAILABLE')
        self.assertEqual(self.monitor.failures, 7)
        self.assertFalse(self.monitor.half_open)

    def test_collector_exception_does_not_become_empty_or_model_action(self):
        self.monitor.factory = mock.Mock(side_effect=OSError('collector failed'))
        self.monitor.tick()
        self.assertFalse(self.monitor.fatal)
        self.assertIsNone(self.monitor.pids)
        self.assertIsNone(self.monitor.next_at)
        self.monitor.tick()
        self.monitor.factory.assert_called_once()

    def test_unreaped_worker_blocks_replacement_until_actual_reap(self):
        self.monitor.tick()
        job = self.jobs[-1]
        job.unreaped, job.done = True, False
        job.process.poll.return_value = None
        job.process.returncode = None
        for _ in range(3):
            self.clock.now += 600
            self.monitor.tick()
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(job.closed)
        self.assertEqual(self.monitor.category, 'WORKER_UNREAPED')
        job.unreaped, job.done, job.reason = False, True, 'timeout'
        job.process.poll.return_value = -9
        job.process.returncode = -9
        self.monitor.tick(allow_query=False)
        self.assertTrue(job.closed)
        self.assertIsNone(self.monitor.job)

    def test_fatal_event_is_preserved_before_shutdown_or_timeout(self):
        self.monitor.tick()
        job = self.jobs[-1]
        job.output = ledger.canonical(dict(request_id=self.monitor.request_id, event='failure',
            error=dict(type='NVMLError', message='device lost', nvml_code=15)))+b'\n'
        job.done = False
        self.monitor.tick()
        self.assertTrue(self.monitor.fatal)
        self.assertEqual(self.monitor.decision.nvml_code, 15)
        job.done, job.reason = True, 'timeout'
        self.monitor.tick()
        self.assertTrue(self.monitor.fatal)
        self.assertEqual(len(self.jobs), 1)

    def test_protocol_corruption_and_wrong_worker_are_not_success(self):
        for raw in (b'{', b'{"status":"OK"}\n',
                    ledger.canonical(dict(request_id='wrong', event='result', **self.result))+b'\n'):
            result = monitor.decode(raw, 'right', 0)
            self.assertNotEqual(monitor.nvml_policy.classify(result).category, 'OK')

    def test_malformed_prefix_or_excess_events_cannot_hide_later_device_failure(self):
        def event(code):
            return ledger.canonical(dict(request_id='owned', event='failure',
                error=dict(type='NVMLError', message='failure', nvml_code=code)))+b'\n'
        for prefix in (b'{broken\n', event(10)*3):
            result = monitor.decode(prefix+event(15), 'owned', -9, 'timeout')
            self.assertEqual(monitor.nvml_policy.classify(result).action, 'fatal')

    def test_cleanup_refresh_does_not_erase_failed_query_backoff(self):
        self.result, self.reason = None, 'timeout'
        self.query()
        when = self.monitor.next_at
        self.monitor.refresh_after_cleanup()
        self.assertEqual(self.monitor.next_at, when)
        self.assertEqual(self.monitor.failures, 1)


class JobTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'build/recovery-production'))
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.fd = os.open(Path(self.temp.name)/'private-cpu-lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.job = None

    def tearDown(self):
        if self.job is not None:
            self.job.kill('test_cleanup')
            jobs.finish(self.job, lambda: False, timeout=2)
        os.close(self.fd)
        self.temp.cleanup()

    def start(self, code, timeout=1, maximum=65536):
        self.job = jobs.Job([sys.executable, '-B', '-c', code, str(self.fd)],
                            lease_fd=self.fd, timeout=timeout, maximum=maximum)

    def finish(self):
        result = jobs.finish(self.job, lambda: False, timeout=3)
        self.assertIsNotNone(result)
        self.job = None
        return result

    def test_cpu_exec_inherits_continuous_lease_without_reacquiring(self):
        self.start('import pathlib,sys; print("FLOCK" in pathlib.Path("/proc/self/fdinfo/"+sys.argv[1]).read_text())')
        self.assertEqual(self.finish()['output'], b'True\n')

    def test_job_timeout_kills_and_reaps_only_owned_child(self):
        self.start('import time; time.sleep(20)', timeout=0.1)
        pid = self.job.process.pid
        result = self.finish()
        self.assertEqual(result['reason'], 'timeout')
        self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_job_output_bound_is_enforced_without_unbounded_capture(self):
        self.start('import os; os.write(1,b"x"*1000000)', maximum=4096)
        result = self.finish()
        self.assertEqual(result['reason'], 'output_limit')
        self.assertEqual(len(result['output']), 4096)

    def test_pin_failure_retains_worker_slot_until_actual_reap(self):
        with mock.patch.object(jobs.os, 'pidfd_open', side_effect=OSError('pin unavailable')):
            self.start('import time; time.sleep(20)')
        pid = self.job.process.pid
        result = self.finish()
        self.assertEqual(result['reason'], 'setup_failed')
        self.assertFalse(Path(f'/proc/{pid}').exists())


class ControllerTests(unittest.TestCase):
    def test_changed_bundle_quarantines_before_spending_model_budget(self):
        actor = object.__new__(controller.Controller)
        actor.package, actor.config = ROOT, dict(manifest_sha256='a'*64)
        actor.quarantine, actor.transaction = mock.Mock(), mock.Mock()
        with mock.patch.object(controller.bundle, 'verify', side_effect=ValueError('changed')):
            self.assertFalse(actor.load({}))
        actor.quarantine.assert_called_once_with('identity_invalid')
        actor.transaction.assert_not_called()

    def test_self_check_uses_eligible_metered_client_or_rejects_configuration(self):
        clients = [dict(id='small', max_prompt_tokens=64, max_output_tokens=8,
                        tokens_per_minute=100),
                   dict(id='check', max_prompt_tokens=512, max_output_tokens=64,
                        tokens_per_minute=576)]
        self.assertEqual(http.self_check_client(dict(clients=clients)), 'check')
        clients[1]['tokens_per_minute'] = 575
        with self.assertRaises(ValueError):
            http.self_check_client(dict(clients=clients))
        clients[1]['tokens_per_minute'] = 0
        self.assertEqual(http.self_check_client(dict(clients=clients)), 'check')

    def test_core_health_separates_context_from_dependency_failure(self):
        self.assertIsNone(controller.health_fault(dict(status='ok', gpu_healthy=True), 200))
        self.assertEqual(controller.health_fault(dict(status='unhealthy', gpu_healthy=False), 503),
                         policy.FaultKind.CUDA_CONTEXT)
        self.assertEqual(controller.health_fault(dict(status='unhealthy', gpu_healthy=True), 503),
                         policy.FaultKind.PLE_UNAVAILABLE)

    def test_invalid_health_is_unknown_not_confirmed_model_failure(self):
        for value, status in (({}, 503), (dict(status='ok', gpu_healthy=1), 200),
                (dict(status='ok', gpu_healthy=False), 200),
                (dict(status='unhealthy', gpu_healthy=False), 200)):
            with self.assertRaises(ValueError):
                controller.health_fault(value, status)

    def test_unconfirmed_exited_compute_is_not_external_live_competition(self):
        records = {10: dict(start_ticks=100, ppid=1, argv=[], executable=''),
                   11: dict(start_ticks=101, ppid=10, argv=[], executable=''),
                   12: dict(start_ticks=102, ppid=1, argv=[], executable='')}
        with mock.patch.object(ownership, 'snapshot', return_value=records):
            observed = ownership.observe(10, 100, [11, 99])
            self.assertEqual(observed, dict(conflicts=[], unconfirmed_compute=[99]))
            self.assertEqual(ownership.observe(10, 100, [12])['conflicts'], [12])
            self.assertEqual(ownership.observe(10, 100, None)['conflicts'], [])

    def test_changed_ancestry_root_is_not_owned(self):
        with self.assertRaises(ValueError):
            ownership.descendants({10: dict(start_ticks=101, ppid=1)}, 10, 100)

    def test_self_check_requires_typed_strict_json_and_complete_response(self):
        good = dict(id='cpu', usage=dict(completion_tokens=5), choices=[dict(
            finish_reason='stop', message=dict(role='assistant', content='{"ok":true}'))])
        for content, finish, tools in (('{"ok":true}', 'stop', []),
            ('{"ok":1}', 'stop', []), ('{"ok":true}', 'length', []),
            ('{"ok":true}', 'stop', [dict(id='forbidden')])):
            good['choices'][0].update(finish_reason=finish,
                message=dict(role='assistant', content=content, tool_calls=tools))
            with mock.patch.object(http, 'request', return_value=dict(http_status=200,
                    body=json.dumps(good))) as request:
                if content == '{"ok":true}' and finish == 'stop' and not tools:
                    self.assertTrue(http.self_check(18080, dict(client='alpha', token='a'*64))['strict_json'])
                else:
                    with self.assertRaises(ValueError):
                        http.self_check(18080, dict(client='alpha', token='a'*64))
                self.assertEqual(request.call_count, 1)


class PublicEntryTests(unittest.TestCase):
    def setUp(self):
        base = ROOT/'build/recovery-entry-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.state = Path(self.temp.name)/'state'
        self.package = ROOT/'build/cpu-package-placeholder'

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self):
        with mock.patch.object(service.bundle, 'verify', return_value=dict(repository=str(ROOT))), \
             mock.patch.object(service.subprocess, 'Popen') as spawn:
            result = service.initialize(self.package, 'a'*64, self.state)
            spawn.assert_not_called()
            return result

    def test_initialize_creates_stopped_identity_without_any_process(self):
        result = self.initialize()
        self.assertEqual(result['desired'], 'STOPPED')
        self.assertFalse(result['automatic_start'])
        with mock.patch.object(service.bundle, 'verify', return_value=dict(repository=str(ROOT))):
            with self.assertRaises(FileExistsError):
                service.initialize(self.package, 'a'*64, self.state)

    def test_changed_bundle_cannot_reuse_service_identity(self):
        self.initialize()
        with self.assertRaises(ValueError):
            service.descriptor(self.state, self.package, 'b'*64)

    def test_missing_live_guardian_never_uses_old_ready_snapshot(self):
        self.initialize()
        (self.state/'status.json').write_text('{"state":"READY","business_ready":true}')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = service.main(['status', '--state', str(self.state)],
                                  package=self.package, digest='a'*64)
        self.assertEqual(result, 1)
        value = json.loads(output.getvalue())
        self.assertEqual(value['state'], 'UNKNOWN')
        self.assertFalse(value['business_ready'])


if __name__ == '__main__':
    unittest.main()
