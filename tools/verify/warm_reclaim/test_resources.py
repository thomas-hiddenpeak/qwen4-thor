"""Host-only observer contracts; no GPU, NVML, or real cache observations."""
import argparse
import errno
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'warm_resources', Path(__file__).with_name('resources.py'))
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)


class ObserverTests(unittest.TestCase):
    def test_psi_waiver_requires_explicit_running_kernel_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.gz'
            with gzip.open(config, 'wt') as target:
                target.write('# CONFIG_PSI is not set\n')
            unsupported = resources.psi_support(config)
            self.assertIs(unsupported['supported'], False)
            missing = dict(value=None, errno=errno.ENOENT, error='missing')
            classified = resources.classify_pressure(missing, unsupported)
            self.assertEqual(classified['status'], 'unsupported')
            self.assertEqual(classified['error'], 'missing')
            for support in (None, {'supported': None}, {'supported': True}):
                self.assertNotIn('status',
                                 resources.classify_pressure(missing, support))
            denied = dict(value=None, errno=errno.EACCES, error='denied')
            self.assertNotIn('status',
                             resources.classify_pressure(denied, unsupported))
            with gzip.open(config, 'wt') as target:
                target.write('CONFIG_PSI=y\n')
            self.assertIs(resources.psi_support(config)['supported'], True)
            config.unlink()
            self.assertIsNone(resources.psi_support(config)['supported'])

    def test_stat_comm_and_identity(self):
        # State is field 3; process-start ticks are field 22.
        value = '91 (a tricky ) name) D ' + ' '.join(
            ['0'] * 18 + ['12345', '0'])
        self.assertEqual(resources.parse_stat(value),
                         dict(state='D', starttime_ticks=12345))

    def test_cgroup_path_confines_to_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(resources.cgroup_path('0::/owned.slice/x\n', root),
                             root / 'owned.slice/x')
            for value in ('1:memory:/foo', '0::relative', '0::/../escape',
                          '0::/foo\n0::/bar'):
                with self.assertRaises(ValueError):
                    resources.cgroup_path(value, root)
            (root / 'outside').symlink_to('/proc')
            with self.assertRaises(ValueError):
                resources.cgroup_path('0::/outside/1', root)

    def test_evidence_exclusive_and_budget_preserves_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            evidence = resources.Evidence(path, resources.FINAL_RESERVE + 64)
            evidence.write('resources.jsonl', {'value': 1}, stream=True)
            with self.assertRaises(OverflowError):
                evidence.write('cache/000000.json', {'large': 'x' * 100})
            self.assertFalse((path / 'cache/000000.json').exists())
            evidence.write('exit.json', {'truncated': True}, final=True)
            evidence.close()
            with self.assertRaises(FileExistsError):
                resources.Evidence(path)
            self.assertEqual(json.loads((path / 'exit.json').read_text()),
                             {'truncated': True})

    def test_cgroup_removed_is_retained_but_live_read_failure_is_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'owned.scope'
            groups = {'scope': dict(path=path, first_seen={}, owners=[{'pid': 9}])}
            rows, failures = resources.group_snapshot(groups)
            self.assertEqual(rows[0]['status'], 'removed')
            self.assertEqual(failures, [])
            path.mkdir()
            rows, failures = resources.group_snapshot(groups)
            self.assertEqual(rows[0]['status'], 'read_error')
            self.assertEqual(len(failures), 1)
            for name in resources.CGROUP_FILES:
                (path / name).write_text('0\n')
            rows, failures = resources.group_snapshot(groups)
            self.assertEqual(rows[0]['status'], 'observed')
            self.assertEqual(failures, [])
            (path / 'memory.pressure').unlink()
            rows, failures = resources.group_snapshot(groups, {'supported': False})
            self.assertEqual(failures, [])
            self.assertEqual(rows[0]['files']['memory.pressure']['status'],
                             'unsupported')
            (path / 'memory.current').unlink()
            _, failures = resources.group_snapshot(groups, {'supported': False})
            self.assertEqual(len(failures), 1)

    def test_target_selection_and_cgroup_tracking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'q4t'
            binary.write_text('binary')
            other = root / 'python'
            other.write_text('other')
            proc_root = root / 'proc'
            proc_root.mkdir()
            cgroup_root = root / 'cgroups'
            cgroup_root.mkdir()
            for pid, model, worker in ((10, True, False), (20, False, True),
                                       (30, False, False)):
                proc = proc_root / str(pid)
                (proc / 'task').mkdir(parents=True)
                (proc / 'exe').symlink_to(binary if model else other)
                args = (['/usr/bin/python', '/tools/nvml_process_query.py',
                         '--worker', 'id'] if worker else ['/unused'])
                (proc / 'cmdline').write_text('\0'.join(args) + '\0')
                (proc / 'cgroup').write_text('0::/owned.scope\n')
                for tid, state in ((pid, 'S'), (pid + 1, 'D'), (pid + 2, 'S')):
                    task = proc / 'task' / str(tid)
                    task.mkdir()
                    stat = '{} (target) {} '.format(tid, state)
                    stat += ' '.join(['0'] * 18 + ['12345', '0'])
                    (task / 'stat').write_text(stat)
                    (task / 'wchan').write_text('test_wchan')
                    (task / 'stack').write_text('test_stack')
            identity = binary.stat()
            groups = {}
            targets, failures = resources.target_snapshot(
                proc_root, (identity.st_dev, identity.st_ino), groups,
                cgroup_root)
            self.assertEqual(failures, [])
            self.assertEqual({r['pid'] for r in targets}, {10, 20})
            for row in targets:
                self.assertEqual({t['tid'] for t in row['tasks']},
                                 {row['pid'], row['pid'] + 1})
            self.assertEqual(len(groups), 1)
            group = next(iter(groups.values()))
            self.assertEqual(group['owners'],
                             [dict(pid=10, starttime_ticks=12345)])

    def run_observer(self, folder, *, fail_read=False, deadline=False):
        folder = Path(folder)
        binary = folder / 'binary'
        binary.write_text('test')
        args = argparse.Namespace(output=folder / 'output',
                                  stop_file=folder / 'stop', binary=binary,
                                  model_dir=folder, max_seconds=360)
        read_result = dict(value='test', error=None, errno=None)
        if fail_read:
            read_result = dict(value=None, error='denied', errno=13)
        clock = [100.0]

        def sleep(duration):
            clock[0] += 361 if deadline else duration
            if not deadline:
                args.stop_file.touch()

        with patch.object(resources.os, 'geteuid', return_value=0), \
                patch.object(resources, 'read_text', return_value=read_result), \
                patch.object(resources, 'psi_support', return_value={'supported': True}), \
                patch.object(resources, 'model_files', return_value=[binary]), \
                patch.object(resources, 'observe_files', return_value=dict(
                    complete_file_set_observed=True, files=[], errors=[])), \
                patch.object(resources, 'target_snapshot', return_value=([], [])), \
                patch.object(resources.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(resources.time, 'sleep', side_effect=sleep):
            result = resources.observe(args)
        final = json.loads((args.output / 'exit.json').read_text())
        return args, result, final

    def test_normal_stop_and_separate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            args, result, final = self.run_observer(folder)
            self.assertEqual(result, 0)
            self.assertEqual(final['status'], 'COMPLETED')
            self.assertEqual(final['reason'], 'stop_file')
            self.assertTrue((args.output / 'ready.json').exists())
            self.assertTrue((args.output / 'cache/000000.json').exists())
            row = json.loads((args.output / 'resources.jsonl').read_text())
            self.assertNotIn('files', row)
            self.assertEqual(final['system_samples'], 1)

    def test_required_read_failure_is_nonzero_and_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            args, result, final = self.run_observer(folder, fail_read=True)
            self.assertEqual(result, 2)
            self.assertEqual(final['status'], 'FAILED')
            self.assertFalse((args.output / 'ready.json').exists())
            row = json.loads((args.output / 'resources.jsonl').read_text())
            self.assertTrue(row['failures'])

    def test_deadline_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            _, result, final = self.run_observer(folder, deadline=True)
            self.assertEqual(result, 2)
            self.assertEqual(final['status'], 'DEADLINE_EXCEEDED')


if __name__ == '__main__':
    unittest.main()
