"""CPU contracts for PF_EXITING pending; no native processes or source sampling.

The frozen failed record is replayed as a decision input, not a new native
monitor run. All proc reads, clock, polls and descriptor operations are mocked.
"""
from collections import Counter
import copy
import errno
import hashlib
import importlib.util
import json
from pathlib import Path
import select
import tempfile
import threading
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'exit_pending_telemetry', Path(__file__).with_name('telemetry.py'))
telemetry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(telemetry)
ORIGINAL_POLL = telemetry._poll_exit_fd
ROOT = Path(__file__).resolve().parents[2]
FAILURE = ROOT / '.q4t-work/suffix-nsys-20261024/diagnostic-v1/run/telemetry-summary.json'
FAILURE_SHA = 'a533d3bdf62558ee8c47ead6db80c6ad6884123fb05362189dcb3452d91a145c'


def stat(state='R', ticks=42, flags=4, vsize=0, pid=999):
    values = [state, '1'] + ['0'] * 42
    values[6], values[19], values[20] = str(flags), str(ticks), str(vsize)
    return str(pid) + ' (owned pending fixture) ' + ' '.join(values)


def identity(state='R', ticks=42):
    return dict(state=state, ppid=1, start_ticks=ticks)


def pending_state(*, pid=999, ticks=42, now=1_000_000_000):
    raw = stat(ticks=ticks, pid=pid)
    detail = dict(pid=pid, expected_start_ticks=ticks, relative_file='io',
                  operation='open', directory_fd=77, errno=errno.EACCES,
                  error_monotonic_ns=now, error="PermissionError(13, 'fixture')")
    poll = dict(started_ns=now, finished_ns=now, timeout_ms=0, events=[], ready=False)
    initial = identity(ticks=ticks)
    return dict(read_started_ns=now, error_observed_ns=now, initial_identity=initial,
        partial_read=dict(observation='OK', start_ticks=ticks, stat=raw,
                          status='Name:\tfixture\nKthread:\t0\nFDSize:\t0\n',
                          schedstat='0 0 0\n', pidfd_binding=poll),
        original_error=detail, live_pidfd_bound=True, pidfd_binding=poll,
        pidfd_binding_error=None,
        rechecks=[dict(started_ns=now, finished_ns=now, stat=raw, identity=dict(initial))],
        decision='UNCLASSIFIED_FATAL_READ')


class Harness:
    def __init__(self):
        self.now = 1_000_000_000
        self.calls, self.polls, self.closed, self.duplicated = [], [], [], []
        self.dup_error_at = None
        self.ready = False
        self.poll_error = None
        self.final_error = None
        self.read_delay = 0
        self.final_raw = stat('Z')
        self.raws = []
        self.error_field = 'io'
        self.error_errno = errno.EACCES
        self.status = 'Name:\tfixture\nKthread:\t0\nFDSize:\t0\n'

    def duplicate(self, fd):
        self.duplicated.append(fd)
        if len(self.duplicated) == self.dup_error_at:
            raise OSError(errno.EMFILE, 'controlled dup failure')
        return 1000 + len(self.duplicated)

    def read(self, fd, name, counts, context=None):
        self.calls.append((fd, name))
        counts['owned_file_read_attempts'] += 1
        if name == self.error_field:
            error = OSError(self.error_errno, 'controlled pending read failure')
            error.telemetry_context = dict(context or {}, relative_file=name,
                operation='open', directory_fd=fd, errno=self.error_errno,
                error_monotonic_ns=self.now)
            raise error
        if name == 'stat':
            if self.raws:
                return self.raws.pop(0)
            self.now += self.read_delay
            if self.final_error:
                raise OSError(self.final_error, 'controlled final pinned stat failure')
            return self.final_raw
        return self.status if name == 'status' else '0 0 0\n'

    def poll(self, fd, timeout_ms):
        self.polls.append((fd, timeout_ms))
        if self.poll_error:
            raise self.poll_error
        return dict(started_ns=self.now, finished_ns=self.now, timeout_ms=timeout_ms,
                    events=[[fd, select.POLLIN]] if self.ready else [], ready=self.ready)

    def patches(self):
        return (mock.patch.object(telemetry.os, 'dup', side_effect=self.duplicate),
                mock.patch.object(telemetry.os, 'close', side_effect=self.closed.append),
                mock.patch.object(telemetry.time, 'monotonic_ns', side_effect=lambda: self.now),
                mock.patch.object(telemetry, '_read_at', side_effect=self.read),
                mock.patch.object(telemetry, '_poll_exit_fd', side_effect=self.poll))


class ExitPendingTests(unittest.TestCase):
    def setUp(self):
        # Register this before mocked descriptor operations, so rmtree runs
        # only after the real os.close has been restored.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output_root = Path(self.temporary.name)
        self.h = Harness()
        for patch in self.h.patches():
            patch.start()
            self.addCleanup(patch.stop)
        self.pending = telemetry._ExitPending()
        self.addCleanup(self.pending.close)
        self.counts = Counter()

    def admit(self, state=None, *, pid=999, ticks=42):
        state = pending_state(pid=pid, ticks=ticks, now=self.h.now) if state is None else state
        return self.pending.admit(77, 98, dict(pid=pid, expected_start_ticks=ticks),
                                  identity(ticks=ticks), state)

    def read_task(self, first=None, after=None):
        self.h.raws = [first or stat(), after or stat()]
        return telemetry._read_task(77, self.counts, 42, with_io=True,
            context=dict(pid=999, expected_start_ticks=42), exit_fd=98,
            exit_budget=dict(remaining_ns=100_000_000, spent_ns=0), pending=self.pending)

    def test_pending_preserves_error_without_terminal_or_synthetic_io(self):
        row = self.read_task()
        self.assertEqual(row['observation'], 'EXITING_IO_UNAVAILABLE')
        state = row['terminal_task_state']
        self.assertEqual(state['decision'], 'SAME_IDENTITY_PF_EXITING_PENDING')
        self.assertTrue(state['not_terminal'])
        self.assertTrue(state['io_missing'])
        self.assertFalse(state['cause_of_permission_error_proven'])
        self.assertEqual(state['pending_id'], '999:42')
        self.assertNotIn('io', row)
        self.assertEqual(row['terminal_error']['errno'], errno.EACCES)
        self.assertIn('PermissionError', row['terminal_error_traceback'])
        self.assertEqual(sum(name == 'io' for fd, name in self.h.calls), 1)
        self.assertTrue(all(timeout == 0 for fd, timeout in self.h.polls))
        self.pending.close()
        row = self.read_task(stat('S'), stat('S'))
        self.assertEqual(row['observation'], 'EXITING_IO_UNAVAILABLE')
        self.assertTrue(row['terminal_task_state']['not_terminal'])

    def test_nonexiting_live_flags_never_admit(self):
        for first, after in [(0, 0), (4, 0), (0, 4)]:
            with self.subTest(first=first, after=after):
                with self.assertRaises(PermissionError):
                    self.read_task(stat(flags=first), stat(flags=after))
                self.assertFalse(self.pending.entries)

    def test_nonzero_vsize_on_either_read_never_admits(self):
        for first, after in [(4096, 0), (0, 4096), (4096, 4096)]:
            with self.subTest(first=first, after=after):
                with self.assertRaises(PermissionError):
                    self.read_task(stat(vsize=first), stat(vsize=after))
                self.assertFalse(self.pending.entries)

    def test_vm_fields_kthread_or_missing_kthread_never_admit(self):
        for status in ['Kthread:\t0\nVmSize:\t0 kB\n', 'Kthread:\t1\n',
                       'Name:\tfixture\n', 'Kthread:\t0\nVmRSS:\t0 kB\n']:
            with self.subTest(status=status):
                self.h.status = status
                with self.assertRaises(PermissionError):
                    self.read_task()
                self.assertFalse(self.pending.entries)

    def test_non_io_and_non_eacces_remain_fatal(self):
        for field, code in [('status', errno.EACCES), ('schedstat', errno.EACCES),
                            ('io', errno.EIO), ('io', errno.EPERM)]:
            with self.subTest(field=field, code=code):
                self.h.error_field, self.h.error_errno = field, code
                with self.assertRaises(OSError) as raised:
                    self.read_task()
                self.assertEqual(raised.exception.errno, code)
                self.assertFalse(self.pending.entries)

    def test_manager_independently_rejects_forged_admission(self):
        for mutation in ('live_binding', 'initial_flag', 'recheck_flag', 'errno',
                         'file', 'identity', 'raw_identity', 'error_identity', 'binding_ready'):
            with self.subTest(mutation=mutation):
                state = pending_state()
                if mutation == 'live_binding': state['live_pidfd_bound'] = False
                if mutation == 'initial_flag': state['partial_read']['stat'] = stat(flags=0)
                if mutation == 'recheck_flag': state['rechecks'][0]['stat'] = stat(flags=0)
                if mutation == 'errno': state['original_error']['errno'] = errno.EIO
                if mutation == 'file': state['original_error']['relative_file'] = 'status'
                if mutation == 'identity': state['rechecks'][0]['identity']['start_ticks'] = 43
                if mutation == 'raw_identity':
                    state['partial_read']['stat'] = stat(ticks=43)
                    state['rechecks'][0]['stat'] = stat(ticks=43)
                if mutation == 'error_identity': state['original_error']['expected_start_ticks'] = 43
                if mutation == 'binding_ready': state['pidfd_binding']['ready'] = True
                with self.assertRaises((OSError, ValueError, telemetry.EnvironmentAbort)):
                    self.admit(state)
                self.assertFalse(self.pending.entries)

    def test_duplicate_entry_does_not_dup_or_extend_deadline(self):
        self.admit()
        before = copy.deepcopy(self.pending.snapshot())
        self.h.now += 1_000_000_000
        with self.assertRaises(telemetry.EnvironmentAbort):
            self.admit()
        row = self.pending.row(999, 42)
        self.assertTrue(row['repeated_pending_record'])
        self.assertTrue(row['not_terminal'])
        self.assertIsNone(self.pending.row(999, 43))
        self.assertEqual(len(self.pending.entries), 1)
        self.assertEqual(self.h.duplicated, [77, 98])
        self.h.now = 6_000_000_001
        with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
            self.pending.resolve(self.counts)
        self.assertTrue(before)

    def test_capacity_is_sixteen_and_distinct_starttime_is_distinct_key(self):
        for i in range(16):
            self.assertEqual(self.admit(ticks=42 + i), '999:' + str(42 + i))
        self.assertEqual(len(self.pending.entries), 16)
        with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
            self.admit(ticks=58)
        self.assertEqual(len(self.pending.entries), 16)

    def test_only_owned_duplicate_fds_close_exactly_once(self):
        self.admit()
        self.assertEqual(self.h.duplicated, [77, 98])
        self.pending.close()
        self.pending.close()
        self.assertEqual(Counter(self.h.closed), Counter({1001: 1, 1002: 1}))
        self.assertNotIn(77, self.h.closed)
        self.assertNotIn(98, self.h.closed)

    def test_partial_dup_failure_closes_first_duplicate(self):
        self.h.dup_error_at = 2
        with self.assertRaises(OSError):
            self.admit()
        self.assertFalse(self.pending.entries)
        self.assertEqual(self.h.closed, [1001])

    def test_unready_resolve_stays_pending_and_never_waits(self):
        self.admit()
        self.h.final_raw = stat('S')
        self.assertEqual(self.pending.resolve(self.counts), [])
        self.assertEqual(len(self.pending.entries), 1)
        self.assertTrue(self.h.polls)
        self.assertTrue(all(timeout == 0 for fd, timeout in self.h.polls))
        self.assertFalse(self.h.closed)

    def test_ready_same_identity_terminal_stat_resolves(self):
        for state in ('Z', 'X'):
            with self.subTest(state=state):
                self.admit()
                self.h.ready, self.h.final_raw = True, stat(state)
                events = self.pending.resolve(self.counts)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]['observation'], 'EXIT_CONFIRMED_AFTER_PENDING_IO')
                self.assertTrue(events[0]['io_missing'])
                self.assertFalse(events[0]['cause_of_permission_error_proven'])
                self.assertFalse(self.pending.entries)
                self.assertTrue(json.dumps(events))

    def test_ready_pinned_disappearance_resolves(self):
        for code in (errno.ENOENT, errno.ESRCH):
            with self.subTest(code=code):
                self.admit()
                self.h.ready, self.h.final_error = True, code
                self.assertEqual(len(self.pending.resolve(self.counts)), 1)
                self.assertFalse(self.pending.entries)

    def test_ready_live_or_wrong_identity_or_other_error_fails(self):
        for final in (stat('R'), stat('S'), stat('Z', ticks=43), errno.EACCES, errno.EIO):
            with self.subTest(final=final):
                self.admit()
                self.h.ready = True
                self.h.final_error = final if isinstance(final, int) else None
                self.h.final_raw = final if isinstance(final, str) else stat('Z')
                with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
                    self.pending.resolve(self.counts)
                self.pending.close()

    def test_poll_error_keeps_pending_for_cleanup(self):
        self.admit()
        self.h.poll_error = OSError(errno.EIO, 'bad owned pidfd event')
        with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
            self.pending.resolve(self.counts)
        self.assertEqual(len(self.pending.entries), 1)

    def test_absolute_ttl_before_equal_and_after(self):
        for offset, success in [(-1, True), (0, True), (1, False)]:
            with self.subTest(offset=offset):
                self.h.now = 1_000_000_000
                self.admit()
                self.h.ready, self.h.final_error = True, None
                self.h.final_raw = stat('Z')
                self.h.now += 5_000_000_000 + offset
                if success:
                    self.assertEqual(len(self.pending.resolve(self.counts)), 1)
                else:
                    with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
                        self.pending.resolve(self.counts)
                self.pending.close()

    def test_admission_never_resets_original_error_time(self):
        state = pending_state()
        self.h.now += 4_000_000_000
        self.admit(state)
        self.h.now += 1_000_000_001
        with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
            self.pending.resolve(self.counts)

    def test_final_stat_read_crossing_deadline_fails(self):
        self.admit()
        self.h.ready = True
        self.h.now += 4_999_999_999
        self.h.read_delay = 2
        with self.assertRaises((OSError, telemetry.EnvironmentAbort)):
            self.pending.resolve(self.counts)

    def test_snapshot_is_json_and_does_not_claim_terminal(self):
        self.admit()
        snapshot = self.pending.snapshot()
        self.assertTrue(json.dumps(snapshot))
        self.assertEqual(snapshot['active'], 1)
        self.assertEqual(len(self.pending.entries), 1)

    def test_frozen_failure_replay_admits_but_does_not_invent_exit(self):
        raw = FAILURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FAILURE_SHA)
        source = json.loads(raw)['sampler_failure_task_state']
        self.h.now = source['error_observed_ns']
        self.h.raws = [source['partial_read']['stat'], source['rechecks'][0]['stat']]
        self.h.status = source['partial_read']['status']
        self.h.final_raw = source['rechecks'][0]['stat']
        row = telemetry._read_task(77, self.counts, source['initial_identity']['start_ticks'],
            with_io=True, context=dict(pid=3189205, expected_start_ticks=99613070),
            exit_fd=98, exit_budget=dict(remaining_ns=100_000_000, spent_ns=0),
            pending=self.pending)
        self.assertEqual(row['observation'], 'EXITING_IO_UNAVAILABLE')
        self.assertEqual(self.pending.resolve(self.counts), [])
        self.assertEqual(len(self.pending.entries), 1)
        self.assertTrue(row['terminal_task_state']['not_terminal'])

    def test_invalid_native_poll_masks_are_rejected(self):
        for events in ([[[98, select.POLLERR]], [[98, select.POLLNVAL]],
                        [[98, select.POLLIN | select.POLLOUT]],
                        [[97, select.POLLIN]],
                        [[98, select.POLLIN], [98, select.POLLIN]]]):
            with self.subTest(events=events):
                poller = mock.Mock()
                poller.poll.return_value = events
                with mock.patch.object(telemetry.select, 'poll', return_value=poller):
                    with self.assertRaises(OSError):
                        ORIGINAL_POLL(98, 0)

    def test_close_error_still_attempts_every_owned_descriptor(self):
        self.admit()
        self.admit(pid=1000)
        closed = []
        def close(fd):
            closed.append(fd)
            if fd == 1001:
                raise OSError(errno.EIO, 'first close failed')
        with mock.patch.object(telemetry.os, 'close', side_effect=close):
            with self.assertRaises(telemetry.EnvironmentAbort):
                self.pending.close()
        self.assertEqual(closed, [1001, 1002, 1003, 1004])
        self.assertFalse(self.pending.entries)

    def test_stop_with_pending_is_environment_abort_not_success(self):
        self.admit()
        stop, failures, heartbeat = threading.Event(), [], {}
        stop.set()
        output = self.output_root / 'samples.jsonl'
        with mock.patch.object(telemetry, '_ExitPending', return_value=self.pending), \
                mock.patch.object(telemetry, '_configured', ([], '[]', '{}')), \
                mock.patch.object(Path, 'read_text', return_value=stat()):
            telemetry.sample(output, stop, failures, heartbeat)
        summary = json.loads((self.output_root / 'telemetry-summary.json').read_text())
        self.assertEqual(summary['sampler_status'], 'ENV_ABORT')
        self.assertEqual(summary['exit_pending']['active'], 1)
        self.assertEqual(summary['exit_pending']['resolved'], 0)
        self.assertTrue(failures)
        self.assertNotIn('last_sample', heartbeat)
        self.assertFalse(self.pending.entries)

    def test_source_failure_is_not_hidden_by_exit_pending(self):
        self.admit()
        stop, failures, heartbeat = threading.Event(), [], {}
        output = self.output_root / 'samples.jsonl'
        with mock.patch.object(telemetry, '_ExitPending', return_value=self.pending), \
                mock.patch.object(telemetry, '_configured',
                                  ([('frozen_source', '/no-live-source-read')], '[]', '{}')), \
                mock.patch.object(Path, 'read_text', side_effect=[stat(),
                    PermissionError(errno.EACCES, 'mock stable source failure')]), \
                mock.patch.object(telemetry.gpu_exclusive, 'processes') as scan:
            telemetry.sample(output, stop, failures, heartbeat)
            scan.assert_not_called()
        summary = json.loads((self.output_root / 'telemetry-summary.json').read_text())
        self.assertEqual(summary['sampler_status'], 'ENV_ABORT')
        self.assertIn('frozen source frozen_source', summary['sampler_failure'])
        self.assertEqual(summary['exit_pending']['active'], 1)
        self.assertFalse(self.pending.entries)
        self.assertNotIn('last_sample', heartbeat)

    def test_separate_identity_deadlines_and_resolution_counters(self):
        self.admit()
        self.h.now += 1_000_000_000
        self.admit(pid=1000, ticks=43)
        snapshot = self.pending.snapshot()
        self.assertEqual(snapshot['admitted'], 2)
        self.assertEqual(snapshot['resolved'], 0)
        self.assertEqual(snapshot['maximum_active'], 2)
        deadlines = {row['pending_id']: row['deadline_ns'] for row in snapshot['entries']}
        self.assertEqual(deadlines, {'999:42': 6_000_000_000, '1000:43': 7_000_000_000})
        self.h.now = 6_000_000_001
        with self.assertRaises(telemetry.EnvironmentAbort):
            self.pending.resolve(self.counts)
        self.assertEqual(len(self.pending.entries), 2)


if __name__ == '__main__':
    unittest.main()
