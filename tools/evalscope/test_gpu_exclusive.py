"""CPU-only lease, fail-closed monitoring, and ownership contracts."""
import fcntl
import json
import signal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import gpu_exclusive as guard

WORK = guard.ROOT / '.q4t-work/gpu-exclusive-tests'


class ExclusiveTests(unittest.TestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'run'
        self.lock = self.root / 'lease'

    def outcome(self):
        return json.loads((self.out / 'exit.json').read_text())

    def test_external_lease_survives_guard_success_and_failure(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), self.lock.open('w+') as held:
                self.out = self.root / f'run-{exit_code}'
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(guard, 'inspect', return_value={'conflicts': []}):
                    code = guard.run_guard(
                        [sys.executable, '-c', f'exit({exit_code})'],
                        self.out, self.lock, interval=0.01, grace=1,
                        lease_fd=held.fileno())
                self.assertEqual(code, 0 if exit_code == 0 else 1)
                self.assertTrue(self.outcome()['external_lease'])
                self.assertEqual(json.loads(self.lock.read_text())['state'],
                                 'external_cleanup_pending')
                with self.lock.open('r+') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                events = (self.out / 'events.jsonl').read_text()
                self.assertIn('lease_guard_finished', events)
                self.assertNotIn('lease_released', events)
            with self.lock.open('r+') as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_external_invalid_leases_never_start(self):
        for mode in ('unlocked', 'shared', 'wrong_inode', 'different_fd'):
            with self.subTest(mode=mode), self.lock.open('w+') as held:
                self.out = self.root / mode
                held.write('original holder')
                held.flush()
                if mode != 'unlocked':
                    fcntl.flock(held, fcntl.LOCK_SH if mode == 'shared' else
                                fcntl.LOCK_EX)
                path = self.root / 'other' if mode == 'wrong_inode' else self.lock
                with path.open('a+') as other:
                    fd = other.fileno() if mode in ('wrong_inode', 'different_fd') else held.fileno()
                    with patch.object(guard, 'inspect') as scan:
                        self.assertEqual(guard.run_guard(
                            ['must-not-run'], self.out, self.lock, lease_fd=fd), 75)
                        scan.assert_not_called()
                self.assertEqual(self.outcome()['status'], 'ENV_ABORT')
                self.assertEqual(self.lock.read_text(), 'original holder')

    def test_external_lease_survives_cleanup_error(self):
        with self.lock.open('w+') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with patch.object(guard, 'inspect', return_value={'conflicts': []}), \
                    patch.object(guard, 'stop_owned', side_effect=RuntimeError('cleanup')):
                code = guard.run_guard([sys.executable, '-c', 'exit(0)'],
                                       self.out, self.lock, interval=0.01,
                                       lease_fd=held.fileno())
            self.assertEqual(code, 75)
            self.assertEqual(self.outcome()['status'], 'ENV_ABORT')
            with self.lock.open('r+') as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_busy_lease_does_not_start_command_or_rewrite_holder(self):
        with self.lock.open('w+') as held:
            held.write('held by another session')
            held.flush()
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(guard, 'inspect') as scan:
                code = guard.run_guard(['must-not-run'], self.out, self.lock)
                scan.assert_not_called()
            self.assertEqual(code, 75)
            self.assertEqual(self.lock.read_text(), 'held by another session')
            self.assertEqual(self.outcome()['status'], 'LEASE_BUSY')

    def test_preflight_conflict_never_launches(self):
        with patch.object(guard, 'inspect', return_value={'conflicts': [{'pid': 1}]}), \
                patch.object(guard.subprocess, 'Popen') as launch:
            self.assertEqual(guard.run_guard(['no'], self.out, self.lock), 75)
            launch.assert_not_called()
        self.assertEqual(self.outcome()['status'], 'ENV_ABORT')

    def test_query_failure_is_not_idle(self):
        with patch.object(guard, 'inspect', side_effect=RuntimeError('query failed')):
            self.assertEqual(guard.run_guard(['no'], self.out, self.lock), 75)
        self.assertIn('query failed', self.outcome()['reason'])

    def test_owned_group_excluded_unknown_compute_process_detected(self):
        records = {10: {'pgid': 5, 'argv': ['q4t', 'serve'], 'executable': '/x/q4t'},
                   20: {'pgid': 20, 'argv': ['python'], 'executable': '/x/python'}}
        with patch.object(guard, 'processes', return_value=records), \
                patch.object(guard, 'nvidia_pids', return_value={10, 20}):
            self.assertEqual([x['pid'] for x in guard.inspect(5)['conflicts']], [20])

    def test_owned_exit_between_driver_and_proc_queries(self):
        history = {}
        owned = {10: {'pgid': 5, 'start_ticks': 100}}
        with patch.object(guard, 'processes', side_effect=[owned, {}, {}]), \
                patch.object(guard, 'nvidia_pids', side_effect=[{10}, {10}, set()]):
            self.assertEqual(guard.inspect(5, history)['conflicts'], [])
            result = guard.inspect(5, history)
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(result['retired_owned_contexts'][0]['identity']['start_ticks'], 100)

    def test_exit_recheck_does_not_hide_reused_pid_or_other_context(self):
        history = {10: {'pgid': 5, 'start_ticks': 100}}
        reused = {10: {'pgid': 20, 'start_ticks': 200}}
        with patch.object(guard, 'processes', side_effect=[{}, reused]), \
                patch.object(guard, 'nvidia_pids', side_effect=[{10}, {10, 30}]):
            result = guard.inspect(5, history)
        self.assertEqual({r['pid'] for r in result['conflicts']}, {10, 30})
        self.assertEqual(result['retired_owned_contexts'], [])

    def test_unknown_missing_pid_and_unreadable_owned_history_fail_closed(self):
        for records, history in [({}, {}), ({10: {'unreadable': True}},
                                           {10: {'pgid': 5, 'start_ticks': 100}})]:
            with patch.object(guard, 'processes', return_value=records), \
                    patch.object(guard, 'nvidia_pids', return_value={10}):
                self.assertEqual(guard.inspect(5, history)['conflicts'][0]['pid'], 10)

    def test_owned_stale_context_still_present_or_requery_error_fails_closed(self):
        history = {10: {'pgid': 5, 'start_ticks': 100}}
        with patch.object(guard, 'processes', return_value={}), \
                patch.object(guard, 'nvidia_pids', return_value={10}):
            self.assertEqual(guard.inspect(5, history)['conflicts'][0]['pid'], 10)
        with patch.object(guard, 'processes', return_value={}), \
                patch.object(guard, 'nvidia_pids', side_effect=[{10}, RuntimeError('query')]):
            with self.assertRaises(RuntimeError):
                guard.inspect(5, history)

    def test_stat_identity_survives_missing_executable(self):
        proc = self.root / 'proc'
        task = proc / '10'
        task.mkdir(parents=True)
        # Fields after comm: state, ppid, pgrp, ... starttime (index 19).
        fields = ['Z', '1', '5'] + ['0'] * 16 + ['123']
        (task / 'stat').write_text('10 (exiting worker) ' + ' '.join(fields))
        records = guard.processes(proc)
        self.assertEqual(records[10]['pgid'], 5)
        self.assertEqual(records[10]['start_ticks'], 123)
        self.assertTrue(records[10]['metadata_missing'])
        with patch.object(guard, 'processes', return_value=records), \
                patch.object(guard, 'nvidia_pids', return_value={10}):
            self.assertEqual(guard.inspect(5)['conflicts'], [])
            self.assertEqual(guard.inspect(6)['conflicts'][0]['pid'], 10)

    def test_real_owned_zombie_not_misclassified_as_foreign(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'],
                                 start_new_session=True)
        self.addCleanup(child.wait)
        deadline = time.monotonic() + 3
        while True:
            stat = Path(f'/proc/{child.pid}/stat').read_text()
            if stat.rsplit(')', 1)[1].split()[0] == 'Z':
                break
            if time.monotonic() >= deadline:
                self.fail('child did not become zombie')
            time.sleep(0.005)
        # Do not poll/wait before inspection: these would reap the zombie.
        self.assertFalse(Path(f'/proc/{child.pid}/exe').exists())
        with patch.object(guard, 'nvidia_pids', return_value={child.pid}):
            self.assertEqual(guard.inspect(child.pid)['conflicts'], [])
        self.assertEqual(guard.processes()[child.pid]['state'], 'Z')

    def test_detached_descendant_and_reparented_identity_are_owned(self):
        history = {}
        records = {10: {'pgid': 10, 'ppid': 1, 'start_ticks': 100},
                   11: {'pgid': 10, 'ppid': 10, 'start_ticks': 101},
                   12: {'pgid': 12, 'ppid': 11, 'start_ticks': 102}}
        with patch.object(guard, 'processes', return_value=records), \
                patch.object(guard, 'nvidia_pids', return_value={12}):
            self.assertEqual(guard.inspect(10, history)['conflicts'], [])
        records = {12: {'pgid': 12, 'ppid': 1, 'start_ticks': 102},
                   99: {'pgid': 99, 'ppid': 1, 'start_ticks': 90}}
        with patch.object(guard, 'processes', return_value=records), \
                patch.object(guard, 'nvidia_pids', return_value={12, 99}):
            self.assertEqual([r['pid'] for r in guard.inspect(10, history)['conflicts']], [99])

    def test_reused_detached_pid_is_not_owned(self):
        history = {12: {'pgid': 12, 'ppid': 11, 'start_ticks': 102}}
        with patch.object(guard, 'processes', return_value={
                12: {'pgid': 12, 'ppid': 1, 'start_ticks': 200}}), \
                patch.object(guard, 'nvidia_pids', return_value={12}):
            self.assertEqual(guard.inspect(10, history)['conflicts'][0]['pid'], 12)

    def test_child_older_than_parent_cannot_prove_ancestry(self):
        records = {10: {'pgid': 10, 'ppid': 1, 'start_ticks': 100},
                   12: {'pgid': 12, 'ppid': 10, 'start_ticks': 90}}
        with patch.object(guard, 'processes', return_value=records), \
                patch.object(guard, 'nvidia_pids', return_value={12}):
            self.assertEqual(guard.inspect(10)['conflicts'][0]['pid'], 12)

    def test_real_detached_child_cleanup_preserves_unrelated_process(self):
        ready = self.root / 'detached-pid'
        target = ("import os,signal,time; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)")
        parent_code = ("import subprocess,sys,time; "
                       f"subprocess.Popen([sys.executable,'-c',{target!r}],start_new_session=True); "
                       "time.sleep(30)")
        parent = subprocess.Popen([sys.executable, '-c', parent_code],
                                  start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        history = {}
        try:
            deadline = time.monotonic() + 3
            while not ready.exists():
                if time.monotonic() > deadline:
                    self.fail('detached child did not become ready')
                time.sleep(.01)
            pid = int(ready.read_text())
            with patch.object(guard, 'nvidia_pids', return_value={pid}):
                self.assertEqual(guard.inspect(parent.pid, history)['conflicts'], [])
            self.assertIn(pid, history)
            self.assertNotEqual(history[pid]['pgid'], parent.pid)
            guard.stop_owned(parent, .3, history)
            record = guard.processes().get(pid)
            self.assertTrue(record is None or record['state'] == 'Z')
            self.assertIsNone(unrelated.poll())
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()
            for pid, identity in history.items():
                guard.signal_identity(pid, identity, signal.SIGKILL)
            unrelated.terminate(); unrelated.wait()

    def test_argument_text_is_not_an_executable(self):
        self.assertFalse(guard.gpu_command(
            {'executable': '/bin/bash', 'argv': ['bash', '-c', 'q4t serve']}))
        self.assertTrue(guard.gpu_command(
            {'executable': '/x/q4t-main', 'argv': ['q4t-main', 'serve']}))

    def test_cpu_command_completes_and_releases_lease(self):
        with patch.object(guard, 'inspect', return_value={'conflicts': []}):
            code = guard.run_guard([sys.executable, '-c', 'pass'], self.out,
                                   self.lock, interval=0.01, grace=1)
        self.assertEqual(code, 0)
        self.assertEqual(self.outcome()['status'], 'COMPLETED')
        self.assertFalse(self.outcome()['performance_acceptance'])
        with self.lock.open() as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_runtime_conflict_stops_only_owned_child(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()))
        snapshots = iter([{'conflicts': []}, {'conflicts': [{'pid': unrelated.pid}]}])
        with patch.object(guard, 'inspect', side_effect=lambda *args: next(snapshots)):
            code = guard.run_guard([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   self.out, self.lock, interval=0.01, grace=1)
        self.assertEqual(code, 75)
        self.assertIsNone(unrelated.poll())
        self.assertEqual(self.outcome()['status'], 'ENV_ABORT')
        self.assertIsNotNone(self.outcome()['child_returncode'])

    def test_cleanup_failure_propagates_nonzero_exit(self):
        with patch.object(guard, 'inspect', return_value={'conflicts': []}), \
                patch.object(guard, 'stop_owned', side_effect=RuntimeError('cleanup failed')):
            result = guard.run_guard([sys.executable, '-c', 'pass'],
                                     self.out, self.lock, interval=.01, grace=1)
        self.assertEqual(result, 75)
        self.assertEqual(self.outcome()['status'], 'ENV_ABORT')
        self.assertIn('cleanup failed', self.outcome()['cleanup_error'])

    def test_command_failure_is_not_environment_failure(self):
        with patch.object(guard, 'inspect', return_value={'conflicts': []}):
            self.assertEqual(guard.run_guard([sys.executable, '-c', 'exit(7)'],
                                            self.out, self.lock, interval=0.01, grace=1), 1)
        self.assertEqual(self.outcome()['status'], 'COMMAND_FAILED')
        self.assertEqual(self.outcome()['child_returncode'], 7)


if __name__ == '__main__':
    unittest.main()
