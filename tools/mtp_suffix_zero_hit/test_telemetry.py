"""CPU-only contracts for pinned /proc task reads during process exit.

No HTTP, GPU, frequency/temperature sampling or foreign-process detail reads.
The optional kernel test owns and reaps its only child even when it skips.
"""

import errno
import importlib.util
import os
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest import mock


_SPEC = importlib.util.spec_from_file_location(
    'zero_hit_telemetry_under_test', Path(__file__).with_name('telemetry.py'))
telemetry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(telemetry)


def proc_stat(state='R', start_ticks=42):
    """Minimal /proc stat record including state, PPID and starttime."""
    fields = [state, '1'] + ['0'] * 17 + [str(start_ticks)]
    return '999 (owned test task) ' + ' '.join(fields)


class TaskReadFixture:
    """Only _read_at is mocked; production task-read decisions execute."""

    def __init__(self, *, error_field='io', error_errno=errno.EACCES,
                 first_start=42, after_start=42, after_state='Z',
                 recheck_errno=None):
        self.error_field = error_field
        self.error_errno = error_errno
        self.first_start = first_start
        self.after_start = after_start
        self.after_state = after_state
        self.recheck_errno = recheck_errno
        self.calls = []
        self.counts = {'owned_file_read_attempts': 0}

    def read(self, directory_fd, name, counts, context=None):
        self.calls.append((directory_fd, name))
        counts['owned_file_read_attempts'] += 1
        if name == self.error_field:
            error = OSError(self.error_errno, 'isolated task-read fixture')
            error.telemetry_context = dict(
                context or {}, relative_file=name, operation='open',
                directory_fd=directory_fd, errno=self.error_errno)
            error.add_note('isolated fixture context retained')
            raise error
        if name == 'stat':
            if sum(field == 'stat' for _, field in self.calls) == 1:
                return proc_stat(start_ticks=self.first_start)
            if self.recheck_errno is not None:
                raise OSError(self.recheck_errno, 'identity recheck fixture')
            return proc_stat(self.after_state, self.after_start)
        return 'fixture data'

    def run(self):
        with mock.patch.object(telemetry, '_read_at', side_effect=self.read):
            return telemetry._read_task(
                77, self.counts, expected_start=42, with_io=True,
                context={'pid': 999, 'expected_start_ticks': 42})


class PinnedTaskReadTests(unittest.TestCase):
    def test_same_identity_terminal_io_denial_retains_error(self):
        for state in ('Z', 'X'):
            with self.subTest(state=state):
                fixture = TaskReadFixture(after_state=state)
                result = fixture.run()
                self.assertEqual(result['observation'], 'EXITED_DURING_IO_READ')
                self.assertEqual(result['start_ticks'], 42)
                self.assertEqual(
                    telemetry._stat_identity(result['stat_after_error']),
                    dict(state=state, ppid=1, start_ticks=42))
                self.assertEqual(result['terminal_error']['pid'], 999)
                self.assertEqual(result['terminal_error']['relative_file'], 'io')
                self.assertEqual(result['terminal_error']['operation'], 'open')
                self.assertEqual(result['terminal_error']['errno'], errno.EACCES)
                self.assertIn('PermissionError', result['terminal_error_traceback'])
                self.assertIn('fixture context retained',
                              result['terminal_error_traceback'])
                self.assertNotIn('io', result)
                self.assertEqual(fixture.calls, [(77, name) for name in
                    ('stat', 'status', 'schedstat', 'io', 'stat')])
                self.assertEqual(fixture.counts['owned_file_read_attempts'], 5)

    def test_live_task_permission_denial_remains_fatal(self):
        for state in ('R', 'S', 'D', 'T'):
            with self.subTest(state=state):
                with self.assertRaises(PermissionError) as raised:
                    TaskReadFixture(after_state=state).run()
                self.assertEqual(raised.exception.errno, errno.EACCES)

    def test_identity_changed_after_error_remains_fatal(self):
        fixture = TaskReadFixture(after_start=43)
        with self.assertRaises(PermissionError):
            fixture.run()
        self.assertTrue(all(fd == 77 for fd, _ in fixture.calls))

    def test_unavailable_identity_recheck_keeps_original_permission_error(self):
        for code in (errno.ENOENT, errno.ESRCH, errno.EACCES):
            with self.subTest(errno=code):
                with self.assertRaises(PermissionError) as raised:
                    TaskReadFixture(recheck_errno=code).run()
                self.assertEqual(raised.exception.errno, errno.EACCES)
                self.assertEqual(raised.exception.telemetry_context['relative_file'], 'io')
                self.assertTrue(any('identity recheck failed' in note
                                    for note in raised.exception.__notes__))

    def test_non_io_permission_error_is_not_reclassified(self):
        for field in ('status', 'schedstat'):
            with self.subTest(field=field):
                fixture = TaskReadFixture(error_field=field)
                with self.assertRaises(PermissionError):
                    fixture.run()
                self.assertEqual(sum(name == 'stat' for _, name in fixture.calls), 1)

    def test_other_io_error_is_not_reclassified(self):
        for code in (errno.EPERM, errno.EIO):
            with self.subTest(errno=code):
                fixture = TaskReadFixture(error_errno=code)
                with self.assertRaises(OSError) as raised:
                    fixture.run()
                self.assertEqual(raised.exception.errno, code)
                self.assertEqual(sum(name == 'stat' for _, name in fixture.calls), 1)

    def test_initial_pid_reuse_stops_before_details(self):
        fixture = TaskReadFixture(first_start=43)
        result = fixture.run()
        self.assertEqual(result['observation'], 'PID_REUSED')
        self.assertEqual(result['expected_start_ticks'], 42)
        self.assertEqual(result['observed_start_ticks'], 43)
        self.assertEqual(fixture.calls, [(77, 'stat')])

    def test_existing_transient_exit_handling_remains(self):
        for code in (errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=code):
                result = TaskReadFixture(error_errno=code).run()
                self.assertEqual(result['observation'], 'VANISHED_DURING_READ')
                self.assertEqual(result['start_ticks'], 42)
                self.assertNotIn('terminal_error', result)

    def test_complete_live_read_is_unchanged(self):
        fixture = TaskReadFixture(error_field=None, after_state='R')
        result = fixture.run()
        self.assertEqual(result['observation'], 'OK')
        self.assertEqual(result['io'], 'fixture data')
        self.assertEqual(result['stat_after'], proc_stat())
        self.assertEqual(fixture.counts['owned_file_read_attempts'], 5)

    def test_read_at_preserves_pid_file_operation_and_original_errno(self):
        fake_os = SimpleNamespace(
            O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC,
            open=mock.Mock(side_effect=PermissionError(errno.EACCES, 'fixture denied')))
        counts = {'owned_file_read_attempts': 0}
        with mock.patch.object(telemetry, 'os', fake_os):
            with self.assertRaises(PermissionError) as raised:
                telemetry._read_at(77, 'io', counts,
                                   {'pid': 999, 'expected_start_ticks': 42})
        self.assertEqual(raised.exception.telemetry_context, dict(
            pid=999, expected_start_ticks=42, relative_file='io', operation='open',
            directory_fd=77, errno=errno.EACCES))
        self.assertTrue(raised.exception.__notes__)
        self.assertEqual(counts['owned_file_read_attempts'], 1)


class OwnedZombieKernelTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'fork') and Path('/proc/self/stat').is_file(),
                         'requires Linux procfs and fork')
    def test_own_zombie_io_permission_race(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        directory_fd = None
        try:
            deadline = time.monotonic() + 3
            while True:
                identity = telemetry._stat_identity(
                    Path(f'/proc/{child}/stat').read_text())
                if identity['state'] == 'Z':
                    break
                if time.monotonic() >= deadline:
                    self.fail('own child did not reach zombie state')
                time.sleep(.001)
            directory_fd = os.open(f'/proc/{child}',
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            context = dict(pid=child, expected_start_ticks=identity['start_ticks'])
            # Not every kernel denies zombie io. Skip that platform rather
            # than treating a readable io file as a monitor implementation bug.
            try:
                telemetry._read_at(directory_fd, 'io',
                                   {'owned_file_read_attempts': 0}, context)
            except OSError as error:
                self.assertEqual(error.errno, errno.EACCES)
            else:
                self.skipTest('this kernel permits io reads for an own zombie')
            result = telemetry._read_task(directory_fd,
                {'owned_file_read_attempts': 0},
                expected_start=identity['start_ticks'], with_io=True,
                context=context)
            self.assertEqual(result['observation'], 'EXITED_DURING_IO_READ')
            self.assertEqual(result['start_ticks'], identity['start_ticks'])
            self.assertEqual(result['terminal_error']['pid'], child)
            self.assertIn('PermissionError', result['terminal_error_traceback'])
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == '__main__':
    unittest.main()
