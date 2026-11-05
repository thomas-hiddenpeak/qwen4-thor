"""CPU-only monitor finalization regressions; no model or GPU probe."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import monitored_stage as monitored

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'monitored_stage_legacy', ROOT / 'tools/mtp_suffix/run_stage.py')
legacy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(legacy)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
                                     ROOT / '.q4t-work/monitor-contracts'))
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.output = self.base / 'run'
        self.output.mkdir()
        self.plan = dict(output=str(self.output))
        self.state = dict(status='MEASURED_REQUIRES_REVIEW', monitor_failures=[])
        self.summary = dict(sampler_status='STOPPED', sampler_failure=None,
            failures_at_exit=[], unflushed_attempts=0, sample_attempts=10,
            flushed_samples=10, exit_pending=dict(active=0),
            last_flushed_heartbeat=12, final_successful_cycle_duration_seconds=1)
        self.post = dict(status='MEASURED_REQUIRES_REVIEW', monitor_failures=[],
            cleanup_errors=[], final_monitor_errors=[],
            final_monitor_barrier=dict(boundary_monotonic=10,
                sample_started_monotonic=11, flushed_monotonic=12))

    def freeze(self):
        for name, value in [('state.json', self.state),
                            ('telemetry-summary.json', self.summary)]:
            legacy.save(self.output / name, value)
        self.post.update(state_sha256=legacy.sha(self.output / 'state.json'),
            final_summary_sha256=legacy.sha(self.output / 'telemetry-summary.json'))
        legacy.save(self.base / 'postflight.json', self.post)

    def reject(self):
        with self.assertRaises(monitored.MonitorIncomplete):
            monitored.require_complete_monitor(self.plan)

    def test_complete_monitor_is_not_performance_acceptance(self):
        self.freeze()
        result = monitored.require_complete_monitor(self.plan)
        self.assertEqual(result['status'], 'MEASURED_REQUIRES_REVIEW')

    def test_missing_or_corrupt_receipt_preserves_decision(self):
        decision = self.output / 'decision.json'
        decision.write_bytes(b'{"historical": "unchanged"}\n')
        original = decision.read_bytes()
        for name in ('state.json', 'telemetry-summary.json', '../postflight.json'):
            for broken in (None, b'not json', b'[]'):
                with self.subTest(name=name, broken=broken):
                    self.freeze()
                    path = self.output / name
                    if broken is None:
                        path.unlink()
                    else:
                        path.write_bytes(broken)
                    self.reject()
                    self.assertEqual(decision.read_bytes(), original)

    def test_each_late_summary_failure_is_rejected(self):
        good = copy.deepcopy(self.summary)
        changes = dict(sampler_status='ENV_ABORT', sampler_failure='late failure',
            failures_at_exit=['pending cleanup failed'], unflushed_attempts=1,
            sample_attempts=11, flushed_samples=0, exit_pending=dict(active=1))
        for key, value in changes.items():
            with self.subTest(key=key):
                self.summary = dict(good, **{key: value})
                self.freeze()
                self.reject()

    def test_failed_state_or_postflight_cannot_be_hidden_by_good_summary(self):
        for target, changes in ((self.state, dict(status='STOPPED',
                monitor_failures=['join failed'], cleanup_errors=['child alive'])),
                (self.post, dict(status='ENV_ABORT', monitor_failures=['late'],
                cleanup_errors=['timeout'], final_monitor_errors=['missing']))):
            for key, value in changes.items():
                original = copy.deepcopy(target)
                with self.subTest(target='state' if target is self.state else 'post',
                                  key=key):
                    target[key] = value
                    self.freeze()
                    self.reject()
                target.clear()
                target.update(original)

    def test_stale_missing_or_future_barrier_is_rejected(self):
        for barrier in ({}, dict(boundary_monotonic=12,
                sample_started_monotonic=11, flushed_monotonic=12),
                dict(boundary_monotonic=10, sample_started_monotonic=11,
                     flushed_monotonic=13)):
            with self.subTest(barrier=barrier):
                self.post['final_monitor_barrier'] = barrier
                self.freeze()
                self.reject()

    def test_nonfinite_or_negative_sample_duration_is_rejected(self):
        for field in ('last_flushed_heartbeat',
                      'final_successful_cycle_duration_seconds'):
            for value in (float('nan'), float('inf'), -1, True, '12'):
                with self.subTest(field=field, value=value):
                    original = self.summary[field]
                    self.summary[field] = value
                    self.freeze()
                    self.reject()
                    self.summary[field] = original

    def test_wrong_counter_types_are_rejected(self):
        for key, value in (('unflushed_attempts', False),
                           ('sample_attempts', 10.0),
                           ('exit_pending', dict(active=False))):
            with self.subTest(key=key):
                original = self.summary[key]
                self.summary[key] = value
                self.freeze()
                self.reject()
                self.summary[key] = original

    def test_changed_raw_receipts_are_rejected(self):
        for name in ('state.json', 'telemetry-summary.json'):
            with self.subTest(name=name):
                self.freeze()
                path = self.output / name
                path.write_bytes(path.read_bytes() + b'\n')
                self.reject()


class ControlledTelemetry:
    """Use a real sampler thread but inject sources and stop-time failures."""
    def __init__(self, mode='healthy'):
        self.mode = mode
        self.started = threading.Event()
        self.sources = None

    def configure_sources(self, sources):
        self.sources = sources

    def sample(self, output, stop, failures, heartbeat):
        count, last, duration = 0, 0, 0
        with output.open('x') as stream:
            while not stop.is_set():
                begin = time.monotonic()
                stream.write('{}\n')
                stream.flush()
                last = time.monotonic()
                duration = last - begin
                count += 1
                if self.mode != 'no_fresh_sample' or count == 1:
                    heartbeat.update(last_sample=last,
                                     last_cycle_duration_seconds=duration)
                self.started.set()
                stop.wait(.002)
        if self.mode == 'late_failure':
            failures.append('ENV_ABORT injected at sampler shutdown')
        if self.mode == 'missing_summary':
            return
        path = output.with_name('telemetry-summary.json')
        if self.mode == 'corrupt_summary':
            path.write_text('incomplete')
            return
        legacy.save(path, dict(sampler_status='ENV_ABORT' if failures else 'STOPPED',
            sampler_failure=None, failures_at_exit=list(failures),
            unflushed_attempts=0, sample_attempts=count, flushed_samples=count,
            exit_pending=dict(active=1 if self.mode == 'pending' else 0),
            last_flushed_heartbeat=heartbeat.get('last_sample'),
            final_successful_cycle_duration_seconds=duration))


class StageLifecycleTest(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.q4t-work/monitor-contracts'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.output = self.base / 'run'
        self.plan = dict(output=str(self.output), files={}, model_file_metadata={},
            stage_timeout_seconds=2, resource_sample_max_age_seconds=.4,
            http_requests=0, steps=[], telemetry_sources={})
        self.plan_path = self.base / 'plan.json'
        legacy.save(self.plan_path, self.plan)
        self.guard = mock.patch.object(legacy, 'require_guard').start()
        self.addCleanup(mock.patch.stopall)
        self.old_sample, self.old_audit = legacy.sample_memory, legacy.audit_server

    def run_controlled(self, mode='healthy'):
        telemetry = ControlledTelemetry(mode)
        # Ensure the first sample predates the final boundary, making the
        # second complete cycle necessary rather than depending on scheduling.
        original_verify = legacy.verify_files

        def verify(plan):
            original_verify(plan)
            if not telemetry.started.wait(timeout=1):
                raise RuntimeError('controlled sampler did not start')

        with mock.patch.object(legacy, 'verify_files', side_effect=verify):
            return monitored.run_stage(legacy, telemetry, self.plan, self.plan_path,
                                       audit_server=lambda *_: None)

    def receipts(self):
        state = json.loads((self.output / 'state.json').read_text())
        post = json.loads((self.base / 'postflight.json').read_text())
        seal = json.loads((self.output / 'files.json').read_text())
        for relative, expected in seal.items():
            self.assertEqual(legacy.sha(self.output / relative), expected)
        self.assertIs(legacy.sample_memory, self.old_sample)
        self.assertIs(legacy.audit_server, self.old_audit)
        return state, post

    def test_real_driver_waits_joins_and_preserves_seal(self):
        self.run_controlled()
        state, post = self.receipts()
        self.assertEqual(state['status'], 'MEASURED_REQUIRES_REVIEW')
        self.assertEqual(post['status'], 'MEASURED_REQUIRES_REVIEW')
        self.assertGreaterEqual(post['final_monitor_barrier']['sample_started_monotonic'],
                                post['final_monitor_barrier']['boundary_monotonic'])
        monitored.require_complete_monitor(self.plan)

    def test_direct_legacy_caller_rejects_failure_appended_during_join(self):
        sampler = ControlledTelemetry('late_failure')
        with mock.patch.object(legacy, 'sample_memory', side_effect=sampler.sample):
            with self.assertRaises(legacy.EnvironmentAbort):
                legacy.run_stage(self.plan, self.plan_path)
        state = json.loads((self.output / 'state.json').read_text())
        self.assertEqual(state['status'], 'ENV_ABORT')
        self.assertEqual(len(state['monitor_failures']), 1)

    def test_wrapper_rejects_failure_appended_during_join(self):
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('late_failure')
        state, post = self.receipts()
        self.assertEqual(state['status'], 'ENV_ABORT')
        self.assertEqual(post['status'], 'ENV_ABORT')

    def test_unresolved_identity_even_without_failure_list_aborts(self):
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('pending')
        state, post = self.receipts()
        self.assertEqual(state['status'], 'MEASURED_REQUIRES_REVIEW')
        self.assertEqual(post['status'], 'ENV_ABORT')
        with self.assertRaises(monitored.MonitorIncomplete):
            monitored.require_complete_monitor(self.plan)

    def test_missing_summary_aborts(self):
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('missing_summary')
        _, post = self.receipts()
        self.assertEqual(post['status'], 'ENV_ABORT')

    def test_corrupt_summary_aborts(self):
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('corrupt_summary')
        _, post = self.receipts()
        self.assertEqual(post['status'], 'ENV_ABORT')

    def test_stale_heartbeat_cannot_finish_stage(self):
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('no_fresh_sample')
        state, post = self.receipts()
        self.assertEqual(state['status'], 'ENV_ABORT')
        self.assertEqual(post['final_monitor_barrier'], {})

    def test_stage_deadline_bounds_final_sample_wait(self):
        self.plan['stage_timeout_seconds'] = .05
        self.plan['resource_sample_max_age_seconds'] = 10
        legacy.save(self.plan_path, self.plan)
        started = time.monotonic()
        with self.assertRaises(legacy.EnvironmentAbort):
            self.run_controlled('no_fresh_sample')
        self.assertLess(time.monotonic() - started, 1)
        state, _ = self.receipts()
        self.assertEqual(state['status'], 'ENV_ABORT')

    def test_existing_evidence_is_never_overwritten(self):
        receipt = self.base / 'postflight.json'
        receipt.write_bytes(b'original evidence\n')
        with self.assertRaises(FileExistsError):
            self.run_controlled()
        self.assertEqual(receipt.read_bytes(), b'original evidence\n')
        self.assertFalse(self.output.exists())

    def test_missing_stage_state_is_reported_as_environment_abort(self):
        with mock.patch.object(legacy, 'run_stage', side_effect=RuntimeError('no state')):
            with self.assertRaises(legacy.EnvironmentAbort):
                self.run_controlled()
        post = json.loads((self.base / 'postflight.json').read_text())
        self.assertEqual(post['status'], 'ENV_ABORT')
        self.assertIsNone(post['state_sha256'])
        self.assertIs(legacy.sample_memory, self.old_sample)
        self.assertIs(legacy.audit_server, self.old_audit)

    def test_postflight_write_failure_cannot_return_success(self):
        original_open = Path.open

        def deny_receipt(path, *args, **kwargs):
            if path == self.base / 'postflight.json':
                raise PermissionError('controlled receipt denial')
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'open', deny_receipt):
            with self.assertRaisesRegex(legacy.EnvironmentAbort, 'cannot save'):
                self.run_controlled()
        self.assertIs(legacy.sample_memory, self.old_sample)
        self.assertIs(legacy.audit_server, self.old_audit)
        with self.assertRaises(monitored.MonitorIncomplete):
            monitored.require_complete_monitor(self.plan)

    def test_body_failure_remains_stopped_and_callbacks_are_restored(self):
        calls = 0
        original_verify = legacy.verify_files

        def fail_after_collection(plan):
            nonlocal calls
            calls += 1
            original_verify(plan)
            if calls == 2:
                raise RuntimeError('injected body failure')

        with mock.patch.object(legacy, 'verify_files', side_effect=fail_after_collection):
            with self.assertRaisesRegex(RuntimeError, 'injected body failure'):
                self.run_controlled()
        state, post = self.receipts()
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(post['status'], 'STOPPED')


if __name__ == '__main__':
    unittest.main()
