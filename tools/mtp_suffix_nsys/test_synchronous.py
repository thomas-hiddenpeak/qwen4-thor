"""CPU-only contracts for synchronous report binding; no collector execution."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'synchronous_under_test', Path(__file__).with_name('synchronous.py'))
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
manual = sync.manual


class Child:
    def __init__(self, pid):
        self.pid, self.returncode = pid, 0

    def poll(self):
        return self.returncode


class SynchronousContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='sync contract ')
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.clock, self.subreaper, self.counter = 10., 0, 0
        self.owner = dict(pid=os.getpid(), start_ticks=100, ppid=1,
                          pgid=os.getpgrp(), state='S')
        self.identities = {self.owner['pid']: self.owner}
        self.calls = []
        def popen(command, **kwargs):
            self.calls.append((command, kwargs))
            return Child(62000 + len(self.calls))
        self.mock(manual, 'proc_identity', side_effect=lambda pid: self.identities[pid].copy())
        self.mock(manual, '_get_subreaper', side_effect=lambda: self.subreaper)
        self.mock(manual, '_set_subreaper', side_effect=lambda v: setattr(self, 'subreaper', v))
        self.mock(manual.subprocess, 'Popen', side_effect=popen)
        self.mock(manual.secrets, 'token_hex', side_effect=lambda n: 'b' * (2 * n))
        self.mock(sync.time, 'monotonic', side_effect=lambda: self.clock)
        self.mock(sync.time, 'sleep', side_effect=lambda v: setattr(self, 'clock', self.clock + v))

    def mock(self, obj, name, **kwargs):
        handle = patch.object(obj, name, **kwargs)
        self.addCleanup(handle.stop)
        return handle.start()

    def session(self):
        output = self.base / str(self.counter)
        self.counter += 1
        output.mkdir()
        return sync.Session('/mock/nsys', output, sync.make_name('sync'),
            cwd=self.base, env={}, deadline=100)

    def launched(self, source='fixture-ready'):
        session = self.session()
        session.start()
        log = (session.output / 'server.log').open('x+')
        self.addCleanup(log.close)
        session.launch(['/mock/target'], log)
        pid = 50000 + self.counter
        target = dict(pid=pid, start_ticks=200, ppid=self.owner['pid'],
                      pgid=self.owner['pgid'], state='S')
        self.identities[pid] = target
        session.prove_descendant(pid)
        raw = (dict(pid=pid, profiler_start_error=0, profiler_stop_error=0,
                    final_sync_error=0, passed=True, completed_steps=32)
               if source == 'fixture-ready' else dict(pid=str(pid), event='end', error='0',
                                                     input_tokens='8170'))
        receipt = dict(pid=pid, start_ticks=200, profiler_stop_error=0, source=source, raw=raw)
        if source == 'model-capture-end':
            receipt['expected_input_tokens'] = 8170
        return session, receipt

    def generated(self, session, name='trace37_001.nsys-rep'):
        report = session.output / name
        report.write_bytes(b'CPU mock report only')
        with (session.output / 'server.log').open('a') as stream:
            stream.write('Generated:\n\t' + str(report) + '\n')
        return report

    def read(self, session):
        return json.loads((session.output / 'synchronous-report-ready.json').read_text())

    def test_exact_sync_lifecycle_no_callback_or_manual_stop(self):
        session, receipt = self.launched()
        report = self.generated(session)
        result = session.await_report(receipt)
        session.shutdown()
        self.identities[receipt['pid']]['state'] = 'Z'
        self.assertEqual(sync.bound_report(session.output), report)
        session.close()
        commands = [row[0] for row in self.calls]
        self.assertEqual([row[1] for row in commands], ['start', 'launch', 'shutdown'])
        self.assertIn('--capture-range-end=repeat:1:sync', commands[0])
        self.assertIn('--flush-on-cudaprofilerstop=true', commands[0])
        self.assertFalse(any('after-report-ready' in value for row in commands for value in row))
        self.assertTrue(result['report_ready_succeeded'])
        self.assertEqual(session.report_path, report)
        self.assertFalse(session.stop_succeeded)
        self.assertEqual(self.subreaper, 0)
        with self.assertRaises(RuntimeError):
            session.stop()

    def test_actual_model_receipt_and_launcher_exit_are_independent(self):
        session, receipt = self.launched('model-capture-end')
        self.generated(session)
        checks = []
        session.await_report(receipt, check=lambda: checks.append(True))
        self.assertGreaterEqual(len(checks), 2)
        self.assertEqual(session.launch_process.poll(), 0)
        self.assertNotIn('target_returncode', self.read(session))

    def test_normalized_and_raw_receipts_must_match(self):
        changes = [lambda r: r.update(pid=True), lambda r: r.update(start_ticks=0),
                   lambda r: r.update(profiler_stop_error=1), lambda r: r.update(source='other'),
                   lambda r: r['raw'].update(pid=r['pid'] + 1),
                   lambda r: r['raw'].update(profiler_stop_error=1),
                   lambda r: r['raw'].update(passed=False)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                session, receipt = self.launched()
                self.generated(session)
                change(receipt)
                with self.assertRaises(ValueError):
                    session.await_report(receipt)
                self.assertIsNotNone(self.read(session)['failure'])
        for key, value in (('event', 'begin'), ('input_tokens', '204781')):
            with self.subTest(model_field=key):
                session, receipt = self.launched('model-capture-end')
                self.generated(session)
                receipt['raw'][key] = value
                with self.assertRaises(ValueError):
                    session.await_report(receipt)

    def test_unbound_recycled_and_dead_targets_are_rejected(self):
        for kind in ('unbound', 'recycled', 'dead'):
            with self.subTest(kind=kind):
                session, receipt = self.launched()
                self.generated(session)
                if kind == 'unbound':
                    session._known_owned.clear()
                elif kind == 'recycled':
                    self.identities[receipt['pid']]['start_ticks'] += 1
                else:
                    self.identities[receipt['pid']]['state'] = 'Z'
                with self.assertRaises(RuntimeError):
                    session.await_report(receipt)
                self.assertFalse(session.report_ready_succeeded)

    def test_generated_missing_partial_duplicate_and_multiple(self):
        for kind in ('missing', 'partial', 'duplicate', 'multiple'):
            with self.subTest(kind=kind):
                session, receipt = self.launched()
                report = session.output / 'trace.nsys-rep'
                report.write_bytes(b'mock')
                text = ('nothing\n' if kind == 'missing' else
                        'Generated:\n\t' + str(report) + ('' if kind == 'partial' else '\n'))
                if kind == 'duplicate':
                    text += 'Generated:\n\t' + str(report) + '\n'
                if kind == 'multiple':
                    text += '\t' + str(session.output / 'second.nsys-rep') + '\n'
                (session.output / 'server.log').write_text(text)
                with self.assertRaises((TimeoutError, ValueError)):
                    session.await_report(receipt, timeout=.1)
                self.assertIsNotNone(self.read(session)['failure'])

    def test_foreign_relative_and_escaping_report_paths_rejected(self):
        for text in ('relative.nsys-rep', str(self.base / 'foreign.nsys-rep'),
                     str(self.base / '..' / 'escape.nsys-rep')):
            with self.subTest(path=text):
                session, receipt = self.launched()
                (session.output / 'server.log').write_text('Generated:\n\t' + text + '\n')
                with self.assertRaises(ValueError):
                    session.await_report(receipt)

    def test_report_zero_symlink_extra_and_missing_rejected(self):
        for kind in ('zero', 'symlink', 'extra', 'missing', 'fifo'):
            with self.subTest(kind=kind):
                session, receipt = self.launched()
                report = self.generated(session)
                if kind == 'zero':
                    report.write_bytes(b'')
                elif kind == 'symlink':
                    report.unlink()
                    report.symlink_to(session.output / 'server.log')
                elif kind == 'extra':
                    (session.output / 'other.nsys-rep').write_bytes(b'extra')
                elif kind == 'missing':
                    report.unlink()
                else:
                    report.unlink()
                    os.mkfifo(report)
                with self.assertRaises((ValueError, OSError)):
                    session.await_report(receipt)

    def test_launch_log_ownership_identity_and_bound_revalidation(self):
        session = self.session()
        session.start()
        with (self.base / 'foreign.log').open('x+') as log:
            with self.assertRaises(ValueError):
                session.launch(['/mock/target'], log)
        session, receipt = self.launched()
        self.generated(session)
        log = session.output / 'server.log'
        text = log.read_text()
        log.rename(session.output / 'old.log')
        log.write_text(text)
        with self.assertRaises(ValueError):
            session.await_report(receipt)

    def test_offline_binding_rejects_report_or_receipt_tamper(self):
        for kind in ('report', 'failed', 'generated'):
            with self.subTest(kind=kind):
                session, receipt = self.launched()
                report = self.generated(session)
                session.await_report(receipt)
                if kind == 'report':
                    report.write_bytes(b'modified report bytes')
                elif kind == 'failed':
                    value = self.read(session)
                    value['report_ready_succeeded'] = False
                    (session.output / 'synchronous-report-ready.json').write_text(json.dumps(value))
                else:
                    with (session.output / 'server.log').open('a') as log:
                        log.write('Generated:\n\t' + str(report) + '\n')
                with self.assertRaises(ValueError):
                    sync.bound_report(session.output)

    def test_bounds_once_and_failure_cleanup_never_repair_success(self):
        session, receipt = self.launched()
        for timeout in (0, -1, 46, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                session.await_report(receipt, timeout=timeout)
        session.deadline = self.clock + .02
        with self.assertRaises(TimeoutError):
            session.await_report(receipt)
        self.assertLessEqual(self.clock, 10.020001)
        with self.assertRaises(RuntimeError):
            session.await_report(receipt)
        # Cleanup still consumes the same stage deadline; allow time only in
        # this isolated mock to exercise the independent flag contract.
        session.deadline = 100
        session.shutdown()
        session.close()
        self.assertTrue(session.shutdown_succeeded)
        self.assertFalse(session.report_ready_succeeded)
        self.assertIsNone(session.report_path)

    def test_monitor_and_live_check_failure_after_binding_is_preserved(self):
        for kind in ('monitor', 'target', 'late'):
            with self.subTest(kind=kind):
                session, receipt = self.launched()
                self.generated(session)
                def fail():
                    raise RuntimeError(kind + ' failure')
                if kind == 'monitor':
                    session.check = fail
                    with self.assertRaises(RuntimeError):
                        session.await_report(receipt)
                elif kind == 'target':
                    with self.assertRaises(RuntimeError):
                        session.await_report(receipt, check=fail)
                else:
                    original = sync._report
                    def late(*args):
                        result = original(*args)
                        self.clock += 1
                        return result
                    with patch.object(sync, '_report', side_effect=late):
                        with self.assertRaises(TimeoutError):
                            session.await_report(receipt, timeout=.1)
                self.assertFalse(session.report_ready_succeeded)
                self.assertIsNotNone(self.read(session)['failure'])

    def test_read_permission_and_size_errors_remain_failures(self):
        session, receipt = self.launched()
        self.generated(session)
        with patch.object(sync, '_generated', side_effect=PermissionError('mock denied')):
            with self.assertRaises(PermissionError):
                session.await_report(receipt)
        self.assertIn('PermissionError', self.read(session)['failure'])
        session, receipt = self.launched()
        self.generated(session)
        with patch.object(sync, 'MAX_LOG_BYTES', 1):
            with self.assertRaises(ValueError):
                session.await_report(receipt)

    def test_preexisting_reports_and_binding_refused(self):
        for name in ('stale.nsys-rep', 'synchronous-report-ready.json', 'dangling-binding'):
            with self.subTest(name=name):
                directory = self.base / ('preexisting-' + name)
                directory.mkdir()
                if name == 'dangling-binding':
                    (directory / 'synchronous-report-ready.json').symlink_to(directory / 'missing')
                else:
                    (directory / name).write_bytes(b'old')
                with self.assertRaises(ValueError):
                    sync.Session('/mock/nsys', directory, sync.make_name('stale'),
                                 cwd=self.base, env={}, deadline=100)


if __name__ == '__main__':
    unittest.main(verbosity=2)
