"""Explicit monitor recovery using CPU fixtures and private test leases."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope')]
from tools.serve_service import package as pkg, service, telemetry, monitor_control as control
from tools.serve_service import test_service as old
from tools.serve_service.test_nvml_policy import failure

guard = service.guard


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        old.Case.setUp(self)
        self.now = 100.0
        self.stopping = False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(guard.nvml_process_query, 'query',
                          side_effect=AssertionError('real NVML forbidden')).start()

    def monitor(self, results):
        out = self.root/'lease'
        out.mkdir()
        self.query = mock.Mock(side_effect=results)
        return telemetry.Monitor(out, query=self.query, clock=lambda: self.now,
            wall_clock=lambda: self.now+1000, resume_path=self.root/control.REQUEST_NAME,
            stopping=lambda: self.stopping)

    def request(self, monitor, **changes):
        value = dict(version=1, request_id=secrets.token_hex(16),
            generation=monitor.state['generation'],
            suspension_epoch=monitor.state['suspension_epoch'])
        value.update(changes)
        pkg.save(self.root/control.REQUEST_NAME, value)
        monitor.poll_resume()
        return value

    def unavailable(self, monitor):
        with self.assertRaises(telemetry.Unavailable):
            monitor.compute()
        monitor.publish()

    def test_cooldown_requires_fresh_explicit_intent_and_preserves_gap(self):
        monitor = self.monitor([failure(18), dict(status='OK', pids=[])])
        self.unavailable(monitor)
        early = self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'COOLDOWN')
        self.now += 60
        monitor.poll_resume()  # An early request is not a scheduled retry.
        self.unavailable(monitor)
        self.assertEqual(self.query.call_count, 1)
        self.request(monitor)
        self.assertEqual(monitor.compute(), set())
        value = monitor.snapshot()
        self.assertEqual((value['resume_attempts'], value['recovered_gaps']), (1, 1))
        self.assertEqual(value['completed_gap_seconds'], 60)
        self.assertTrue(monitor.had_gaps)
        self.assertEqual(value['last_resume']['status'], 'RECOVERED')
        self.assertEqual(value['last_failure']['nvml_code'], 18)
        self.request(monitor, **early)
        self.assertEqual(monitor.state['resume_attempts'], 1)

    def test_resume_budget_is_not_refunded_after_success(self):
        monitor = self.monitor([failure(3), dict(status='OK', pids=[])]*3)
        for used in (1, 2):
            self.unavailable(monitor)
            self.now += 60
            self.request(monitor)
            monitor.compute()
            self.assertEqual(monitor.state['resume_attempts'], used)
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'RESUME_LIMIT')
        self.unavailable(monitor)
        self.assertEqual(self.query.call_count, 5)

    def test_manual_recovery_after_retry_exhaustion_is_only_one_probe(self):
        monitor = self.monitor([dict(status='TIMEOUT')]*7)
        self.unavailable(monitor)
        for delay in (5, 10, 20, 40, 60):
            self.now += delay
            self.unavailable(monitor)
        self.assertEqual(monitor.state['suspension_reason'], 'RETRY_LIMIT')
        self.now += 60
        self.request(monitor)
        self.unavailable(monitor)
        self.assertEqual(monitor.state['suspension_reason'], 'RESUME_FAILED')
        self.now += 1000
        self.unavailable(monitor)
        self.assertEqual(self.query.call_count, 7)

    def test_generation_epoch_and_duplicate_requests_cannot_rearm(self):
        monitor = self.monitor([failure(999), failure(999)])
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor, generation='0'*32)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'STALE_GENERATION')
        accepted = self.request(monitor)
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor, **accepted)
        self.assertEqual(monitor.state['resume_attempts'], 1)
        self.request(monitor, suspension_epoch=accepted['suspension_epoch'])
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'STALE_SUSPENSION')
        self.assertEqual(self.query.call_count, 2)

    def test_live_worker_must_be_reaped_before_any_new_query(self):
        monitor = self.monitor([dict(status='OK', pids=[])])
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'])
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        self.addCleanup(cleanup)
        monitor.track_worker(child)
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'WORKER_UNREAPED')
        self.assertEqual(self.query.call_count, 0)
        cleanup()
        self.request(monitor)
        monitor.compute()
        self.assertEqual(self.query.call_count, 1)
        self.assertTrue(monitor.snapshot()['query_worker_reaped'])

    def test_missing_worker_handle_is_not_cleanup_evidence(self):
        monitor = self.monitor([dict(status='CLEANUP_FAILED')])
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor)
        self.assertFalse(monitor.snapshot()['query_worker_reaped'])
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'WORKER_UNREAPED')
        self.assertEqual(self.query.call_count, 1)

    def test_stop_after_acceptance_cancels_query_without_refunding_budget(self):
        monitor = self.monitor([failure(3)])
        self.unavailable(monitor)
        self.now += 60
        self.request(monitor)
        self.stopping = True
        self.unavailable(monitor)
        self.assertEqual(monitor.state['last_resume']['status'], 'CANCELLED')
        self.assertEqual(monitor.state['resume_attempts'], 1)
        self.assertEqual(self.query.call_count, 1)
        self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'STOPPING')
        monitor.close()
        self.stopping = False
        self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'STOPPING')

    def test_fatal_device_failure_latches_and_cannot_resume(self):
        monitor = self.monitor([failure(15)])
        with self.assertRaisesRegex(RuntimeError, 'DEVICE_FAILURE'):
            monitor.compute()
        self.now += 60
        self.request(monitor)
        self.assertEqual(monitor.state['last_resume_request']['reason'], 'FATAL_FAULT')
        with self.assertRaisesRegex(RuntimeError, 'latched'):
            monitor.compute()
        self.assertEqual(self.query.call_count, 1)

    def test_invalid_control_message_never_triggers_query(self):
        monitor = self.monitor([failure(3)])
        self.unavailable(monitor)
        self.now += 60
        for value in ({}, dict(version=True), ['invalid']):
            pkg.save(self.root/control.REQUEST_NAME, value)
            monitor.poll_resume()
            self.assertEqual(monitor.state['last_resume_request']['reason'], 'INVALID_REQUEST')
        self.assertEqual(self.query.call_count, 1)

    def test_cli_binds_live_controller_and_new_package_without_model_actions(self):
        monitor = self.monitor([failure(18), dict(status='OK', pids=[])])
        self.unavailable(monitor)
        self.now += 60
        monitor.publish()
        pkg.save(self.root/'controller.json', dict(
            owner=service.supervisor.process_identity(os.getpid()),
            package=str(self.root), manifest_sha256='fixture'))
        manifest = dict(version=9, settings=pkg.settings(18999, 0, nvml_policy='degrade'))
        with mock.patch.object(pkg, 'verify', return_value=manifest), \
             mock.patch.object(control.time, 'monotonic', return_value=self.now), \
             mock.patch.object(service, 'run') as launch, \
             mock.patch.object(sys, 'argv', ['service.py', 'resume-monitor', '--run', str(self.root)]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(service.main(), 0)
            launch.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['state'], 'MONITOR_RESUME_REQUESTED')
            manifest['version'] = 10
            self.assertEqual(control.request(self.root)['state'], 'MONITOR_RESUME_REQUESTED')
            manifest['version'] = 11
            self.assertEqual(control.request(self.root)['state'], 'MONITOR_RESUME_REQUESTED')
            manifest['version'] = 7
            with self.assertRaisesRegex(RuntimeError, 'v8/v9/v10/v11 degrade'):
                control.request(self.root)
            manifest['version'] = 9
            pkg.save(self.root/'stop.request.json', {})
            with self.assertRaisesRegex(RuntimeError, 'STOPPING'):
                control.request(self.root)
        # The CLI only persisted intent; no actual query or model launch yet.
        self.assertEqual(self.query.call_count, 1)

    def test_controller_identity_mismatch_cannot_submit_intent(self):
        pkg.save(self.root/'controller.json', dict(
            owner=dict(pid=os.getpid(), start_ticks=-1)))
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            control.request(self.root)
        self.assertFalse((self.root/control.REQUEST_NAME).exists())

    def test_real_cpu_guard_recovers_monitor_under_same_lease_and_child(self):
        out, lock = self.root/'lease', self.root/'lease.lock'
        request_path = self.root/control.REQUEST_NAME
        calls = []
        def query(**_):
            calls.append(1)
            with lock.open('r+') as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return failure(18) if len(calls) == 2 else dict(status='OK', pids=[])
        monitor = telemetry.Monitor(out, query=query, resume_path=request_path)
        script = '''import json,os,pathlib,secrets,time
root=pathlib.Path(__import__('sys').argv[1])
(root/'start.pid').write_text(str(os.getpid()))
end=time.monotonic()+5
sent=False
while time.monotonic()<end:
    p=root/'lease/compute-monitor.json'
    if p.exists():
        v=json.loads(p.read_text())
        if v['suspended'] and not sent and time.monotonic()>=v['resume_not_before_monotonic']:
            r=dict(version=1,request_id=secrets.token_hex(16),generation=v['generation'],suspension_epoch=v['suspension_epoch'])
            t=root/'request.new';t.write_text(json.dumps(r));t.replace(root/'monitor.resume.request.json');sent=True
        if v['last_resume'] and v['last_resume']['status']=='RECOVERED':
            (root/'done.pid').write_text(str(os.getpid()));break
    time.sleep(.005)
else: raise SystemExit(2)
'''
        with mock.patch.object(control, 'COOLDOWN_SECONDS', .01), \
             mock.patch.object(guard, 'gpu_command', return_value=False):
            rc = guard.run_guard([sys.executable, '-B', '-c', script, str(self.root)],
                out, lock, interval=.01, grace=.2, inherit_lease=True, managed_monitor=monitor)
        result = pkg.read(out/'exit.json')
        self.assertEqual(rc, 0)
        self.assertEqual(result['status'], 'COMPLETED_WITH_MONITOR_GAPS')
        self.assertEqual(result['monitor_policy'], control.PROFILE)
        self.assertEqual(result['compute_monitor']['resume_attempts'], 1)
        self.assertEqual((self.root/'start.pid').read_text(), (self.root/'done.pid').read_text())
        self.assertTrue(service.normal_stop(True, True, result))
        self.assertGreaterEqual(len(calls), 3)
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_cpu_guard_stops_device_fault_without_replacement(self):
        out = self.root/'lease'
        query = mock.Mock(side_effect=[dict(status='OK', pids=[]), failure(16)])
        monitor = telemetry.Monitor(out, query=query)
        with mock.patch.object(guard, 'gpu_command', return_value=False), \
             mock.patch.object(guard.subprocess, 'Popen', wraps=subprocess.Popen) as launch:
            rc = guard.run_guard([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                out, self.root/'lease.lock', interval=.01, grace=.2,
                inherit_lease=True, managed_monitor=monitor)
        result = pkg.read(out/'exit.json')
        self.assertEqual((rc, launch.call_count), (75, 1))
        self.assertEqual(result['status'], 'ENV_ABORT')
        self.assertIn('DEVICE_FAILURE', result['reason'])
        self.assertNotIn('cleanup_error', result)
        self.assertEqual(result['compute_monitor']['resume_attempts'], 0)


if __name__ == '__main__':
    unittest.main()
