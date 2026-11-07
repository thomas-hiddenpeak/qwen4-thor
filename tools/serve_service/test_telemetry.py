"""CPU fault injection for managed NVML degradation; no NVML/GPU calls."""
import fcntl
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import package as pkg, service, telemetry
from tools.serve_service import test_service as old

guard = service.guard


class MonitorTests(unittest.TestCase):
    setUp = old.Case.setUp

    def make_monitor(self, results):
        self.now = 100.0
        self.query = mock.Mock(side_effect=results)
        out = self.root/'lease'
        out.mkdir()
        return telemetry.Monitor(out, query=self.query,
            clock=lambda: self.now, wall_clock=lambda: self.now+1000)

    def ok(self, pids=None):
        return dict(status='OK', pids=[] if pids is None else pids)

    def failed(self, status='TIMEOUT'):
        return dict(status=status, pids=None, **(dict(error=dict(
            type='NVMLError_Timeout', message='fixture', nvml_code=10))
            if status == 'ERROR' else {}))

    def test_unknown_before_launch_is_rejected(self):
        monitor = self.make_monitor([self.failed()])
        with self.assertRaisesRegex(RuntimeError, 'before managed launch'):
            monitor()
        self.assertIsNone(pkg.read(self.root/'lease/compute-monitor.json')['pids'])

    @mock.patch.object(guard, 'processes', return_value={})
    def test_failure_backoff_recovery_and_no_cached_pids(self, _):
        monitor = self.make_monitor([self.ok(), self.failed(), self.ok()])
        monitor()
        value = monitor(42)['compute_monitor']
        self.assertEqual(value['visibility'], 'UNKNOWN')
        self.assertIsNone(value['pids'])
        self.assertEqual(value['last_success']['pids'], [])
        self.now = 104.99
        self.assertIsNone(monitor(42)['compute_monitor']['pids'])
        self.assertEqual(self.query.call_count, 2)
        self.now = 105
        value = monitor(42)['compute_monitor']
        self.assertEqual((value['visibility'], value['recovered_gaps']), ('AVAILABLE', 1))
        self.assertEqual(value['completed_gap_seconds'], 5)
        self.assertTrue(monitor.had_gaps)

    @mock.patch.object(guard, 'processes', return_value={})
    def test_retry_budget_suspends_after_five_retries(self, _):
        monitor = self.make_monitor([self.ok()]+[self.failed('ERROR')]*6)
        monitor()
        value = monitor(42)['compute_monitor']
        for delay in (5, 10, 20, 40, 60):
            self.now += delay
            value = monitor(42)['compute_monitor']
        self.assertEqual(value['suspension_reason'], 'RETRY_LIMIT')
        self.now += 3600
        monitor(42)
        self.assertEqual(self.query.call_count, 7)
        self.assertEqual(value['failed_queries'], 6)

    @mock.patch.object(guard, 'processes', return_value={})
    def test_unreaped_query_stops_queries_and_can_be_reaped_later(self, _):
        monitor = self.make_monitor([self.ok(), self.failed('CLEANUP_FAILED')])
        worker = mock.Mock(pid=1234)
        worker.poll.return_value = None
        monitor()
        monitor.track_worker(worker)
        value = monitor(42)['compute_monitor']
        self.assertTrue(value['suspended'])
        self.assertFalse(value['query_worker_reaped'])
        self.now += 3600
        monitor(42)
        self.assertEqual(self.query.call_count, 1)
        self.assertFalse(monitor.close()['query_worker_reaped'])
        worker.poll.return_value = -9
        self.assertTrue(monitor.close()['query_worker_reaped'])
        self.assertTrue(monitor.snapshot()['suspended'])

    def test_protocol_and_log_errors_are_fatal(self):
        monitor = self.make_monitor([self.failed('PROTOCOL_ERROR')])
        with self.assertRaisesRegex(RuntimeError, 'worker protocol'):
            monitor(42)
        self.assertEqual(monitor.snapshot()['visibility'], 'UNKNOWN')
        self.assertTrue(monitor.snapshot()['fatal'])
        monitor = telemetry.Monitor(self.root/'lease', query=lambda **_: self.failed())
        with mock.patch.object(guard, 'open_log', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                monitor(42)

    def test_known_foreign_process_remains_a_conflict_during_outage(self):
        monitor = self.make_monitor([self.failed()])
        record = dict(pgid=99, ppid=1, start_ticks=3, state='S',
                      executable='/engine/q4t', argv=['q4t', 'serve'])
        with mock.patch.object(guard, 'processes', return_value={99: record}):
            row = monitor(42, {99: dict(record, start_ticks=2)})
        self.assertEqual([x['pid'] for x in row['conflicts']], [99])
        self.assertIsNone(row['conflicts'][0]['nvidia_compute_context'])

    @mock.patch.object(guard, 'processes', return_value={})
    def test_refresh_failure_keeps_known_foreign_and_unknown_owned_exit(self, _):
        monitor = self.make_monitor([self.ok([100, 200]), self.failed()])
        row = monitor(42, {100: dict(start_ticks=3)})
        self.assertEqual([x['pid'] for x in row['conflicts']], [200])
        self.assertEqual(row['unconfirmed_owned_contexts'], [100])
        self.assertEqual(row['retired_owned_contexts'], [])

    def test_real_cpu_payload_and_lease_survive_outage(self):
        # Fast retry timing is local to this CPU fixture, never package policy.
        out, lock = self.root/'guard', self.root/'lease.lock'
        calls = []
        def query(**_):
            calls.append(1)
            if len(calls) == 2:
                with lock.open('r+') as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self.failed()
            return self.ok()
        monitor = telemetry.Monitor(out, query=query)
        monitor.RETRY_SECONDS = (.01,)
        code = 'import time; time.sleep(.15)'
        with mock.patch.object(guard, 'gpu_command', return_value=False):
            rc = guard.run_guard([sys.executable, '-B', '-c', code], out, lock,
                interval=.01, grace=.2, inherit_lease=True, managed_monitor=monitor)
        result = pkg.read(out/'exit.json')
        self.assertEqual((rc, result['child_returncode']), (0, 0))
        self.assertEqual(result['status'], 'COMPLETED_WITH_MONITOR_GAPS')
        self.assertEqual(result['compute_monitor']['recovered_gaps'], 1)
        self.assertTrue(service.normal_stop(True, True, result))
        self.assertFalse(service.normal_stop(False, True, result))
        self.assertFalse(service.normal_stop(True, False, result))
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_monitor_worker_cleanup_failure_overrides_success(self):
        out, lock = self.root/'guard', self.root/'lease.lock'
        monitor = telemetry.Monitor(out, query=lambda **_: self.ok())
        worker = mock.Mock(pid=1234)
        worker.poll.return_value = None
        monitor.worker = worker
        with mock.patch.object(guard, 'gpu_command', return_value=False):
            rc = guard.run_guard([sys.executable, '-B', '-c', 'pass'], out, lock,
                interval=.01, grace=.2, inherit_lease=True, managed_monitor=monitor)
        result = pkg.read(out/'exit.json')
        self.assertEqual((rc, result['status']), (75, 'ENV_ABORT'))
        self.assertIn('monitor_cleanup_error', result)

    def test_default_guard_still_aborts_and_managed_policy_requires_inheritance(self):
        with mock.patch.object(guard.nvml_process_query, 'query', return_value=self.failed()):
            rc = guard.run_guard([sys.executable, '-c', 'pass'],
                self.root/'strict', self.root/'lease.lock')
        self.assertEqual(rc, 75)
        with self.assertRaisesRegex(ValueError, 'inherited NVML lease'):
            guard.run_guard([], self.root/'invalid', self.root/'lease.lock',
                            managed_monitor=object())
        self.assertFalse((self.root/'invalid').exists())
        with self.assertRaisesRegex(RuntimeError, 'restart-limit zero'):
            pkg.settings(18999, 1, nvml_policy='degrade')

    def test_query_hook_retains_real_timeout_worker_for_reaping(self):
        workers = []
        result = guard.nvml_process_query.supervise(
            [sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
            'cpu-only', .1, on_worker=workers.append)
        self.assertEqual(result['status'], 'TIMEOUT')
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].poll())

    @mock.patch.object(guard, 'processes', return_value={})
    def test_final_snapshot_does_not_refresh_or_clear_last_observed_gpu_context(self, _):
        monitor = self.make_monitor([self.ok([123])])
        monitor(42)
        self.now += 20
        final = monitor.close()
        self.assertEqual(final['visibility'], 'UNKNOWN')
        self.assertIsNone(final['pids'])
        self.assertEqual(final['last_success'], dict(monotonic=100, unix=1100, pids=[123]))
        self.assertEqual(final['last_query_completed_monotonic'], 100)
        self.assertEqual(final['post_cleanup_gpu_visibility'], 'NOT_QUERIED')
        self.assertFalse(monitor.had_gaps)
        self.assertEqual(self.query.call_count, 1)

    def test_live_status_exposes_health_and_monitor_separately(self):
        pkg.save(self.root/'controller.json', dict(owner=service.supervisor.process_identity(
            os.getpid())))
        pkg.save(self.root/'status.json', dict(state='READY'))
        (self.root/'lease').mkdir()
        pkg.save(self.root/'lease/compute-monitor.json', dict(visibility='UNKNOWN', pids=None))
        result = service.status(self.root)
        self.assertEqual(result['state'], 'READY')
        self.assertEqual(result['gpu_monitor']['visibility'], 'UNKNOWN')
        self.assertFalse(result['cleanup_confirmed'])

    def test_real_controller_wires_explicit_policy_and_stops_with_gap_receipt(self):
        out = self.root/'controller'
        manifest = dict(repository=str(ROOT), python=dict(path=sys.executable),
                        settings=pkg.settings(18999, 0, nvml_policy='degrade'))
        script = ("import json,time; from pathlib import Path; "
                  f"p=Path({str(out)!r}); "
                  "(p/'status.json').write_text(json.dumps(dict(state='STOPPED',cleanup_confirmed=True))); "
                  "(p/'stop.request.json').write_text('{}'); time.sleep(.08)")
        original = guard.run_guard
        def run(_command, path, lock, **kwargs):
            return original([sys.executable, '-B', '-c', script], path, lock,
                            interval=.01, grace=.2, **kwargs)
        queries = [self.ok(), self.failed()]+[self.ok()]*10
        with mock.patch.object(pkg, 'verify', return_value=manifest), \
             mock.patch.object(guard, 'common_lock', return_value=self.root/'lease.lock'), \
             mock.patch.object(guard, 'gpu_command', return_value=False), \
             mock.patch.object(guard.nvml_process_query, 'query', side_effect=queries), \
             mock.patch.object(guard, 'run_guard', side_effect=run):
            rc = service.run(self.root, 'digest', out)
        terminal = service.status(out)
        self.assertEqual((rc, terminal['state']), (0, 'STOPPED'))
        self.assertTrue(terminal['cleanup_confirmed'])
        self.assertEqual(terminal['guard']['status'], 'COMPLETED_WITH_MONITOR_GAPS')

    def test_cli_can_inherit_strict_lease_without_enabling_degradation(self):
        out = self.root/'cli'
        out.mkdir()
        pkg.save(out/'exit.json', dict(status='COMPLETED'))
        with mock.patch.object(sys, 'argv', ['gpu_exclusive.py', '--output', str(out),
                    '--inherit-lease', '--', 'cpu-only']), \
             mock.patch.object(guard, 'run_guard', return_value=0) as run, \
             mock.patch.object(guard.signal, 'signal'), \
             self.assertRaises(SystemExit) as exited:
            guard.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertTrue(run.call_args.kwargs['inherit_lease'])
        self.assertNotIn('managed_monitor', run.call_args.kwargs)


class PackageTests(unittest.TestCase):
    setUp = old.Case.setUp
    fixture = old.PackageTests.fixture

    def test_v8_binds_recovery_sources_and_v7_inventory_remains_readable(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              nvml_policy='degrade')
        target = Path(receipt['package'])
        value = pkg.read(target/'manifest.json')
        extra = {'source/tools/serve_service/nvml_policy.py',
                 'source/tools/serve_service/monitor_control.py'}
        self.assertTrue(extra <= value['files'].keys())
        # An old sealed package keeps its v7 inventory and pinned source bytes.
        # This fixture checks validation compatibility, not old code execution.
        value['version'] = 7
        for path in extra:
            del value['files'][path]
        pkg.save(target/'manifest.json', value)
        self.assertEqual(pkg.verify(target, pkg.sha(target/'manifest.json'))['version'], 7)
        value['version'] = 8
        pkg.save(target/'manifest.json', value)
        with self.assertRaisesRegex(RuntimeError, 'inventory differs'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_recovery_source_tampering_is_rejected(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              nvml_policy='degrade')
        target = Path(receipt['package'])/'source/tools/serve_service/monitor_control.py'
        target.chmod(0o600)
        target.write_text('# altered\n')
        with self.assertRaisesRegex(RuntimeError, 'package file changed'):
            pkg.verify(receipt['package'], receipt['manifest_sha256'])

    def test_explicit_policy_and_source_are_bound_and_defaults_remain_strict(self):
        binary, release, model = self.fixture()
        for mode, version in (('strict', 3), ('degrade', 8)):
            receipt = pkg.prepare(binary, release, model, self.root/mode,
                                  nvml_policy=mode)
            value = pkg.verify(receipt['package'], receipt['manifest_sha256'])
            self.assertEqual(value['version'], version)
            self.assertEqual(value['settings'].get('nvml_policy', 'strict'), mode)
            self.assertEqual('source/tools/serve_service/telemetry.py' in value['files'],
                             mode == 'degrade')
        target = self.root/'degrade'
        value = pkg.read(target/'manifest.json')
        del value['settings']['nvml_policy']
        pkg.save(target/'manifest.json', value)
        with self.assertRaisesRegex(RuntimeError, 'explicit degradation policy'):
            pkg.verify(target, pkg.sha(target/'manifest.json'))

    def test_policy_can_combine_with_operations_port_and_rejects_unknown(self):
        binary, release, model = self.fixture()
        receipt = pkg.prepare(binary, release, model, self.root/'package',
                              operations_port=18998, nvml_policy='degrade')
        value = pkg.verify(receipt['package'], receipt['manifest_sha256'])
        self.assertIn('--operations-port', pkg.command(receipt['package'], value))
        for invalid in ('ignore', True, None):
            with self.subTest(value=invalid), self.assertRaisesRegex(RuntimeError, 'NVML policy'):
                pkg.prepare(binary, release, model, self.root/'invalid', nvml_policy=invalid)
        self.assertFalse((self.root/'invalid').exists())

    def test_existing_client_and_resource_options_compose_with_v8(self):
        from tools.serve_service import resources
        binary, release, model = self.fixture()
        clients = self.root/'clients.json'
        pkg.save(clients, dict(version=1, gateway_token='a'*64, clients=[dict(
            id='alpha', max_inflight=1, max_control_inflight=2,
            max_prompt_tokens=4096, max_output_tokens=128, max_body_bytes=4096)]))
        # Only external executable identity discovery is mocked; package
        # inventories, policy validators and field binding are the real code.
        with mock.patch.object(resources, 'dependencies', return_value={}), \
             mock.patch.object(resources, 'verify_dependencies'):
            for has_clients in (False, True):
                for has_resources in (False, True):
                    for operations in (0, 18998):
                        receipt = pkg.prepare(binary, release, model,
                            self.root/f'v8-{has_clients}-{has_resources}-{operations}',
                            client_policy=clients if has_clients else None,
                            host_resources=resources.policy(32, 64, 8) if has_resources else None,
                            operations_port=operations, nvml_policy='degrade')
                        value = pkg.verify(receipt['package'], receipt['manifest_sha256'])
                        self.assertEqual(value['version'], 8)
                        self.assertEqual('client_policy' in value, has_clients)
                        self.assertEqual('host_resources' in value, has_resources)
                        self.assertEqual(value['settings'].get('operations_port', 0), operations)


if __name__ == '__main__':
    unittest.main()
