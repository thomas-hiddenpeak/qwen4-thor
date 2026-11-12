"""Targeted deployment control checks; no host device rule or GPU device open."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import (access_store, device_domain as d,
    device_inventory, device_platform as p, device_store, ledger, service, store)


class PlatformTests(unittest.TestCase):
    def facts(self):
        return dict(kernel_release=p.KERNEL, modules=[dict(name=name,
            source=value[1], version='595.78') for name, value in p.MODULES.items()])

    def test_profile_binds_real_artifacts_kernel_and_alternate_compute_driver(self):
        facts = self.facts(); hashes = {name: value[2] for name, value in p.MODULES.items()}
        self.assertEqual(p.validate(facts, hashes)['profile'], p.PROFILE)
        for field, value in [('kernel_release', 'other-kernel'),
                             ('modules', [dict(facts['modules'][0], source='F'*24)])]:
            with self.assertRaises(ValueError): p.validate(dict(facts, **{field: value}), hashes)
        with self.assertRaises(ValueError): p.validate(facts, dict(hashes, nvidia='0'*64))
        with self.assertRaises(ValueError): p.validate(facts, hashes, alternate_driver=True)

    def test_alternate_device_entry_points_never_disappear_from_fence_review(self):
        base = 'Character devices:\n195 nvidia\n195 nvidiactl\n486 nvidia-uvm\n'
        for name in ('nvgpu', 'nvhost-gpu', 'gdrdrv', 'nvidia-other'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                device_inventory.character_devices(base+'490 '+name+'\nBlock devices:\n')

    def test_single_service_entry_dispatches_exact_management_arguments(self):
        argv = ['arm', '--directory', '/var/lib/q4t/domain', '--lease-path', '/tmp/lease']
        with mock.patch.object(d, 'main', return_value=17) as call:
            self.assertEqual(service.main(['device-domain', *argv]), 17)
        call.assert_called_once_with(argv)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'explicit privileged CPU checks required')
        self.temporary = tempfile.TemporaryDirectory(prefix='q4t-device-cpu-', dir='/run')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root/'state'
        self.binary = Path(os.environ['Q4T_DEVICE_CONTROL_BINARY']).resolve(strict=True)
        self.binary_hash = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.uid, self.gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        self.service_id = str(uuid.uuid4())
        value = d.initialize(self.directory, self.service_id, self.uid, self.gid,
                             self.binary, self.binary_hash)
        self.config = value['configuration']
        self.binding = access_store.binding(self.directory, self.service_id)
        self.lease_path = self.root/'lease'
        self.lease = os.open(self.lease_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.fchown(self.lease, self.uid, self.gid)
        self.addCleanup(os.close, self.lease)
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.group, self.pin = self.root/'group', self.root/'pins/policy'
        self.group.mkdir(); self.pin.parent.mkdir(mode=0o700)
        self.target = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.allowed = os.open(self.group, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.target); self.addCleanup(os.close, self.allowed)
        self.observed = dict(version=1, status='NO_USER_HANDLES_OBSERVED',
            facts=dict(boot_id=store.stamp().boot_id, character_devices=[dict(major=1, names=['cpu'])]),
            outstanding=[], model_actions=False, launch_authorized=False)
        self.kernel = dict(target_cgroup_id=1, allowed_cgroup_id=os.fstat(self.allowed).st_ino,
                           link_id=11, program_id=12, program_tag='0123456789abcdef')
        self.operations = []

    @contextmanager
    def scope(self, _config, **_kwargs):
        # Only the policy ordering uses a fake scope. Native kernel scope/code
        # verification is exercised separately with actual /dev/zero rules.
        yield self.group, self.pin, self.target, self.allowed

    @contextmanager
    def reply(self, operation, args, **_kwargs):
        self.operations.append(operation)
        if operation == 'attach': self.pin.write_bytes(b'cpu-only-fake-link')
        if operation in ('remove', 'detach'): self.pin.unlink()
        yield mock.Mock(result='detached' if operation == 'detach' else self.kernel,
                        fds=[self.target, self.allowed])

    @contextmanager
    def environment(self):
        with mock.patch.object(d, 'scope', side_effect=self.scope), \
             mock.patch.object(d, 'names', return_value=(self.group, self.pin.parent)), \
             mock.patch.object(d, 'empty'), \
             mock.patch.object(d.Observation, 'inspect', return_value=self.observed), \
             mock.patch.object(d.device_control.Control, 'call', side_effect=self.reply):
            yield

    def policy(self):
        with device_store.open_bound(self.binding) as writer:
            return deepcopy(writer.head['state']['policy'])

    def test_root_ancestry_symlink_and_live_management_lock_are_enforced(self):
        self.assertEqual(d.secure_directory(self.directory, private=True), self.directory)
        alias = self.root/'alias'; alias.symlink_to(self.directory)
        with self.assertRaises(ValueError): d.secure_directory(alias)
        self.root.chmod(0o777)
        try:
            with self.assertRaises(ValueError): d.secure_directory(self.directory)
        finally: self.root.chmod(0o700)
        with d.exclusive(self.directory):
            with self.assertRaises(BlockingIOError):
                with d.exclusive(self.directory): pass

    def test_initialized_budget_and_lost_bound_state_are_never_reset(self):
        with self.assertRaises(ValueError):
            d.initialize(self.directory, self.service_id, self.uid, self.gid,
                         self.binary, self.binary_hash)
        device_store.commit_qualification(self.binding, 'a'*64)
        device_store.charge_once(self.binding, 'b'*64, 1, 2)
        self.assertFalse(d.resume_initialize(self.directory)['initialized'])
        self.assertEqual(self.policy()['total'], 1)
        shutil.rmtree(self.directory/'device-domain')
        with self.assertRaises(OSError): d.resume_initialize(self.directory)
        self.assertTrue((self.directory/'device-domain.json').exists())

    def test_interrupted_initial_journal_resumes_without_admitting_a_model(self):
        other = self.root/'incomplete'
        with mock.patch.object(d.access_store, 'bootstrap_bound', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                d.initialize(other, str(uuid.uuid4()), self.uid, self.gid,
                             self.binary, self.binary_hash)
        value = d.resume_initialize(other)
        self.assertTrue(value['initialized'])
        self.assertFalse(d.status(other)['qualified'])

    def test_changed_helper_or_forged_record_never_reaches_kernel_control(self):
        helper = self.directory/'q4t-device-control'
        helper.write_bytes(b'changed')
        with mock.patch.object(d.device_control, 'Control') as control:
            with self.assertRaises(ValueError): d.arm(self.directory, self.lease, self.lease_path)
        control.assert_not_called()
        self.assertEqual(self.policy()['total'], 0)

    def test_busy_observation_refuses_before_creating_or_attaching_scope(self):
        with mock.patch.object(d.Observation, 'inspect', side_effect=ValueError('BUSY')), \
             mock.patch.object(d, 'scope') as scope:
            with self.assertRaisesRegex(ValueError, 'BUSY'):
                d.arm(self.directory, self.lease, self.lease_path)
        scope.assert_not_called()
        self.assertIsNone(self.policy()['qualification'])

    def test_interrupted_post_pin_qualification_reuses_exact_custody(self):
        with self.environment(), mock.patch.object(device_store, 'commit_qualification',
                                                   side_effect=OSError('fsync')):
            with self.assertRaises(OSError): d.arm(self.directory, self.lease, self.lease_path)
        self.assertTrue(self.pin.exists())
        self.assertIsNone(self.policy()['qualification'])
        with self.environment(): value = d.arm(self.directory, self.lease, self.lease_path)
        self.assertTrue(value['armed']); self.assertEqual(self.operations, ['attach', 'recover'])
        self.assertEqual(self.policy()['total'], 0)

    def test_entry_persists_charge_before_issuer_and_never_refunds_failed_issue(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        @contextmanager
        def rejected(operation, args, **kwargs):
            if operation == 'issue':
                self.assertEqual(self.policy()['total'], 1)
                raise OSError('issuer failed')
            with self.reply(operation, args, **kwargs) as reply: yield reply
        with self.environment(), mock.patch.object(d.device_control.Control, 'call', side_effect=rejected):
            with self.assertRaisesRegex(OSError, 'issuer failed'):
                d.enter(self.directory, self.lease, self.lease_path, 'a'*64, ['/bin/false'])
            with self.assertRaisesRegex(ValueError, 'cooldown'):
                d.enter(self.directory, self.lease, self.lease_path, 'a'*64, ['/bin/false'])
        self.assertEqual(self.policy()['total'], 1)

    def test_changed_boot_or_ownership_cannot_issue_or_consume_budget(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        changed = deepcopy(self.observed); changed['facts']['boot_id'] = str(uuid.uuid4())
        with self.environment(), mock.patch.object(d.Observation, 'inspect', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'ownership'):
                d.enter(self.directory, self.lease, self.lease_path, 'a'*64, ['/bin/false'])
        self.assertEqual(self.policy()['total'], 0)

    def test_disarm_uses_retained_rules_without_monitor_and_preserves_budget(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        device_store.charge_once(self.binding, 'a'*64, 1, 2)
        # Fake scope identity is retained in the intent; only the native helper
        # is mocked. It never points at the real host device cgroup.
        with self.environment(), mock.patch.object(d, 'names', return_value=(self.group, self.pin.parent)), \
             mock.patch.object(d.Observation, 'inspect', side_effect=AssertionError('monitor called')):
            value = d.disarm(self.directory, self.lease, self.lease_path)
        self.assertFalse(value['armed']); self.assertEqual(self.policy()['total'], 1)
        self.assertIsNone(self.policy()['qualification']); self.assertFalse(self.pin.exists())

    def test_wrong_user_or_unheld_lease_never_creates_a_filter(self):
        with mock.patch.object(d, 'scope') as scope:
            os.fchown(self.lease, 0, 0)
            with self.assertRaises(ValueError): d.arm(self.directory, self.lease, self.lease_path)
            os.fchown(self.lease, self.uid, self.gid)
            fcntl.flock(self.lease, fcntl.LOCK_UN)
            with self.assertRaises(ValueError): d.arm(self.directory, self.lease, self.lease_path)
        scope.assert_not_called()

    def test_detach_interruption_resumes_saved_identity_without_recovering_authority(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        original = self.reply
        @contextmanager
        def fail(operation, args, **kwargs):
            if operation == 'detach': raise OSError('injected detach before receipt')
            with original(operation, args, **kwargs) as result: yield result
        with self.environment(), mock.patch.object(d.device_control.Control, 'call', side_effect=fail), \
                self.assertRaises(OSError):
            d.disarm(self.directory, self.lease, self.lease_path)
        before = d.read_record(self.directory/'removal.json')
        self.assertEqual(before['identity'], self.kernel); self.assertTrue(self.pin.exists())
        self.operations.clear()
        with self.environment(), mock.patch.object(d, 'names', return_value=(self.group, self.pin.parent)):
            self.assertFalse(d.disarm(self.directory, self.lease, self.lease_path)['armed'])
        self.assertNotIn('recover', self.operations); self.assertIn('detach', self.operations)
        self.assertEqual(d.read_record(self.directory/'removal.json'), before)

    def test_pending_detach_cannot_requalify_or_change_its_selected_fence(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        intent = d.read_record(self.directory/'fence.json')
        d.write_record(self.directory/'removal.json', dict(version=1, intent=intent, identity=self.kernel))
        before = self.policy()
        with self.environment(), self.assertRaisesRegex(ValueError, 'removal before rearming'):
            d.arm(self.directory, self.lease, self.lease_path)
        self.assertEqual(self.policy(), before); self.assertTrue(self.pin.exists())
        d.write_record(self.directory/'removal.json', dict(version=1,
            intent=dict(intent, allowed_cgroup_id=42), identity=self.kernel))
        with self.environment(), self.assertRaisesRegex(ValueError, 'removal selection changed'):
            d.disarm(self.directory, self.lease, self.lease_path)
        self.assertTrue(self.pin.exists())

    def test_detach_failure_never_falls_back_to_unpin_only_helper(self):
        with self.environment(): d.arm(self.directory, self.lease, self.lease_path)
        original = self.reply; calls = []
        @contextmanager
        def legacy(operation, args, **kwargs):
            calls.append(operation)
            if operation == 'detach': raise ValueError('legacy helper has no detach operation')
            with original(operation, args, **kwargs) as result: yield result
        with self.environment(), mock.patch.object(d.device_control.Control, 'call', side_effect=legacy), \
                self.assertRaisesRegex(ValueError, 'legacy helper'):
            d.disarm(self.directory, self.lease, self.lease_path)
        self.assertTrue(self.pin.exists()); self.assertNotIn('remove', calls)
        self.assertTrue((self.directory/'removal.json').exists())

    def test_actual_management_cli_status_never_claims_runtime_qualification(self):
        output = io.StringIO()
        with mock.patch('sys.stdout', output):
            self.assertEqual(service.main(['device-domain', 'status', '--directory', str(self.directory)]), 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value['qualified']); self.assertFalse(value['runtime_verified'])
        self.assertFalse(value['model_actions']); self.assertEqual(value['charged_total'], 0)


if __name__ == '__main__': unittest.main()
