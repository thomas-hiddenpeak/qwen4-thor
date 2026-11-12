"""Real owned resource-leaf binding; device rule/observation are CPU fixtures.

No host-root rule, GPU query or model start occurs. Actual native rule detach
has separate kernel/IPC evidence using scoped /dev/zero cgroups.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import device_domain as domain, device_resource as bound, device_store
from tools.serve_recovery import resource_install as install, resource_kernel as kernel, store
from tools.serve_recovery import test_resource_install as fixtures


class ResourceTests(fixtures.InstallationTests):
    def setUp(self):
        super().setUp(); self.initialize()
        self.device = self.parent/'device'; self.pins = self.parent/'cpu-bpffs'; self.pins.mkdir(mode=0o700)
        self.binary = ROOT/'.q4t-work/device-detach-build/q4t-device-control'
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        patch = mock.patch.object(domain, 'BPFFS', self.pins)
        patch.start(); self.addCleanup(patch.stop)
        self.lease_path = self.parent/'cpu-lease'
        self.lease = os.open(self.lease_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchown(self.lease, self.uid, self.gid); self.addCleanup(os.close, self.lease)
        import fcntl
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.calls = []

    def init_device(self):
        return domain.initialize(self.device, self.sid, self.uid, self.gid, self.binary,
            self.digest, resource_directory=self.directory)

    def config(self): return domain.configuration(domain.read_record(self.device/'configuration.json'))

    @contextmanager
    def controls(self):
        observation = dict(version=1, status='NO_USER_HANDLES_OBSERVED',
            facts=dict(boot_id=store.stamp().boot_id, character_devices=[dict(major=1, names=['cpu'])]),
            outstanding=[], model_actions=False, launch_authorized=False)
        @contextmanager
        def reply(operation, args, **_kwargs):
            self.calls.append(operation)
            path = Path(args['pin'])
            identity = dict(target_cgroup_id=os.fstat(args['target_fd']).st_ino,
                allowed_cgroup_id=os.fstat(args['allowed_fd']).st_ino,
                link_id=11, program_id=12, program_tag='0123456789abcdef')
            if operation == 'attach': path.write_bytes(b'CPU fake device rule only')
            if operation == 'detach': path.unlink()
            self.assertIn(operation, ('attach', 'recover', 'detach'))
            yield mock.Mock(result='detached' if operation == 'detach' else identity, fds=[])
        with mock.patch.object(domain.Observation, 'inspect', return_value=observation), \
                mock.patch.object(domain.device_control.Control, 'call', side_effect=reply):
            yield

    def test_selection_and_actual_allowed_fd_are_original_service_leaf(self):
        value = self.init_device(); config = value['configuration']; original = install.load(self.directory)
        self.assertEqual(config['format'], bound.CONFIG_FORMAT)
        with domain.scope(config, create=True) as (group, _pin, target, allowed):
            self.assertEqual(group, Path(original['record']['root'])/'service')
            self.assertEqual(os.fstat(target).st_ino, 1)
            self.assertEqual(os.fstat(allowed).st_ino, original['record']['children']['service'][1])
            self.assertNotEqual(os.fstat(allowed).st_ino, original['record']['children']['outer'][1])
        self.assertFalse(domain.status(self.device)['qualified'])
        self.assertEqual(install.load(self.directory), original)

    def test_runtime_or_service_identity_mismatch_is_rejected_before_device_install(self):
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            domain.initialize(self.device, self.sid, self.uid+1, self.gid, self.binary,
                self.digest, resource_directory=self.directory)
        self.assertFalse(self.device.exists())

    def test_partial_association_blocks_removal_and_resumes_original_ids(self):
        original = domain.write_record
        def partial(path, value):
            if Path(path).name == bound.ASSOCIATION: raise OSError('injected association publication')
            return original(path, value)
        with mock.patch.object(domain, 'write_record', side_effect=partial), self.assertRaises(OSError):
            self.init_device()
        config = self.config(); resource = install.load(self.directory)
        with self.assertRaises((OSError, ValueError)): install.remove(self.directory)
        self.assertEqual(install.load(self.directory), resource)
        domain.resume_initialize(self.device)
        self.assertEqual(self.config(), config)
        self.assertTrue(bound.lifecycle(self.device, config)['initialized'])

    def test_lost_published_binding_or_fence_history_cannot_reset(self):
        self.init_device(); config = self.config()
        for path in (self.device/bound.FENCE, self.directory/'admin'/bound.ASSOCIATION,
                     self.directory/'admin'/bound.PERMANENT):
            with self.subTest(path=path.name):
                raw = path.read_bytes(); path.unlink()
                with self.assertRaises((OSError, ValueError)): domain.resume_initialize(self.device)
                with self.assertRaises((OSError, ValueError)): install.remove(self.directory)
                self.assertEqual(install.load(self.directory)['phase'], 'ready')
                path.write_bytes(raw); path.chmod(0o600)
        self.assertEqual(self.config(), config)

    def test_resource_removal_waits_for_explicit_detach_and_preserves_original_budget(self):
        self.init_device(); config = self.config()
        with self.controls(): domain.arm(self.device, self.lease, self.lease_path)
        before = install.load(self.directory)
        with domain.managed(self.device) as (_, _, binding, _):
            with device_store.open_bound(binding) as journal:
                total = journal.head['state']['policy']['total']
        with self.assertRaisesRegex(ValueError, 'disarm'): install.remove(self.directory)
        self.assertEqual(install.load(self.directory), before)
        with self.controls(): domain.disarm(self.device, self.lease, self.lease_path)
        self.assertTrue(Path(before['record']['root']).exists())
        self.assertEqual(install.load(self.directory), before)
        self.assertEqual(domain.status(self.device)['charged_total'], total)
        self.assertIn('detach', self.calls); self.assertTrue(install.remove(self.directory)['removed'])

    def test_populated_service_leaf_blocks_device_scope_without_signalling_it(self):
        self.init_device(); process, _ = self.run_worker('join')
        with self.assertRaisesRegex(ValueError, 'populated'):
            with domain.scope(self.config(), create=True): self.fail('populated domain accepted')
        self.assertIsNone(process.poll()); self.assertEqual(self.calls, [])

    def test_same_boot_replaced_kernel_leaf_does_not_match_resource_binding(self):
        self.init_device(); value = install.load(self.directory); root = Path(value['record']['root'])
        for role in kernel.NAMES: (root/role).rmdir()
        root.rmdir()
        kernel.create(self.sid, value['domain_id'], self.uid, self.selected)
        with self.assertRaisesRegex(ValueError, 'kernel domain changed'):
            with domain.scope(self.config(), create=True): self.fail('replaced domain accepted')

    def test_new_boot_release_preserves_history_without_recreating_old_scope(self):
        self.init_device(); config = self.config()
        old = bound.lifecycle(self.device, config)
        old = dict(old, phase='armed', boot_id='00000000-0000-0000-0000-000000000001')
        domain.write_record(self.device/bound.FENCE, old)
        resource = deepcopy(install.load(self.directory))
        with mock.patch.object(domain, 'scope', side_effect=AssertionError('must not recreate old scope')):
            result = domain.disarm(self.device, self.lease, self.lease_path)
        self.assertFalse(result['armed']); self.assertEqual(install.load(self.directory), resource)
        self.assertEqual(bound.lifecycle(self.device, config)['phase'], 'idle')


if __name__ == '__main__': unittest.main(module=None)
