"""CPU fixtures for resource control/data separation; no real cgroup writes."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_service import package as pkg, resources as r, supervisor as sup
from tools.serve_service import test_service as old


class ResourceControlTests(unittest.TestCase):
    run_supervisor = old.Case.run_supervisor
    observation = old.Case.observation
    fixture = old.PackageTests.fixture

    def setUp(self):
        old.Case.setUp(self)
        self.cg = self.root/'cgroup'
        self.unit = 'q4t-host-'+32*'a'+'.scope'
        self.relative = (f'/user.slice/user-{os.getuid()}.slice/'
                         f'user@{os.getuid()}.service/app.slice/{self.unit}')
        self.scope = self.cg/self.relative.lstrip('/')
        self.control, self.payload = self.scope/'control', self.scope/'payload'
        self.control.mkdir(parents=True)
        self.payload.mkdir()
        self.selected = r.policy(32, 64, 8)
        for name, amount in r.limits(self.selected).items():
            self.put(name, str(amount))
        for name, amount in (('memory.max', self.selected['control_max_bytes']),
                             ('memory.swap.max', 0), ('pids.max', 64)):
            (self.control/name).write_text(str(amount)+'\n')
        self.values = {'memory.current': '1024', 'memory.peak': '2048',
            'memory.swap.current': '0', 'pids.current': '1',
            'memory.events': 'high 0\nmax 0\noom 0\noom_kill 0',
            'pids.events': 'max 0', 'cgroup.events': 'populated 0',
            'cgroup.procs': ''}
        for name, value in self.values.items():
            # cgroup.procs on an empty group is an empty file, not a blank PID.
            (self.payload/name).write_text(value+'\n' if value else '')
        owner = sup.process_identity(os.getpid())
        self.record = dict(unit=self.unit, token='fixture-token', owner=owner,
            policy=self.selected, root=str(self.scope), root_identity=r.inode(self.scope),
            control_identity=r.inode(self.control), payload_identity=r.inode(self.payload),
            invocation_id=32*'b')
        self.props = dict(Id=self.unit, LoadState='loaded', ActiveState='active',
            Description='fixture-token', Delegate='yes', OOMPolicy='continue',
            DelegateControllers='memory pids', InvocationID=32*'b',
            ControlGroup=self.relative)
        for patch in (mock.patch.object(r, 'CGROUP', self.cg),
                      mock.patch.object(r, 'membership', return_value=self.relative+'/control')):
            patch.start()
            self.addCleanup(patch.stop)
        # The setup itself only manufactures files, never a resource envelope.
        self.envelope = object.__new__(r.Envelope)
        self.envelope.output = self.root
        self.envelope.selected = self.selected
        self.envelope.owner = owner
        self.envelope.record = self.record
        self.envelope.error = self.envelope.latest = None
        self.envelope.closed = False
        self.envelope.isolate_manager = True
        self.envelope.statistics_gap_samples = 0

    def put(self, name, value):
        (self.payload/name).write_text(value+'\n')

    def remove_leaf(self, path):
        # Simulate cgroupfs virtual files disappearing with the owned leaf.
        self.assertEqual(path, self.payload)
        for file in path.iterdir():
            file.unlink()
        os.rmdir(path)

    def test_runtime_does_not_query_manager(self):
        with mock.patch.object(r, 'properties', side_effect=OSError('D-Bus down')) as manager:
            for _ in range(4):
                self.envelope.check()
        manager.assert_not_called()
        self.assertIsNone(self.envelope.error)
        self.assertEqual(self.envelope.latest['memory.current'], 1024)

    def test_startup_and_strict_mode_still_require_manager(self):
        with mock.patch.object(r, 'properties', side_effect=OSError('D-Bus down')):
            with self.assertRaises(OSError):
                self.envelope.check(startup=True)
            self.envelope.isolate_manager = False
            with self.assertRaises(OSError):
                self.envelope.check()
        self.assertEqual(self.envelope.error, 'host_resource_observation_unavailable')

    def test_statistics_missing_and_recovery_keep_history(self):
        for name in ('memory.current', 'memory.peak', 'memory.swap.current', 'pids.current'):
            with self.subTest(name=name):
                (self.payload/name).unlink()
                self.envelope.check()
                self.assertIsNone(self.envelope.latest[name])
                self.assertEqual(self.envelope.latest['statistics_errors'],
                                 {name: 'FileNotFoundError'})
                self.put(name, self.values[name])
                self.envelope.check()
                self.assertEqual(self.envelope.latest['statistics_errors'], {})
        self.assertEqual(self.envelope.statistics_gap_samples, 4)
        self.assertIsNone(self.envelope.error)

    def test_statistics_invalid_is_unknown_without_fake_zero_or_last_value(self):
        self.envelope.check()
        self.put('memory.current', 'invalid')
        self.envelope.check()
        self.assertIsNone(self.envelope.latest['memory.current'])
        self.assertEqual(self.envelope.latest['statistics_errors'],
                         {'memory.current': 'RuntimeError'})

    def test_critical_files_missing_never_use_statistics_tolerance(self):
        for name in ('memory.events', 'pids.events', 'cgroup.events', 'cgroup.procs'):
            with self.subTest(name=name):
                (self.payload/name).unlink()
                with self.assertRaises(FileNotFoundError):
                    r.sample(self.record, require_manager=False, tolerate_statistics=True)
                value = self.values[name]
                (self.payload/name).write_text(value+'\n' if value else '')

    def test_oom_and_tasks_remain_terminal_with_missing_statistics(self):
        (self.payload/'memory.peak').unlink()
        for name, value, reason in (
                ('memory.events', 'high 0\nmax 0\noom 1\noom_kill 0', 'host_resource_oom'),
                ('pids.events', 'max 1', 'host_resource_tasks')):
            self.envelope.error = None
            self.put(name, value)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, reason):
                self.envelope.check()
            self.put(name, self.values[name])

    def test_owner_identity_or_membership_change_is_terminal(self):
        self.record['owner'] = dict(self.record['owner'], start_ticks=0)
        with self.assertRaisesRegex(RuntimeError, 'identity/membership'):
            self.envelope.check()
        self.record['owner'] = sup.process_identity(os.getpid())
        with mock.patch.object(r, 'membership', return_value='/another-service'), \
             self.assertRaisesRegex(RuntimeError, 'identity/membership'):
            self.envelope.check()

    def test_replaced_or_symlinked_leaf_is_rejected(self):
        self.payload.rename(self.scope/'old-payload')
        self.payload.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'child replaced'):
            self.envelope.check()
        os.rmdir(self.payload)
        self.payload.symlink_to(self.scope/'old-payload', target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'owned cgroup directory'):
            self.envelope.check()

    def test_payload_and_control_limits_remain_exact(self):
        for group, name in ((self.payload, 'memory.max'), (self.payload, 'pids.max'),
                            (self.control, 'memory.max')):
            path = group/name
            original = path.read_text()
            path.write_text('999\n')
            with self.subTest(group=str(group), name=name), \
                 self.assertRaisesRegex(RuntimeError, 'resource limit drift'):
                self.envelope.check()
            path.write_text(original)

    def test_final_manager_failure_never_confirms_or_removes_leaf(self):
        with mock.patch.object(r, 'properties', side_effect=OSError('D-Bus down')), \
             self.assertRaises(OSError):
            self.envelope.close()
        self.assertFalse(self.envelope.closed)
        self.assertTrue(self.payload.exists())
        self.assertFalse((self.root/'resources-final.json').exists())

    def test_final_statistics_gap_keeps_cleanup_and_history_separate(self):
        (self.payload/'memory.peak').unlink()
        with mock.patch.object(r, 'properties', return_value=self.props), \
             mock.patch.object(Path, 'rmdir', autospec=True, side_effect=self.remove_leaf):
            self.envelope.close()
        value = pkg.read(self.root/'resources-final.json')
        self.assertTrue(value['payload_removed'])
        self.assertTrue(value['scope_cleanup_pending'])
        self.assertEqual(value['statistics_gap_samples'], 1)
        self.assertIsNone(value['final']['memory.peak'])

    def test_foreign_or_populated_group_cannot_be_removed(self):
        for property_name, value in (('Description', 'other-owner'),
                                     ('InvocationID', 32*'c')):
            with mock.patch.object(r, 'properties', return_value=dict(self.props, **{property_name:value})), \
                 self.assertRaises(RuntimeError):
                self.envelope.close()
        self.put('cgroup.events', 'populated 1')
        with mock.patch.object(r, 'properties', return_value=self.props), \
             self.assertRaisesRegex(RuntimeError, 'still populated'):
            self.envelope.close()
        self.assertTrue(self.payload.exists())
        self.assertFalse(self.envelope.closed)

    def test_combined_outage_keeps_real_cpu_backend_progressing(self):
        down = [False]
        def properties(_):
            if down[0]:
                raise OSError('D-Bus unavailable during business')
            return self.props
        def envelope(selected, output, *, isolate_manager=False):
            self.assertTrue(isolate_manager)
            self.envelope.output = output
            self.envelope.check(startup=True)
            down[0] = True
            (self.payload/'memory.peak').unlink()
            return self.envelope
        script = f'''import os,time
from pathlib import Path
r=Path({str(self.root)!r})
(r/'start.pid').write_text(str(os.getpid()))
for i in range(64):
    (r/'progress').write_text(str(i+1))
    time.sleep(.002)
(r/'done.pid').write_text(str(os.getpid()))
time.sleep(30)
'''
        seen = []
        def observe(pid, _):
            seen.append(pid)
            if (self.root/'done.pid').exists():
                down[0] = False
                self.put('memory.peak', '4096')
                pkg.save(self.root/'runtime/stop.request.json', {})
            return self.observation(pid)
        with mock.patch.object(r, 'Envelope', side_effect=envelope), \
             mock.patch.object(r, 'properties', side_effect=properties) as manager, \
             mock.patch.object(Path, 'rmdir', autospec=True, side_effect=self.remove_leaf):
            rc, state = self.run_supervisor(observe, isolated=True, script=script,
                config_overrides=dict(startup_seconds=2),
                manifest_overrides=dict(version=10, host_resources=self.selected))
        self.assertEqual((rc, state['state'], state['attempts'], state['restarts']),
                         (0, 'STOPPED', 1, 0))
        self.assertEqual((self.root/'progress').read_text(), '64')
        self.assertEqual((self.root/'start.pid').read_text(), (self.root/'done.pid').read_text())
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(manager.call_count, 3)  # Initial and two strict close checks.
        resource = state['host_resources']
        self.assertGreater(resource['statistics_gap_samples'], 0)
        self.assertEqual(resource['observation']['statistics_errors'], {})
        self.assertEqual(resource['runtime_manager_observation'], 'NOT_QUERIED')
        self.assertTrue(state['cleanup_confirmed'])
        self.assertFalse(sup.processes.group_members(seen[0]))

    def test_v10_requires_resources_and_rejects_restart_without_changing_v9(self):
        binary, release, model = self.fixture()
        args = dict(nvml_policy='degrade', host_resources=self.selected)
        with self.assertRaises(RuntimeError):
            pkg.prepare(binary, release, model, self.root/'bad-restart', restart_limit=1, **args)
        receipt = pkg.prepare(binary, release, model, self.root/'v10-package', **args)
        path = Path(receipt['package'])/'manifest.json'
        value = pkg.verify(path.parent, receipt['manifest_sha256'])
        self.assertEqual(value['version'], 10)
        old_value = deepcopy(value)
        old_value['version'] = 9
        pkg.save(path, old_value)
        self.assertEqual(pkg.verify(path.parent, pkg.sha(path))['version'], 9)
        del value['host_resources']
        pkg.save(path, value)
        with self.assertRaisesRegex(RuntimeError, 'requires host resources'):
            pkg.verify(path.parent, pkg.sha(path))

    def test_v10_oom_stops_real_backend_without_replacement(self):
        def envelope(selected, output, *, isolate_manager=False):
            self.assertTrue(isolate_manager)
            self.envelope.output = output
            self.envelope.check(startup=True)
            return self.envelope
        seen = []
        def observe(pid, _):
            seen.append(pid)
            self.put('memory.events', 'high 0\nmax 1\noom 1\noom_kill 0')
            return self.observation(pid)
        with mock.patch.object(r, 'Envelope', side_effect=envelope), \
             mock.patch.object(r, 'properties', return_value=self.props), \
             mock.patch.object(Path, 'rmdir', autospec=True, side_effect=self.remove_leaf):
            rc, state = self.run_supervisor(observe, isolated=True,
                manifest_overrides=dict(version=10, host_resources=self.selected))
        self.assertEqual((rc, state['attempts'], state['restarts']), (1, 1, 0))
        self.assertEqual(state['error'], 'host_resource_oom')
        self.assertTrue(state['cleanup_confirmed'])
        self.assertFalse(sup.processes.group_members(seen[0]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
