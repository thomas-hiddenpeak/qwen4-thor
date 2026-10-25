"""CPU-only automatic lifecycle contracts; never starts Nsight or a target."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'automatic_under_test', Path(__file__).with_name('automatic.py'))
automatic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(automatic)
manual = automatic.manual
callback = automatic.callback


class Child:
    def __init__(self, pid, code=0):
        self.pid, self.returncode = pid, code

    def poll(self):
        return self.returncode


class AutomaticContracts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='auto contract ')
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.clock = 10.0
        self.subreaper = 0
        self.owner = dict(pid=os.getpid(), start_ticks=100, ppid=1,
                          pgid=os.getpgrp(), state='S')
        self.calls = []

        def popen(command, **kwargs):
            self.calls.append((command, kwargs))
            return Child(61000 + len(self.calls))

        self.mock(manual, 'proc_identity', return_value=self.owner.copy())
        self.mock(manual, '_get_subreaper', side_effect=lambda: self.subreaper)
        self.mock(manual, '_set_subreaper', side_effect=lambda v: setattr(self, 'subreaper', v))
        self.mock(manual.subprocess, 'Popen', side_effect=popen)
        self.mock(automatic.secrets, 'token_hex', side_effect=lambda n: 'a' * (2 * n))
        self.mock(automatic.time, 'monotonic', side_effect=lambda: self.clock)
        self.mock(automatic.time, 'monotonic_ns', side_effect=lambda: int(self.clock * 1e9))
        self.mock(automatic.time, 'sleep', side_effect=self.advance)

    def mock(self, obj, name, **kwargs):
        handle = patch.object(obj, name, **kwargs)
        self.addCleanup(handle.stop)
        return handle.start()

    def advance(self, seconds):
        self.clock += seconds

    def session(self, **kwargs):
        directory = self.output / str(len(list(self.output.iterdir())))
        directory.mkdir()
        return automatic.Session('/mock/nsys', directory,
            automatic.make_name('automatic'), cwd=self.output, env={'PATH': '/bin'},
            deadline=100, **kwargs)

    def launched(self, **kwargs):
        session = self.session(**kwargs)
        session.start()
        session.launch(['/mock/target'], io.StringIO())
        return session

    def emit(self, session, environment=None):
        report = session.output / 'trace.nsys-rep'
        if not report.exists():
            report.write_bytes(b'CPU mock report; not GPU evidence')
        env = dict(NSYS_SESSION_NAME=session.name, NSYS_CALLBACK_NAME='opaque-documented-variable',
                   NSYS_REPORT_PATH=str(report))
        if environment:
            env.update(environment)
        return callback.callback(session.name, session._callback_nonce,
            session._callback_path, report, env)

    def rewrite(self, session, value):
        session._callback_path.write_text(json.dumps(value))

    def failure_receipt(self, session):
        return json.loads((session.output / 'automatic-report-ready.json').read_text())

    def test_auto_exact_commands_and_independent_success_flags(self):
        session = self.launched()
        value = self.emit(session)
        checks = []
        result = session.await_report(check=lambda: checks.append('target-live'))
        session.shutdown()
        session.close()
        commands = [row[0] for row in self.calls]
        self.assertEqual([row[1] for row in commands], ['start', 'launch', 'shutdown'])
        self.assertIn('--capture-range-end=stop', commands[0])
        self.assertNotIn('--capture-range-end=none', commands[0])
        self.assertIn('--flush-on-cudaprofilerstop=true', commands[0])
        self.assertIn('--kill=none', commands[-1])
        self.assertIn('--capture-range-end=none', manual.START_OPTIONS)
        self.assertEqual(result['callback'], value)
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue(session.report_ready_succeeded)
        self.assertFalse(session.stop_succeeded)
        self.assertEqual(self.subreaper, 0)

    def test_callback_command_quotes_paths_and_is_single_fixed_helper(self):
        session = self.launched()
        command = self.calls[0][0]
        option = next(x for x in command if x.startswith('--after-report-ready='))
        args = shlex.split(option.split('=', 1)[1])
        self.assertTrue(Path(args[0]).is_absolute())
        self.assertEqual(args[1], '-B')
        self.assertEqual(args[2], str(Path(automatic.__file__).with_name('report_ready.py').resolve()))
        self.assertEqual(args[args.index('--session') + 1], session.name)
        self.assertEqual(args[args.index('--nonce') + 1], session._callback_nonce)
        self.assertEqual(args[args.index('--output') + 1], str(session._callback_path))
        self.assertEqual(args[args.index('--report') + 1], str(session._expected_report))

    def test_callback_publish_is_exclusive_and_does_not_overwrite(self):
        session = self.launched()
        self.emit(session)
        original = session._callback_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.emit(session)
        self.assertEqual(session._callback_path.read_bytes(), original)
        self.assertFalse(list(session.output.glob('.*.tmp')))
        self.assertEqual(session._callback_path.stat().st_mode & 0o777, 0o600)

    def test_callback_environment_errors_are_preserved_and_rejected(self):
        for environment in ({'NSYS_SESSION_NAME': 'wrong'}, {'NSYS_REPORT_PATH': '/wrong'},
                            {'NSYS_CALLBACK_NAME': ''}, {'NSYS_CALLBACK_NAME': '  '}):
            with self.subTest(environment=environment):
                session = self.launched()
                result = self.emit(session, environment)
                self.assertIsNotNone(result['failure'])
                with self.assertRaises((ValueError, RuntimeError)):
                    session.await_report()
                self.assertEqual(self.failure_receipt(session)['callback'], result)
                self.assertFalse(session.report_ready_succeeded)

    def test_nonce_session_time_and_callback_pid_binding(self):
        for key, value in [('nonce', 'b' * 32), ('session', 'other'), ('callback_pid', 0),
                           ('observed_monotonic_ns', 1), ('observed_monotonic_ns', 10**15)]:
            with self.subTest(key=key, value=value):
                session = self.launched()
                receipt = self.emit(session)
                receipt[key] = value
                self.rewrite(session, receipt)
                with self.assertRaises(ValueError):
                    session.await_report()
                self.assertFalse(self.failure_receipt(session)['report_ready_succeeded'])

    def test_report_missing_empty_symlink_and_replaced_are_not_ready(self):
        for kind in ('missing', 'empty', 'symlink', 'changed'):
            with self.subTest(kind=kind):
                session = self.launched()
                receipt = self.emit(session)
                report = session._expected_report
                if kind == 'missing':
                    report.unlink()
                elif kind == 'empty':
                    report.write_bytes(b'')
                elif kind == 'symlink':
                    report.unlink()
                    report.symlink_to(session._callback_path)
                else:
                    report.write_bytes(b'changed bytes')
                with self.assertRaises((ValueError, OSError)):
                    session.await_report()
                self.assertIsNotNone(self.failure_receipt(session)['failure'])

    def test_malformed_oversize_symlink_and_permission_receipts_fail_closed(self):
        for kind in ('malformed', 'oversize', 'symlink', 'permission'):
            with self.subTest(kind=kind):
                session = self.launched()
                if kind == 'malformed':
                    session._callback_path.write_text('{')
                elif kind == 'oversize':
                    session._callback_path.write_bytes(b' ' * (callback.MAX_RECEIPT_BYTES + 1))
                elif kind == 'symlink':
                    report = session._expected_report
                    report.write_bytes(b'{}')
                    session._callback_path.symlink_to(report)
                if kind == 'permission':
                    with patch.object(session, '_read_callback', side_effect=PermissionError('mock denial')):
                        with self.assertRaises(PermissionError):
                            session.await_report()
                else:
                    with self.assertRaises((ValueError, OSError)):
                        session.await_report()
                self.assertIsNotNone(self.failure_receipt(session)['failure'])

    def test_wait_timeout_is_bounded_once_and_no_stop_fallback(self):
        session = self.launched()
        with self.assertRaises(TimeoutError):
            session.await_report(timeout=.1)
        self.assertLessEqual(self.clock, 10.100001)
        with self.assertRaises(RuntimeError):
            session.await_report()
        with self.assertRaises(RuntimeError):
            session.stop()
        self.assertNotIn('stop', [row[0][1] for row in self.calls])
        self.assertIn('deadline', self.failure_receipt(session)['failure'])

    def test_fixed_limits_and_stage_deadline(self):
        session = self.launched()
        for value in (0, -1, 46, float('inf'), float('nan')):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                session.await_report(timeout=value)
        session.deadline = self.clock + .02
        with self.assertRaises(TimeoutError):
            session.await_report(timeout=45)
        self.assertLessEqual(self.clock, 10.020001)
        self.assertEqual(automatic.contract()['report_wait_timeout_seconds'], 45)

    def test_target_and_monitor_checks_cannot_be_repaired_by_callback(self):
        for role in ('target', 'monitor'):
            with self.subTest(role=role):
                session = self.launched()
                self.emit(session)
                def fail():
                    raise RuntimeError(role + ' failed')
                if role == 'monitor':
                    session.check = fail
                with self.assertRaisesRegex(RuntimeError, role + ' failed'):
                    session.await_report(check=fail if role == 'target' else None)
                self.assertFalse(session.report_ready_succeeded)
                self.assertIn(role, self.failure_receipt(session)['failure'])

    def test_launcher_exit_is_not_target_exit(self):
        session = self.launched()
        session.launch_process.returncode = 0
        self.emit(session)
        session.await_report(check=lambda: None)
        self.assertTrue(session.report_ready_succeeded)
        self.assertNotIn('target_returncode', self.failure_receipt(session))

    def test_failure_cleanup_shutdown_does_not_make_collection_pass(self):
        session = self.launched()
        with self.assertRaises(TimeoutError):
            session.await_report(timeout=.01)
        session.shutdown()
        session.close()
        result = json.loads((session.output / 'automatic-close.json').read_text())
        self.assertTrue(result['shutdown_succeeded'])
        self.assertFalse(result['report_ready_succeeded'])
        self.assertFalse(result['stop_succeeded'])

    def test_callback_checks_after_parsing_and_before_acceptance(self):
        session = self.launched()
        self.emit(session)
        original = session._validate_callback
        def delayed(value):
            result = original(value)
            self.clock += 1
            return result
        with patch.object(session, '_validate_callback', side_effect=delayed):
            with self.assertRaises(TimeoutError):
                session.await_report(timeout=.1)
        self.assertFalse(session.report_ready_succeeded)
        self.assertIsNotNone(self.failure_receipt(session)['callback'])

    def test_callback_failure_report_and_preexisting_receipt(self):
        session = self.launched()
        report = session._expected_report
        result = callback.callback(session.name, session._callback_nonce,
            session._callback_path, report, dict(NSYS_SESSION_NAME=session.name,
                NSYS_CALLBACK_NAME='nonempty', NSYS_REPORT_PATH=str(report)))
        self.assertIn('FileNotFoundError', result['failure'])
        with self.assertRaises(RuntimeError):
            session.await_report()
        with self.assertRaises(ValueError):
            automatic.Session('/mock/nsys', session.output,
                automatic.make_name('automatic'), cwd=self.output, env={}, deadline=100)


if __name__ == '__main__':
    unittest.main(verbosity=2)
