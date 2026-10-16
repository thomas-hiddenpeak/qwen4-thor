"""CPU-only lease, fail-closed monitoring, and ownership contracts."""
import fcntl
import json
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

    def test_command_failure_is_not_environment_failure(self):
        with patch.object(guard, 'inspect', return_value={'conflicts': []}):
            self.assertEqual(guard.run_guard([sys.executable, '-c', 'exit(7)'],
                                            self.out, self.lock, interval=0.01, grace=1), 1)
        self.assertEqual(self.outcome()['status'], 'COMMAND_FAILED')
        self.assertEqual(self.outcome()['child_returncode'], 7)


if __name__ == '__main__':
    unittest.main()
