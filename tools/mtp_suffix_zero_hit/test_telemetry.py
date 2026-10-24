"""CPU-only contracts for pinned /proc task reads during process exit.

No HTTP, GPU, frequency/temperature sampling or foreign-process detail reads.
The two bounded kernel tests each own and reap one child, including on skip.
Recorded-trace replay creates no children and is not a native monitor run.
"""

import errno
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import tempfile
import threading
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
    """Read, poll and clock are mocked; production read decisions execute."""

    def __init__(self, *, error_field='io', error_errno=errno.EACCES,
                 first_start=42, after_start=42, after_state='Z',
                 recheck_errno=None, rechecks=None, binding_ready=False,
                 confirmation_ready=True, poll_error=None, wait_ns=1000000,
                 first_raw=None, binding_error=None, stat_delays_ns=None,
                 error_handling_delay_ns=0):
        self.error_field = error_field
        self.error_errno = error_errno
        self.first_start = first_start
        self.after_start = after_start
        self.after_state = after_state
        self.recheck_errno = recheck_errno
        self.rechecks = list(rechecks) if rechecks is not None else None
        self.binding_ready = binding_ready
        self.confirmation_ready = confirmation_ready
        self.poll_error = poll_error
        self.binding_error = binding_error
        self.wait_ns = wait_ns
        self.first_raw = first_raw
        self.stat_delays_ns = stat_delays_ns or {}
        self.error_handling_delay_ns = error_handling_delay_ns
        self.now_ns = 1000000000
        self.poll_calls = []
        self.calls = []
        self.counts = {'owned_file_read_attempts': 0}

    def read(self, directory_fd, name, counts, context=None):
        self.calls.append((directory_fd, name))
        counts['owned_file_read_attempts'] += 1
        if name == self.error_field:
            error = OSError(self.error_errno, 'isolated task-read fixture')
            error.telemetry_context = dict(
                context or {}, relative_file=name, operation='open',
                directory_fd=directory_fd, errno=self.error_errno,
                error_monotonic_ns=self.now_ns)
            error.add_note('isolated fixture context retained')
            self.now_ns += self.error_handling_delay_ns
            raise error
        if name == 'stat':
            stat_number = sum(field == 'stat' for _, field in self.calls)
            self.now_ns += self.stat_delays_ns.get(stat_number, 0)
            if stat_number == 1:
                return (self.first_raw if self.first_raw is not None else
                        proc_stat(start_ticks=self.first_start))
            if self.rechecks is not None:
                value = self.rechecks.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value
            if self.recheck_errno is not None:
                raise OSError(self.recheck_errno, 'identity recheck fixture')
            return proc_stat(self.after_state, self.after_start)
        return 'fixture data'

    def poll(self, fd, timeout_ms):
        self.poll_calls.append((fd, timeout_ms))
        started = self.now_ns
        if not timeout_ms and self.binding_error is not None:
            raise self.binding_error
        if timeout_ms:
            self.now_ns += self.wait_ns
            if self.poll_error is not None:
                raise self.poll_error
        ready = self.confirmation_ready if timeout_ms else self.binding_ready
        return dict(started_ns=started, finished_ns=self.now_ns,
                    timeout_ms=timeout_ms,
                    events=[[fd, select.POLLIN]] if ready else [], ready=ready)

    def run(self, *, exit_fd=None, budget=None):
        with mock.patch.object(telemetry, '_read_at', side_effect=self.read), \
                mock.patch.object(telemetry, '_poll_exit_fd', side_effect=self.poll), \
                mock.patch.object(telemetry.time, 'monotonic_ns',
                                  side_effect=lambda: self.now_ns):
            return telemetry._read_task(
                77, self.counts, expected_start=42, with_io=True,
                context={'pid': 999, 'expected_start_ticks': 42},
                exit_fd=exit_fd, exit_budget=budget)


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
                state = raised.exception.telemetry_task_state
                self.assertEqual(state['rechecks'][0]['errno'], code)
                self.assertIn('status', state['partial_read'])
                self.assertIn('schedstat', state['partial_read'])

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
        detail = raised.exception.telemetry_context
        self.assertIsInstance(detail['error_monotonic_ns'], int)
        self.assertEqual({k: v for k, v in detail.items()
                          if k != 'error_monotonic_ns'}, dict(
            pid=999, expected_start_ticks=42, relative_file='io', operation='open',
            directory_fd=77, errno=errno.EACCES))
        self.assertTrue(raised.exception.__notes__)
        self.assertEqual(counts['owned_file_read_attempts'], 1)


class BoundPidfdReadTests(unittest.TestCase):
    @staticmethod
    def budget(remaining=100000000):
        return dict(remaining_ns=remaining, spent_ns=0)

    def assert_denied(self, fixture, decision, *, budget=None, exit_fd=98):
        with self.assertRaises(PermissionError) as raised:
            fixture.run(exit_fd=exit_fd,
                        budget=self.budget() if budget is None else budget)
        error = raised.exception
        self.assertEqual(error.errno, errno.EACCES)
        self.assertEqual(error.telemetry_context['relative_file'], 'io')
        self.assertEqual(error.telemetry_task_state['decision'], decision)
        self.assertNotIn('io', error.telemetry_task_state['partial_read'])
        return error.telemetry_task_state

    def test_live_then_terminal_is_retained_missing_io(self):
        fixture = TaskReadFixture(rechecks=[proc_stat('R'), proc_stat('Z')])
        budget = self.budget()
        row = fixture.run(exit_fd=98, budget=budget)
        self.assertEqual(row['observation'], 'TERMINATED_AFTER_IO_DENIAL')
        state = row['terminal_task_state']
        self.assertTrue(state['live_pidfd_bound'])
        self.assertFalse(state['cause_of_permission_error_proven'])
        self.assertTrue(state['io_missing'])
        self.assertEqual([r['identity']['state'] for r in state['rechecks']],
                         ['R', 'Z'])
        self.assertEqual(fixture.poll_calls, [(98, 0), (98, 50)])
        self.assertEqual(budget, dict(remaining_ns=99000000, spent_ns=1000000))
        self.assertEqual(row['terminal_error']['errno'], errno.EACCES)
        self.assertIn('PermissionError', row['terminal_error_traceback'])
        self.assertNotIn('io', row)
        self.assertEqual(sum(name == 'io' for _, name in fixture.calls), 1)

    def test_immediate_disappearance_requires_bound_exit_proof(self):
        for code in (errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=code):
                fixture = TaskReadFixture(rechecks=[OSError(code, 'gone'),
                                                   OSError(code, 'reaped')])
                row = fixture.run(exit_fd=98, budget=self.budget())
                self.assertEqual(row['observation'], 'TERMINATED_AFTER_IO_DENIAL')
                self.assertEqual([r['errno'] for r in
                                  row['terminal_task_state']['rechecks']],
                                 [code, code])

    def test_final_disappearance_keeps_original_denial(self):
        for code in (errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=code):
                fixture = TaskReadFixture(rechecks=[proc_stat('S'),
                                                   OSError(code, 'reaped')])
                row = fixture.run(exit_fd=98, budget=self.budget())
                self.assertEqual(row['observation'], 'TERMINATED_AFTER_IO_DENIAL')
                self.assertEqual(row['terminal_error']['errno'], errno.EACCES)

    def test_persistent_live_denial_is_fatal(self):
        for state in ('R', 'S', 'D', 'T'):
            with self.subTest(state=state):
                fixture = TaskReadFixture(after_state=state,
                                          confirmation_ready=False)
                saved = self.assert_denied(fixture, 'LIVE_TASK_EXIT_TIMEOUT')
                self.assertEqual(saved['rechecks'][0]['identity']['state'], state)

    def test_ready_at_binding_does_not_prove_later_live_identity(self):
        fixture = TaskReadFixture(after_state='R', binding_ready=True)
        self.assert_denied(fixture, 'NO_PRIOR_LIVE_PIDFD_BINDING')
        self.assertEqual(fixture.poll_calls, [(98, 0)])

    def test_missing_or_exhausted_budget_never_waits(self):
        fixture = TaskReadFixture(after_state='R')
        with self.assertRaises(PermissionError) as raised:
            fixture.run(exit_fd=98)
        self.assertEqual(raised.exception.telemetry_task_state['decision'],
                         'NO_EXIT_WAIT_BUDGET')
        for remaining in (0, -1, 999999):
            with self.subTest(remaining=remaining):
                fixture = TaskReadFixture(after_state='R')
                self.assert_denied(fixture, 'EXIT_WAIT_BUDGET_EXHAUSTED',
                                   budget=self.budget(remaining))
                self.assertEqual(fixture.poll_calls, [(98, 0)])

    def test_wait_allowance_uses_shared_budget_and_actual_time(self):
        budget = self.budget(7500000)
        for expected_timeout in (7, 6):
            fixture = TaskReadFixture(rechecks=[proc_stat('R'), proc_stat('Z')])
            fixture.run(exit_fd=98, budget=budget)
            self.assertEqual(fixture.poll_calls[-1], (98, expected_timeout))
        self.assertEqual(budget, dict(remaining_ns=5500000, spent_ns=2000000))

    def test_ready_after_deadline_or_shared_allowance_is_fatal(self):
        for remaining, elapsed in ((100000000, 50000001), (5000000, 5000001)):
            with self.subTest(remaining=remaining, elapsed=elapsed):
                fixture = TaskReadFixture(after_state='R', wait_ns=elapsed)
                saved = self.assert_denied(
                    fixture, 'EXIT_CONFIRMATION_BUDGET_OVERRUN',
                    budget=self.budget(remaining))
                self.assertTrue(saved['exit_confirmation']['ready'])
                self.assertEqual(saved['exit_wait_spent_ns'], elapsed)

    def test_final_stat_completion_must_remain_within_task_deadline(self):
        for final_read_ns in (49000000, 49000001):
            with self.subTest(final_read_ns=final_read_ns):
                fixture = TaskReadFixture(
                    rechecks=[proc_stat('R'), proc_stat('Z')], wait_ns=1000000,
                    stat_delays_ns={3: final_read_ns})
                if final_read_ns == 49000000:
                    row = fixture.run(exit_fd=98, budget=self.budget())
                    saved = row['terminal_task_state']
                    self.assertEqual(row['observation'], 'TERMINATED_AFTER_IO_DENIAL')
                    self.assertEqual(saved['finished_ns'],
                                     saved['exit_confirmation_deadline_ns'])
                else:
                    saved = self.assert_denied(
                        fixture, 'EXIT_CONFIRMATION_BUDGET_OVERRUN')
                    self.assertGreater(saved['rechecks'][-1]['finished_ns'],
                                       saved['exit_confirmation_deadline_ns'])
                self.assertTrue(saved['exit_confirmation']['ready'])
                self.assertEqual(saved['rechecks'][-1]['stat'], proc_stat('Z'))
                self.assertEqual(saved['original_error']['errno'], errno.EACCES)

    def test_deadline_starts_at_recorded_io_error_not_catch_time(self):
        for handling_delay, decision in (
                (49000000, 'EXIT_CONFIRMATION_BUDGET_OVERRUN'),
                (50000001, 'EXIT_WAIT_BUDGET_EXHAUSTED')):
            with self.subTest(handling_delay=handling_delay):
                fixture = TaskReadFixture(
                    rechecks=[proc_stat('R'), proc_stat('Z')], wait_ns=2000000,
                    error_handling_delay_ns=handling_delay)
                saved = self.assert_denied(fixture, decision)
                self.assertEqual(saved['error_observed_ns'], 1000000000)
                self.assertEqual(saved['original_error']['error_monotonic_ns'],
                                 saved['error_observed_ns'])
                self.assertEqual(saved['exit_confirmation_deadline_ns'], 1050000000)
                self.assertEqual(fixture.poll_calls,
                                 [(98, 0), (98, 1)] if handling_delay == 49000000
                                 else [(98, 0)])

    def test_final_identity_errors_and_live_contradiction_are_fatal(self):
        cases = [(proc_stat('Z', 43), 'IDENTITY_CHANGED_AFTER_EXIT_POLL'),
                 (proc_stat('R'), 'EXIT_POLL_CONTRADICTS_LIVE_STAT'),
                 (OSError(errno.EACCES, 'denied'), 'FINAL_IDENTITY_RECHECK_FAILED'),
                 (OSError(errno.EIO, 'bad read'), 'FINAL_IDENTITY_RECHECK_FAILED'),
                 ('malformed stat', 'FINAL_IDENTITY_RECHECK_FAILED')]
        for final, decision in cases:
            with self.subTest(decision=decision, final=repr(final)):
                saved = self.assert_denied(
                    TaskReadFixture(rechecks=[proc_stat('R'), final]), decision)
                self.assertEqual(len(saved['rechecks']), 2)

    def test_poll_errors_do_not_replace_original_errno_or_retry(self):
        for code in (errno.EINTR, errno.EIO, errno.EBADF):
            with self.subTest(errno=code):
                failure = OSError(code, 'poll failed')
                failure.telemetry_poll = dict(events=[[98, select.POLLNVAL]])
                fixture = TaskReadFixture(after_state='R', poll_error=failure)
                saved = self.assert_denied(fixture, 'EXIT_POLL_FAILED')
                self.assertEqual(saved['exit_confirmation']['errno'], code)
                self.assertEqual(saved['exit_confirmation']['poll'],
                                 failure.telemetry_poll)
                self.assertEqual(len(fixture.poll_calls), 2)

    def test_bad_immediate_recheck_never_polls_for_exit(self):
        for value in (OSError(errno.EACCES, 'denied'),
                      OSError(errno.EIO, 'bad read'), 'malformed stat'):
            with self.subTest(value=repr(value)):
                fixture = TaskReadFixture(rechecks=[value])
                self.assert_denied(fixture, 'IDENTITY_RECHECK_FAILED')
                self.assertEqual(fixture.poll_calls, [(98, 0)])

    def test_all_denial_outcomes_retain_acquired_raw_state(self):
        fixture = TaskReadFixture(after_state='S', confirmation_ready=False)
        saved = self.assert_denied(fixture, 'LIVE_TASK_EXIT_TIMEOUT')
        self.assertEqual(saved['partial_read']['stat'], proc_stat())
        self.assertEqual(saved['partial_read']['status'], 'fixture data')
        self.assertEqual(saved['partial_read']['schedstat'], 'fixture data')
        self.assertEqual(saved['original_error']['pid'], 999)
        self.assertEqual(saved['rechecks'][0]['stat'], proc_stat('S'))
        self.assertLessEqual(saved['read_started_ns'], saved['error_observed_ns'])
        self.assertLessEqual(saved['error_observed_ns'], saved['finished_ns'])

    def test_initial_and_final_parse_errors_keep_raw_state(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                fixture = (TaskReadFixture(first_raw='malformed stat') if initial
                           else TaskReadFixture(error_field=None,
                                                rechecks=['malformed stat']))
                with self.assertRaises((ValueError, IndexError)) as raised:
                    fixture.run()
                saved = raised.exception.telemetry_task_state
                key = 'stat' if initial else 'stat_after'
                self.assertEqual(saved['partial_read'][key], 'malformed stat')

    def test_binding_failure_keeps_poll_evidence(self):
        failure = OSError(errno.EIO, 'invalid binding poll')
        failure.telemetry_poll = dict(events=[[98, select.POLLNVAL]])
        fixture = TaskReadFixture(binding_error=failure)
        with self.assertRaises(OSError) as raised:
            fixture.run(exit_fd=98, budget=self.budget())
        saved = raised.exception.telemetry_task_state
        self.assertEqual(saved['pidfd_binding_error'], failure.telemetry_poll)
        self.assertFalse(saved['live_pidfd_bound'])
        self.assertEqual(fixture.calls, [(77, 'stat')])


class PollAndOwnershipTests(unittest.TestCase):
    def test_pidfd_poll_terminal_and_nonterminal_flags(self):
        for flags in (None, select.POLLIN, select.POLLHUP,
                      select.POLLIN | select.POLLRDNORM,
                      select.POLLIN | select.POLLHUP):
            with self.subTest(flags=flags):
                poller = mock.Mock()
                events = [] if flags is None else [(98, flags)]
                poller.poll.return_value = events
                with mock.patch.object(telemetry.select, 'poll', return_value=poller):
                    row = telemetry._poll_exit_fd(98, 7)
                self.assertEqual(row['ready'], flags is not None)
                self.assertEqual(row['events'], [list(e) for e in events])
                poller.register.assert_called_once_with(98, select.POLLIN)
                poller.poll.assert_called_once_with(7)

    def test_pidfd_poll_bad_flags_or_wrong_fd_remain_errors(self):
        for event in ((98, select.POLLERR), (98, select.POLLNVAL),
                      (98, select.POLLIN | select.POLLERR),
                      (97, select.POLLIN), (98, select.POLLOUT)):
            with self.subTest(event=event):
                poller = mock.Mock()
                poller.poll.return_value = [event]
                with mock.patch.object(telemetry.select, 'poll', return_value=poller):
                    with self.assertRaises(OSError) as raised:
                        telemetry._poll_exit_fd(98, 0)
                self.assertEqual(raised.exception.errno, errno.EIO)
                self.assertEqual(raised.exception.telemetry_poll['events'],
                                 [list(event)])

    def test_owned_binding_order_and_descriptor_cleanup(self):
        record = {999: dict(start_ticks=42, executable='/usr/bin/python3')}
        for error in (None, PermissionError(errno.EACCES, 'read failed')):
            with self.subTest(error=repr(error)):
                events = []
                def pidfd(pid):
                    events.append(('pidfd', pid))
                    return 98
                def proc_open(path, flags):
                    events.append(('proc', path))
                    return 77
                def read(*args, **kwargs):
                    events.append(('read', args[0], kwargs['exit_fd']))
                    if error is not None:
                        raise error
                    return dict(observation='OK')
                with mock.patch.object(telemetry.os, 'pidfd_open', side_effect=pidfd), \
                        mock.patch.object(telemetry.os, 'open', side_effect=proc_open), \
                        mock.patch.object(telemetry.os, 'close') as close, \
                        mock.patch.object(telemetry, '_read_task', side_effect=read):
                    if error is None:
                        telemetry._sample_processes(record, [999], {})
                    else:
                        with self.assertRaises(PermissionError):
                            telemetry._sample_processes(record, [999], {})
                self.assertEqual(events, [('pidfd', 999), ('proc', '/proc/999'),
                                          ('read', 77, 98)])
                self.assertEqual(close.call_args_list, [mock.call(77), mock.call(98)])

    def test_partial_binding_closes_pidfd_and_preserves_failure(self):
        records = {999: dict(start_ticks=42)}
        for code in (errno.EACCES, errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=code):
                with mock.patch.object(telemetry.os, 'pidfd_open', return_value=98), \
                        mock.patch.object(telemetry.os, 'open',
                                          side_effect=OSError(code, 'proc open')), \
                        mock.patch.object(telemetry.os, 'close') as close:
                    if code == errno.EACCES:
                        with self.assertRaises(PermissionError) as raised:
                            telemetry._sample_processes(records, [999], {})
                        self.assertEqual(raised.exception.telemetry_task_state['pid'], 999)
                    else:
                        rows, _ = telemetry._sample_processes(records, [999], {})
                        self.assertEqual(rows[999]['observation'], 'VANISHED')
                close.assert_called_once_with(98)

    def test_pidfd_open_error_does_not_open_proc_or_close_unowned_fd(self):
        records = {999: dict(start_ticks=42)}
        for code in (errno.EMFILE, errno.ESRCH):
            with self.subTest(errno=code):
                with mock.patch.object(telemetry.os, 'pidfd_open',
                                      side_effect=OSError(code, 'pidfd open')), \
                        mock.patch.object(telemetry.os, 'open') as proc_open, \
                        mock.patch.object(telemetry.os, 'close') as close:
                    if code == errno.EMFILE:
                        with self.assertRaises(OSError) as raised:
                            telemetry._sample_processes(records, [999], {})
                        self.assertEqual(raised.exception.telemetry_task_state['errno'], code)
                    else:
                        rows, _ = telemetry._sample_processes(records, [999], {})
                        self.assertEqual(rows[999]['observation'], 'VANISHED')
                proc_open.assert_not_called()
                close.assert_not_called()

    def test_owned_pid_capacity_remains_bounded(self):
        records = {1: dict(ppid=0, start_ticks=1)}
        records.update({pid: dict(ppid=1, start_ticks=pid)
                        for pid in range(2, telemetry.MAX_OWNED_PIDS + 2)})
        with self.assertRaises(telemetry.EnvironmentAbort):
            telemetry._owned_snapshot(records, 1, 1, {})

    def test_q4t_thread_capacity_closes_all_bound_descriptors(self):
        records = {999: dict(start_ticks=42, executable='/tmp/q4t')}
        with mock.patch.object(telemetry.os, 'pidfd_open', return_value=98), \
                mock.patch.object(telemetry.os, 'open', side_effect=[77, 88]), \
                mock.patch.object(telemetry.os, 'listdir',
                                  return_value=[str(i) for i in range(129)]), \
                mock.patch.object(telemetry.os, 'close') as close, \
                mock.patch.object(telemetry, '_read_task',
                                  return_value=dict(observation='OK')):
            with self.assertRaises(telemetry.EnvironmentAbort):
                telemetry._sample_processes(records, [999],
                                             {'directory_scan_attempts': 0})
        self.assertEqual(close.call_args_list,
                         [mock.call(88), mock.call(77), mock.call(98)])


class SummaryFailureTests(unittest.TestCase):
    def test_sampler_summary_preserves_task_failure_without_source_sampling(self):
        # Every source/process access is mocked; sample() runs no real scan.
        failure = PermissionError(errno.EACCES, 'isolated denied io')
        failure.telemetry_context = dict(pid=999, relative_file='io')
        failure.telemetry_task_state = dict(decision='LIVE_TASK_EXIT_TIMEOUT',
                                           partial_read={'stat': proc_stat()})
        stop, failures, heartbeat = threading.Event(), [], {}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'memory.jsonl'
            with mock.patch.object(telemetry, '_configured', ((), '[]', '{}')), \
                    mock.patch.object(telemetry.os, 'getpid', return_value=999), \
                    mock.patch.object(telemetry.Path, 'read_text', return_value=proc_stat()), \
                    mock.patch.object(telemetry.gpu_exclusive, 'processes',
                        return_value={999: dict(ppid=1, start_ticks=42)}), \
                    mock.patch.object(telemetry, '_sample_processes', side_effect=failure):
                telemetry.sample(output, stop, failures, heartbeat)
            summary = json.loads((Path(temporary) / 'telemetry-summary.json').read_text())
        self.assertTrue(stop.is_set())
        self.assertEqual(len(failures), 1)
        self.assertEqual(summary['sampler_status'], 'ENV_ABORT')
        self.assertEqual(summary['sampler_failure_task_state'], failure.telemetry_task_state)
        self.assertEqual(summary['sampler_failure_context'], failure.telemetry_context)
        self.assertEqual(summary['sample_attempts'], 1)
        self.assertEqual(summary['flushed_samples'], 0)
        self.assertIn('PermissionError', summary['sampler_failure_traceback'])

    def test_summary_write_error_is_not_swallowed(self):
        stop, failures = threading.Event(), []
        with mock.patch.object(telemetry, '_configured', None), \
                mock.patch.object(telemetry.Path, 'open',
                                  side_effect=PermissionError(errno.EACCES, 'summary')):
            telemetry.sample(Path('/unused/memory.jsonl'), stop, failures, {})
        self.assertTrue(stop.is_set())
        self.assertEqual(len(failures), 2)
        self.assertIn('telemetry summary', failures[-1])

    def test_discovered_source_failure_still_aborts_before_process_scan(self):
        stop, failures = threading.Event(), []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'memory.jsonl'
            configured = ((('fixed_source', '/mock/frozen-source'),), '[]', '{}')
            with mock.patch.object(telemetry, '_configured', configured), \
                    mock.patch.object(telemetry.Path, 'read_text',
                        side_effect=[proc_stat(), PermissionError(errno.EACCES, 'source')]), \
                    mock.patch.object(telemetry.gpu_exclusive, 'processes') as scan:
                telemetry.sample(output, stop, failures, {})
            summary = json.loads((Path(temporary) / 'telemetry-summary.json').read_text())
        scan.assert_not_called()
        self.assertTrue(stop.is_set())
        self.assertEqual(summary['sampler_status'], 'ENV_ABORT')
        self.assertIn('frozen source fixed_source', summary['sampler_failure'])
        self.assertEqual(summary['final_attempt_read_counts']['source_file_read_attempts'], 1)


class RecordedExitTraceReplayTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('Q4T_EXIT_IO_REPRODUCTION_PATH'),
                         'requires explicitly selected frozen CPU reproduction')
    def test_fixed_64_real_traces_against_old_and_new_decisions(self):
        """Replay decisions, not 64 native executions of the new monitor.

        The recorded pre-go live handshake supplies the mocked nonready
        binding. No pre-error poll was recorded. Subsequent denial, immediate
        stat, final terminal stat and ready flags come from the frozen trace.
        """
        source = Path(os.environ['Q4T_EXIT_IO_REPRODUCTION_PATH'])
        trace = json.loads(source.read_text())
        old_path = Path(os.environ.get('Q4T_EXIT_IO_OLD_TELEMETRY_PATH',
            str(Path(__file__).resolve().parents[2] /
                '.q4t-work/suffix-execution-20261024/diagnostic-v1/'
                'source-snapshot/tools/mtp_suffix_zero_hit/telemetry.py')))
        self.assertEqual(hashlib.sha256(old_path.read_bytes()).hexdigest(),
                         '473d0b472304cbea8a54bf5fb95fcf2fbea47c9b1f7c57aec8e5fa243fd42cbe')
        spec = importlib.util.spec_from_file_location('frozen_old_exit_telemetry', old_path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        self.assertTrue(trace['complete_fixed_pass'])
        self.assertTrue(trace['all_created_children_reaped'])
        self.assertEqual(len(trace['children']), 64)
        old_pass = old_denied = new_pass = new_confirmed = 0
        for child in trace['children']:
            denial = child['io_denial']
            with self.subTest(index=child['index'], shape=child['shape']):
                self.assertTrue(child['pidfd_opened_before_go'])
                self.assertTrue(child['cleanup']['reaped'])
                self.assertEqual(denial['io_error']['errno'], errno.EACCES)
                self.assertTrue(denial['final_pidfd_poll']['exit_ready'])
                self.assertIn(denial['final_stat']['identity']['state'], ('Z', 'X'))
                self.assertEqual(denial['final_stat']['identity']['start_ticks'],
                                 child['start_ticks'])
                elapsed = (denial['final_pidfd_poll']['finished_monotonic_ns'] -
                           denial['io_error']['started_monotonic_ns'])
                self.assertGreaterEqual(elapsed, 0)
                self.assertLessEqual(elapsed, 50000000)

                def replay(module, new):
                    now = [child['initial_stat']['started_monotonic_ns']]
                    stat_rows = iter([child['initial_stat'],
                                      denial['stat_after_error'], denial['final_stat']])
                    calls = []

                    def read(fd, name, counts, context=None):
                        calls.append((fd, name))
                        counts['owned_file_read_attempts'] += 1
                        if name == 'stat':
                            row = next(stat_rows)
                        elif name == 'status':
                            row = child['initial_status']
                        elif name == 'schedstat':
                            row = denial['schedstat']
                        else:
                            self.assertEqual(name, 'io')
                            row = denial['io_error']
                            now[0] = row['finished_monotonic_ns']
                            failure = PermissionError(errno.EACCES,
                                                      'replayed recorded io denial')
                            failure.telemetry_context = dict(
                                context or {}, relative_file='io',
                                operation=row['operation'], directory_fd=fd,
                                errno=errno.EACCES,
                                error_monotonic_ns=row['started_monotonic_ns'])
                            raise failure
                        now[0] = max(now[0], row['finished_monotonic_ns'])
                        return row['raw']

                    def poll(fd, timeout_ms):
                        started = now[0]
                        if timeout_ms == 0:
                            self.assertLess(now[0], child['go_monotonic_ns'])
                            return dict(started_ns=started, finished_ns=started,
                                        timeout_ms=0, events=[], ready=False,
                                        replay_basis='pre-go live handshake; not native poll')
                        self.assertGreater(timeout_ms, 0)
                        self.assertLessEqual(timeout_ms, 50)
                        recorded = denial['final_pidfd_poll']
                        now[0] = recorded['finished_monotonic_ns']
                        return dict(started_ns=started, finished_ns=now[0],
                                    timeout_ms=timeout_ms,
                                    events=[[fd, recorded['event_mask']]], ready=True)

                    kwargs = dict(expected_start=child['start_ticks'], with_io=True,
                                  context=dict(pid=child['pid'],
                                               expected_start_ticks=child['start_ticks']))
                    if new:
                        kwargs.update(exit_fd=98,
                                      exit_budget=dict(remaining_ns=100000000, spent_ns=0))
                    with mock.patch.object(module, '_read_at', side_effect=read), \
                            mock.patch.object(module.time, 'monotonic_ns',
                                              side_effect=lambda: now[0]):
                        if new:
                            with mock.patch.object(module, '_poll_exit_fd', side_effect=poll):
                                result = module._read_task(
                                    77, {'owned_file_read_attempts': 0}, **kwargs)
                        else:
                            result = module._read_task(
                                77, {'owned_file_read_attempts': 0}, **kwargs)
                    self.assertEqual(sum(name == 'io' for _, name in calls), 1)
                    self.assertTrue(all(fd == 77 for fd, _ in calls))
                    return result

                try:
                    previous = replay(old, False)
                except PermissionError as error:
                    old_denied += 1
                    self.assertEqual(error.errno, errno.EACCES)
                    self.assertEqual(error.telemetry_context['pid'], child['pid'])
                else:
                    old_pass += 1
                    self.assertEqual(previous['observation'], 'EXITED_DURING_IO_READ')
                current = replay(telemetry, True)
                new_pass += 1
                new_confirmed += current['observation'] == 'TERMINATED_AFTER_IO_DENIAL'
                self.assertEqual(current['terminal_error']['errno'], errno.EACCES)
                self.assertEqual(current['terminal_error']['pid'], child['pid'])
                self.assertNotIn('io', current)
                self.assertEqual(current['stat_after_error'], denial['stat_after_error']['raw'])
        self.assertEqual((old_pass, old_denied, new_pass, new_confirmed), (8, 56, 64, 56))


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

    @unittest.skipUnless(hasattr(os, 'fork') and hasattr(os, 'pidfd_open') and
                         Path('/proc/self/stat').is_file(),
                         'requires Linux procfs, fork and pidfd_open')
    def test_live_nondumpable_owned_process_remains_fatal(self):
        # The only additional real child. It never executes GPU/HTTP code and
        # has a bounded self-exit even if the parent assertion fails.
        ready_read, ready_write = os.pipe()
        finish_read, finish_write = os.pipe()
        started = time.monotonic()
        child = os.fork()
        if child == 0:
            os.close(ready_read)
            os.close(finish_write)
            exit_code = 2
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                result = libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4.
                os.write(ready_write, b'R' if result == 0 else b'E')
                os.close(ready_write)
                select.select([finish_read], [], [], 2.5)
                os.close(finish_read)
                exit_code = 0 if result == 0 else 2
            finally:
                os._exit(exit_code)
        os.close(ready_write)
        os.close(finish_read)
        directory_fd = pidfd = None
        try:
            ready, _, _ = select.select([ready_read], [], [], 2)
            self.assertTrue(ready, 'owned child did not initialize')
            self.assertEqual(os.read(ready_read, 1), b'R', 'prctl failed')
            pidfd = os.pidfd_open(child)
            directory_fd = os.open(f'/proc/{child}',
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            counts = {'owned_file_read_attempts': 0}
            identity = telemetry._stat_identity(
                telemetry._read_at(directory_fd, 'stat', counts))
            context = dict(pid=child, expected_start_ticks=identity['start_ticks'])
            try:
                telemetry._read_at(directory_fd, 'io', counts, context)
            except OSError as error:
                self.assertEqual(error.errno, errno.EACCES)
            else:
                self.skipTest('current credentials/kernel allow nondumpable child io')
            with self.assertRaises(PermissionError) as raised:
                telemetry._read_task(
                    directory_fd, counts, identity['start_ticks'], with_io=True,
                    context=context, exit_fd=pidfd,
                    exit_budget=dict(remaining_ns=100000000, spent_ns=0))
            state = raised.exception.telemetry_task_state
            self.assertTrue(state['live_pidfd_bound'])
            self.assertIn(state['decision'], ('LIVE_TASK_EXIT_TIMEOUT',
                                             'EXIT_CONFIRMATION_BUDGET_OVERRUN'))
            self.assertFalse(state['exit_confirmation']['ready'])
            self.assertEqual(state['original_error']['errno'], errno.EACCES)
            self.assertEqual(state['rechecks'][0]['identity']['start_ticks'],
                             identity['start_ticks'])
            self.assertNotIn(state['rechecks'][0]['identity']['state'], ('Z', 'X'))
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if pidfd is not None:
                os.close(pidfd)
            os.close(ready_read)
            try:
                os.write(finish_write, b'F')
            except BrokenPipeError:
                pass
            os.close(finish_write)
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertLess(time.monotonic() - started, 3)


if __name__ == '__main__':
    unittest.main()
