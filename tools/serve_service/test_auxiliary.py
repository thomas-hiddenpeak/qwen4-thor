"""CPU fault isolation contracts. No NVML, CUDA or model execution."""
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import auxiliary, monitor_control, package as pkg
from tools.serve_service import service, supervisor as sup, telemetry
from tools.serve_service import test_service as old
from tools.serve_service.test_nvml_policy import failure

guard = service.guard


def cpu_supervisor(root):
    """Separate CPU controller with a noisy, progressing backend child."""
    fixture = old.Case()
    fixture.root = root
    calls = []
    config = dict(sup.expected_configuration(), protocol_sha256='protocol')
    original_write = sup.RotatingLog.write

    def write(logger, data):
        if logger.path.name == 'backend.log':
            raise OSError(errno.ENOSPC, 'fixture sink full')
        return original_write(logger, data)

    def get(_port, path, **_options):
        if path == '/metrics':
            (root/'unexpected-metrics').write_text('called')
            raise sup.contracts.InvalidObservation('missing_required_metric')
        if path == '/v1/config':
            return dict(http_status=200, body='{"observability_version":4}')
        if path == '/readyz':
            calls.append(1)
            if (root/'done.pid').exists() or len(calls) > 300:
                pkg.save(root/'runtime/stop.request.json', {})
        return dict(http_status=200,
                    body='{"live":true,"ready":true,"status":"ready"}')

    script = f'''import os,time
from pathlib import Path
r=Path({str(root)!r})
(r/'start.pid').write_text(str(os.getpid()))
for i in range(128):
    data=b'x'*16384
    while data: data=data[os.write(1,data):]
    (r/'progress').write_text(str(i+1))
    time.sleep(.002)
(r/'done.pid').write_text(str(os.getpid()))
time.sleep(30)
'''
    with mock.patch.object(sup.identity, 'process', side_effect=lambda pid, _:
            dict(pid=pid, executable_sha256='binary', listener_scope='loopback')), \
         mock.patch.object(sup.transport, 'get', side_effect=get), \
         mock.patch.object(sup.contracts, 'configuration', return_value=config):
        rc, state = fixture.run_supervisor(None, isolated=True, script=script,
            config_overrides=dict(startup_seconds=2, unavailable_seconds=.05),
            patch=mock.patch.object(sup.RotatingLog, 'write', write))
    pkg.save(root/'supervisor-result.json', state)
    return rc


class IsolationTests(unittest.TestCase):
    setUp = old.Case.setUp
    run_supervisor = old.Case.run_supervisor
    observation = old.Case.observation

    def monitor(self, query):
        out = self.root/'lease'
        out.mkdir()
        return telemetry.Monitor(out, query=query, clock=lambda: self.now,
            resume_path=self.root/monitor_control.REQUEST_NAME)

    def test_io_backoff_and_recovery_preserve_loss_history(self):
        self.now = 100
        ledger = auxiliary.Observations(clock=lambda: self.now)
        operation = mock.Mock(side_effect=OSError(errno.ENOSPC, 'fixture'))
        self.assertFalse(ledger.perform('backend_log', operation, size=10))
        for _ in range(100):
            self.assertFalse(ledger.perform('backend_log', operation, size=10))
        self.assertEqual(operation.call_count, 1)
        self.now += 60
        operation.side_effect = None
        self.assertTrue(ledger.perform('backend_log', operation, size=10))
        row = ledger.snapshot()['backend_log']
        self.assertEqual(row['state'], 'AVAILABLE')
        self.assertEqual((row['failed_operations'], row['skipped_operations']), (1, 100))
        self.assertEqual(row['dropped_bytes'], 1010)
        self.assertTrue(ledger.had_gaps)
        with self.assertRaises(ValueError):
            ledger.perform('unbounded arbitrary name', operation)

    def test_snapshot_fsync_error_isolated_with_no_temp_left_behind(self):
        ledger = auxiliary.Observations()
        for number in (errno.ENOSPC, errno.EROFS, errno.EACCES):
            ledger = auxiliary.Observations()
            path = self.root/f'{number}.json'
            with mock.patch.object(pkg.os, 'fsync', side_effect=OSError(number, 'fixture')):
                self.assertFalse(ledger.save(path, {'state': 'READY'}, 'status'))
            self.assertEqual(ledger.snapshot()['status']['errno'], number)
            self.assertFalse(path.with_name(path.name+'.new').exists())

    @mock.patch.object(guard, 'processes', return_value={})
    def test_collector_exception_suspends_and_does_not_spawn_more_workers(self, _):
        self.now = 100
        query = mock.Mock(side_effect=ValueError('collector bug'))
        monitor = self.monitor(query)
        for _ in range(100):
            monitor(42)
            self.now += 60
        value = monitor.snapshot()
        self.assertEqual(query.call_count, 1)
        self.assertEqual(value['query_category'], 'QUERY_ERROR')
        self.assertTrue(value['suspended'])
        self.assertFalse(value['fatal'])
        self.assertIsNone(value['pids'])

    def test_collector_exception_keeps_unreaped_worker_handle(self):
        self.now = 100
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(30)'])
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        self.addCleanup(cleanup)
        def query(**options):
            options['on_worker'](child)
            raise OSError('collector transport failed')
        monitor = self.monitor(query)
        with self.assertRaises(telemetry.Unavailable):
            monitor.compute()
        self.assertIs(monitor.worker, child)
        self.assertFalse(monitor.snapshot()['query_worker_reaped'])
        self.now += 60
        self.assertEqual(monitor_control.denial(monitor.snapshot(), self.now),
                         'WORKER_UNREAPED')
        cleanup()
        self.assertIsNone(monitor_control.denial(monitor.snapshot(), self.now))

    def test_device_exception_remains_fatal_even_when_log_and_snapshot_fail(self):
        self.now = 100
        class DeviceError(Exception):
            value = 15
        monitor = self.monitor(mock.Mock(side_effect=DeviceError('fixture')))
        with mock.patch.object(auxiliary.BoundedLog, 'write', side_effect=OSError('full')), \
             mock.patch.object(pkg, 'save', side_effect=OSError('read only')):
            with self.assertRaisesRegex(RuntimeError, 'DEVICE_FAILURE'):
                monitor.compute()
        self.assertTrue(monitor.state['fatal'])
        self.assertTrue(monitor.had_gaps)

    def test_resume_authorization_write_failure_never_issues_query(self):
        self.now = 100
        query = mock.Mock(return_value=failure(3))
        monitor = self.monitor(query)
        with self.assertRaises(telemetry.Unavailable):
            monitor.compute()
        self.now += 60
        request = dict(version=1, request_id='a'*32,
            generation=monitor.state['generation'],
            suspension_epoch=monitor.state['suspension_epoch'])
        pkg.save(monitor.resume_path, request)
        original = pkg.save
        def save(path, value):
            if Path(path).name == 'monitor.resume.accepted.json':
                raise OSError(errno.ENOSPC, 'control storage full')
            return original(path, value)
        with mock.patch.object(pkg, 'save', side_effect=save):
            monitor.poll_resume()
        self.assertTrue(monitor.state['suspended'])
        self.assertFalse(monitor.resume_probe)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'CONTROL_WRITE_FAILED')
        self.assertEqual(monitor.state['resume_attempts'], 1)
        with self.assertRaises(telemetry.Unavailable):
            monitor.compute()
        self.assertEqual(query.call_count, 1)

    def test_core_health_probe_never_calls_metrics_and_keeps_device_protection(self):
        config = dict(sup.expected_configuration(), protocol_sha256='protocol')
        health = dict(live=True, ready=True, status='ready')
        paths = []
        def get(_port, path, **_):
            paths.append(path)
            if path == '/metrics':
                raise AssertionError('auxiliary request entered health path')
            return dict(http_status=(503 if path == '/readyz' and not health['ready'] else 200),
                        body=json.dumps(dict(observability_version=4) if
                                        path == '/v1/config' else health))
        owner = dict(executable_sha256='binary', listener_scope='loopback')
        with mock.patch.object(sup.identity, 'process', return_value=owner), \
             mock.patch.object(sup.transport, 'get', side_effect=get), \
             mock.patch.object(sup.contracts, 'configuration', return_value=config):
            for status, expected in (('ready', 'READY'), ('unhealthy', 'BACKEND_UNHEALTHY')):
                health.update(status=status, ready=status == 'ready')
                value = sup.probe(42, 18999, include_metrics=False)
                self.assertEqual(sup.classify(value, 'protocol', 'binary'), expected)
                self.assertIsNone(value['metrics'])
        self.assertNotIn('/metrics', paths)

    def test_isolated_supervisor_preserves_core_http_deadline(self):
        seen = []
        def observe(pid, _):
            seen.append(pid)
            if len(seen) == 1:
                return self.observation(pid)
            raise sup.contracts.InvalidObservation('http_deadline')
        rc, state = self.run_supervisor(observe, isolated=True)
        self.assertEqual((rc, state['attempts'], state['restarts']), (1, 1, 0))
        self.assertEqual(state['error'], 'observation deadline exceeded')

    def test_isolated_supervisor_stops_unhealthy_without_metrics_or_restart(self):
        def observe(pid, _):
            value = self.observation(pid, ready=False)
            value.update(metrics=None, health_status='unhealthy')
            return value
        rc, state = self.run_supervisor(observe, isolated=True)
        self.assertEqual((rc, state['attempts'], state['restarts']), (1, 1, 0))
        self.assertEqual(state['error'], 'Backend unhealthy')
        self.assertTrue(state['cleanup_confirmed'])

    def test_initial_control_identity_write_failure_prevents_launch(self):
        original = pkg.save
        def save(path, value):
            if Path(path).name == 'status.json':
                raise OSError(errno.EROFS, 'read only')
            return original(path, value)
        with mock.patch.object(sup.subprocess, 'Popen') as launch:
            with self.assertRaises(OSError):
                self.run_supervisor(lambda pid, _: self.observation(pid), isolated=True,
                    patch=mock.patch.object(pkg, 'save', side_effect=save))
            launch.assert_not_called()

    def test_running_snapshot_and_event_write_failures_keep_pid(self):
        clock = [100.0]
        ledger = auxiliary.Observations(clock=lambda: clock[0])
        original_save, original_write = pkg.save, sup.RotatingLog.write
        seen = []
        def save(path, value):
            if Path(path).name == 'status.json' and value['state'] == 'READY' and len(seen) < 4:
                raise OSError(errno.ENOSPC, 'full')
            return original_save(path, value)
        def write(logger, data):
            if logger.path.name == 'events.log':
                raise OSError(errno.EACCES, 'denied')
            return original_write(logger, data)
        def observe(pid, _):
            seen.append(pid)
            if len(seen) == 4:
                clock[0] += 60
            if len(seen) == 5:
                pkg.save(self.root/'runtime/stop.request.json', {})
            return self.observation(pid)
        with mock.patch.object(auxiliary, 'Observations', return_value=ledger), \
             mock.patch.object(sup.RotatingLog, 'write', new=write):
            rc, state = self.run_supervisor(observe, isolated=True,
                patch=mock.patch.object(pkg, 'save', side_effect=save))
        self.assertEqual((rc, state['attempts'], state['restarts']), (0, 1, 0))
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(state['had_auxiliary_gaps'])
        self.assertEqual(state['auxiliary_io']['status']['state'], 'AVAILABLE')
        self.assertEqual(state['auxiliary_io']['events']['state'], 'UNAVAILABLE')

    def test_stale_snapshot_is_not_current_ready_or_known_gpu_pids(self):
        pkg.save(self.root/'controller.json', dict(
            owner=sup.process_identity(os.getpid()), auxiliary_isolation=True))
        pkg.save(self.root/'status.json', dict(state='READY', updated_unix=time.time()-60))
        (self.root/'lease').mkdir()
        pkg.save(self.root/'lease/compute-monitor.json', dict(
            snapshot_unix=time.time()-60, visibility='AVAILABLE', pids=[42]))
        value = service.status(self.root)
        self.assertEqual(value['state'], 'OBSERVATION_UNAVAILABLE')
        self.assertTrue(value['gpu_monitor']['snapshot_stale'])
        self.assertIsNone(value['gpu_monitor']['pids'])

    def test_final_supervisor_event_failure_is_in_stopped_snapshot(self):
        original = sup.RotatingLog.write
        def write(logger, data):
            if logger.path.name == 'events.log' and data.endswith(b' STOPPED\n'):
                raise OSError(errno.ENOSPC, 'final supervisor event full')
            return original(logger, data)
        def observe(pid, _):
            pkg.save(self.root/'runtime/stop.request.json', {})
            return self.observation(pid)
        rc, state = self.run_supervisor(observe, isolated=True,
            patch=mock.patch.object(sup.RotatingLog, 'write', new=write))
        self.assertEqual((rc, state['state']), (0, 'STOPPED'))
        self.assertTrue(state['had_auxiliary_gaps'])
        self.assertEqual(state['auxiliary_io']['events']['failed_operations'], 1)

    def test_final_snapshot_failure_marks_terminal_gap(self):
        self.terminal_gap_fixture('snapshot')

    def test_final_event_failure_marks_terminal_gap(self):
        self.terminal_gap_fixture('event')

    def terminal_gap_fixture(self, target):
        out = self.root/'lease'
        monitor = telemetry.Monitor(out, query=lambda **_: dict(status='OK', pids=[]))
        original_save, original_write = pkg.save, auxiliary.BoundedLog.write
        def save(path, value):
            if target == 'snapshot' and Path(path).name == 'compute-monitor.json' and value['closed']:
                raise OSError(errno.ENOSPC, 'final snapshot failed')
            return original_save(path, value)
        def write(writer, text):
            if target == 'event' and any(name in text for name in
                    ('"event": "lease_guard_finished"', '"event": "lease_released"')):
                raise OSError(errno.ENOSPC, 'final event failed')
            return original_write(writer, text)
        with mock.patch.object(guard, 'gpu_command', return_value=False), \
             mock.patch.object(pkg, 'save', side_effect=save), \
             mock.patch.object(auxiliary.BoundedLog, 'write', new=write):
            rc = guard.run_guard([sys.executable, '-B', '-c', 'pass'], out,
                self.root/'lease.lock', interval=.01, grace=.2,
                inherit_lease=True, managed_monitor=monitor)
        value = pkg.read(out/'exit.json')
        self.assertEqual(rc, 0)
        self.assertTrue(monitor.observations.had_gaps)
        self.assertEqual(value['status'], 'COMPLETED_WITH_MONITOR_GAPS')
        channel = 'monitor_snapshot' if target == 'snapshot' else 'guard_events'
        self.assertEqual(value['compute_monitor']['auxiliary_io'][channel]['failed_operations'], 1)

    def test_combined_cpu_business_progress_under_same_lease_and_pid(self):
        out, lock = self.root/'lease', self.root/'lease.lock'
        calls = []
        def query(**_):
            calls.append(1)
            with lock.open('r+') as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if len(calls) > 1:
                raise ValueError('collector bug')
            return dict(status='OK', pids=[])
        monitor = telemetry.Monitor(out, query=query)
        script = (f'import sys;from pathlib import Path;sys.path.insert(0,{str(ROOT)!r});'
                  'from tools.serve_service.test_auxiliary import cpu_supervisor;'
                  f'raise SystemExit(cpu_supervisor(Path({str(self.root)!r})))')
        original = pkg.save
        def save(path, value):
            if Path(path).name == 'compute-monitor.json':
                raise OSError(errno.ENOSPC, 'monitor snapshot full')
            return original(path, value)
        with mock.patch.object(guard, 'gpu_command', return_value=False), \
             mock.patch.object(auxiliary.BoundedLog, 'write', side_effect=OSError(errno.EROFS, 'log readonly')), \
             mock.patch.object(pkg, 'save', side_effect=save):
            rc = guard.run_guard([sys.executable, '-B', '-c', script], out, lock,
                interval=.01, grace=.3, inherit_lease=True, managed_monitor=monitor)
        result = pkg.read(out/'exit.json')
        state = pkg.read(self.root/'supervisor-result.json')
        self.assertEqual((rc, result['status']), (0, 'COMPLETED_WITH_MONITOR_GAPS'))
        self.assertEqual((state['attempts'], state['restarts']), (1, 0))
        self.assertTrue(state['cleanup_confirmed'])
        self.assertEqual((self.root/'progress').read_text(), '128')
        self.assertEqual((self.root/'start.pid').read_text(), (self.root/'done.pid').read_text())
        self.assertFalse((self.root/'unexpected-metrics').exists())
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(state['auxiliary_io']['backend_log']['dropped_bytes'], 2*1024**2)
        self.assertEqual(result['compute_monitor']['query_category'], 'QUERY_ERROR')
        self.assertFalse(sup.processes.group_members(int((self.root/'start.pid').read_text())))
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main(verbosity=2)
