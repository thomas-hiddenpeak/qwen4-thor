"""CPU-only contracts using isolated proc/cgroup fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    'cgroup_observer', HERE / 'cgroup_observer.py')
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def stat_text(pid=123, ticks=456):
    fields = ['S'] + ['0'] * 18 + [str(ticks)] + ['0'] * 3
    return f'{pid} (worker (cache) fixture) ' + ' '.join(fields) + '\n'


class CgroupObserverContracts(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.q4t-work/startup-cache-20261021/observer-host'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proc = self.base / 'proc'
        self.groups = self.base / 'cgroup'
        self.process = self.proc / '123'
        self.process.mkdir(parents=True)
        (self.proc / 'self').mkdir()
        (self.proc / 'self/mountinfo').write_text(
            f'1 0 0:1 / {self.groups} rw - cgroup2 cgroup2 rw\n')
        (self.process / 'stat').write_text(stat_text())
        (self.process / 'cgroup').write_text('0::/user.slice/worker.scope\n')
        self.leaf = self.groups / 'user.slice/worker.scope'
        for group in (self.groups, self.groups / 'user.slice', self.leaf):
            group.mkdir(parents=True, exist_ok=True)
            for name in observer.MEMORY_FILES:
                content = ('anon 64\nfile 32\n' if name == 'memory.stat' else
                           'low 0\nhigh 2\nmax 3\noom 0\noom_kill 0\n'
                           if name == 'memory.events' else
                           'max\n' if name.endswith(('.max', '.high'))
                           else '96\n')
                (group / name).write_text(content)

    def observe(self, **kwargs):
        return observer.observe_process(123, proc_root=self.proc,
                                        cgroup_root=self.groups, **kwargs)

    def test_current_and_all_visible_ancestors_keep_raw_and_parsed_values(self):
        result = self.observe(expected_start_ticks=456)
        self.assertEqual(result['status'], 'OBSERVED')
        self.assertTrue(result['identity_valid'])
        self.assertEqual(result['start_ticks'], 456)
        self.assertEqual([row['cgroup'] for row in result['ancestors']],
                         ['/user.slice/worker.scope', '/user.slice', '/'])
        files = result['ancestors'][0]['files']
        self.assertEqual(files['memory.max']['value'], 'max')
        self.assertEqual(files['memory.current']['value'], 96)
        self.assertEqual(files['memory.events']['value']['max'], 3)
        self.assertEqual(files['memory.stat']['raw'], 'anon 64\nfile 32\n')
        self.assertLessEqual(result['started']['monotonic'],
                             result['finished']['monotonic'])

    def test_missing_root_interfaces_are_unknown_not_zero(self):
        (self.groups / 'memory.max').unlink()
        result = self.observe()
        self.assertTrue(result['identity_valid'])
        self.assertFalse(result['data_complete'])
        self.assertEqual(result['status'], 'PARTIAL_UNKNOWN')
        root = result['ancestors'][-1]
        self.assertEqual(root['files']['memory.max']['status'],
                         'UNKNOWN_NOT_FOUND')
        self.assertIsNone(root['files']['memory.max']['value'])
        self.assertEqual(
            result['ancestors'][0]['files']['memory.current']['value'], 96)

    def test_disappeared_process_is_not_an_empty_healthy_cgroup(self):
        (self.process / 'stat').unlink()
        (self.process / 'cgroup').unlink()
        self.process.rmdir()
        result = self.observe()
        self.assertFalse(result['identity_valid'])
        self.assertFalse(result['data_complete'])
        self.assertEqual(result['ancestors'], [])
        self.assertEqual(result['process_error']['status'], 'UNKNOWN_NOT_FOUND')

    def test_wrong_expected_identity_does_not_read_hierarchy(self):
        with patch.object(observer, 'group_snapshot') as sample:
            result = self.observe(expected_start_ticks=999)
        sample.assert_not_called()
        self.assertFalse(result['identity_valid'])

    def test_pid_reuse_invalidates_retained_partial_data(self):
        original = observer.read_at
        count = 0

        def read(directory, name):
            nonlocal count
            value = original(directory, name)
            if name == 'stat':
                count += 1
                if count == 2:
                    value['raw'] = stat_text(ticks=457)
            return value

        with patch.object(observer, 'read_at', side_effect=read):
            result = self.observe()
        self.assertFalse(result['identity_valid'])
        self.assertEqual(len(result['ancestors']), 3)
        self.assertEqual(result['stat_after']['value'], 457)

    def test_migration_during_snapshot_is_explicitly_invalid(self):
        original = observer.read_at
        count = 0

        def read(directory, name):
            nonlocal count
            value = original(directory, name)
            if name == 'cgroup':
                count += 1
                if count == 2:
                    value['raw'] = '0::/another.scope\n'
            return value

        with patch.object(observer, 'read_at', side_effect=read):
            result = self.observe()
        self.assertFalse(result['identity_valid'])
        self.assertEqual(result['cgroup_after']['value'], '/another.scope')
        self.assertEqual(len(result['ancestors']), 3)

    def test_unsafe_or_ambiguous_membership_never_reads_groups(self):
        for text in ('0::/../escape\n', '0::/user.slice/./worker.scope\n',
                     '0:://user.slice\n', '0::relative\n',
                     '0::/one\n0::/two\n', '1:memory:/worker\n'):
            (self.process / 'cgroup').write_text(text)
            with patch.object(observer, 'group_snapshot') as sample:
                result = self.observe()
            sample.assert_not_called()
            self.assertFalse(result['identity_valid'])
            self.assertEqual(result['cgroup_before']['status'],
                             'UNKNOWN_UNSAFE_MEMBERSHIP')

    def test_group_symlink_is_not_followed_outside_hierarchy(self):
        outside = self.base / 'private'
        outside.mkdir()
        (self.groups / 'escape').symlink_to(outside, target_is_directory=True)
        (self.process / 'cgroup').write_text('0::/escape\n')
        result = self.observe()
        self.assertFalse(result['data_complete'])
        self.assertEqual(result['ancestors'][0]['status'], 'UNKNOWN_GROUP')
        self.assertEqual(result['ancestors'][0]['files'], {})
        self.assertEqual(result['ancestors'][-1]['cgroup'], '/')

    def test_control_file_symlink_is_not_followed(self):
        private = self.base / 'secret'
        private.write_text('do not read')
        (self.leaf / 'memory.max').unlink()
        (self.leaf / 'memory.max').symlink_to(private)
        result = self.observe()
        value = result['ancestors'][0]['files']['memory.max']
        self.assertEqual(value['status'], 'UNKNOWN_UNSAFE_PATH')
        self.assertIsNone(value['raw'])
        self.assertIsNone(value['value'])

    def test_permission_failure_preserves_other_values(self):
        original = observer.read_at

        def read(directory, name):
            if name == 'memory.max':
                return observer.failure(PermissionError(13, 'fixture denied'))
            return original(directory, name)

        with patch.object(observer, 'read_at', side_effect=read):
            result = self.observe()
        files = result['ancestors'][0]['files']
        self.assertEqual(files['memory.max']['status'], 'UNKNOWN_PERMISSION')
        self.assertIsNone(files['memory.max']['value'])
        self.assertEqual(files['memory.stat']['value']['anon'], 64)
        self.assertFalse(result['data_complete'])

    def test_group_removed_mid_read_preserves_completed_values(self):
        original = observer.read_at
        leaf_inode = self.leaf.stat().st_ino

        def read(directory, name):
            value = original(directory, name)
            if (name == 'memory.stat' and
                    os.fstat(directory).st_ino == leaf_inode):
                for path in self.leaf.iterdir():
                    path.unlink()
                self.leaf.rmdir()
            return value

        with patch.object(observer, 'read_at', side_effect=read):
            result = self.observe()
        self.assertEqual(result['ancestors'][0]['status'],
                         'UNKNOWN_GROUP_DISAPPEARED')
        files = result['ancestors'][0]['files']
        self.assertEqual(files['memory.stat']['value']['file'], 32)
        self.assertFalse(result['data_complete'])

    def test_invalid_counter_is_not_converted_to_zero(self):
        for text in ('oom not-a-number\n', 'oom -1\n', 'oom 0\noom 1\n', ''):
            (self.leaf / 'memory.events').write_text(text)
            value = self.observe()['ancestors'][0]['files']['memory.events']
            self.assertEqual(value['status'], 'UNKNOWN_INVALID_VALUE')
            self.assertIsNone(value['value'])
            self.assertEqual(value['raw'], text)

    def test_unmapped_mount_does_not_guess_hierarchy(self):
        (self.proc / 'self/mountinfo').write_text(
            f'1 0 0:1 /hidden {self.groups} rw - cgroup2 cgroup2 rw\n')
        result = self.observe()
        self.assertTrue(result['identity_valid'])
        self.assertEqual(result['ancestors'], [])
        self.assertEqual(result['hierarchy_error']['status'],
                         'UNKNOWN_MOUNT_MAPPING')
        self.assertFalse(result['data_complete'])

    def test_invalid_stat_and_input_are_rejected(self):
        (self.process / 'stat').write_text('123 (bad) S\n')
        result = self.observe()
        self.assertFalse(result['identity_valid'])
        self.assertEqual(result['stat_before']['status'],
                         'UNKNOWN_INVALID_STAT')
        for pid in (0, -1, True, '123'):
            with self.assertRaises(ValueError):
                observer.observe_process(pid)

    def test_cli_retains_partial_status_and_refuses_existing_output(self):
        output = self.base / 'snapshot.json'
        sample = dict(identity_valid=True, data_complete=False,
                      status='PARTIAL_UNKNOWN')
        command = ['observer', '--pid', '123', '--expected-start-ticks',
                   '456', '--output', str(output)]
        with patch.object(sys, 'argv', command), \
                patch.object(observer, 'observe_process', return_value=sample):
            self.assertEqual(observer.main(), 2)
            self.assertEqual(json.loads(output.read_text()), sample)
            with self.assertRaises(FileExistsError):
                observer.main()
        self.assertEqual(json.loads(output.read_text()), sample)


if __name__ == '__main__':
    unittest.main()
