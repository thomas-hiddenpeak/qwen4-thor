"""Historical boot records plus real owned kernel domains; no host reboot."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import resource_install as install, resource_kernel as kernel, store
from tools.serve_recovery import test_resource_launch as launch_tests


class RenewalTests(launch_tests.LaunchTests):
    def drop_owned_domain(self):
        value = install.load(self.directory); root = Path(value['record']['root'])
        with kernel.Scope(value['record']) as scope:
            self.assertTrue(scope.empty()); self.assertTrue(scope.empty('outer'))
        for role in kernel.NAMES: (root/role).rmdir()
        root.rmdir()

    def old_epoch(self):
        self.initialize(); self.drop_owned_domain()
        value = install.load(self.directory)
        # Explicit synthetic historical metadata after removing only our empty
        # CPU fixture. This is not an observation that the host rebooted.
        value['record']['boot_id'] = str(uuid.uuid4())
        install.commit(self.directory, value)
        return deepcopy(value)

    def test_new_boot_recreation_preserves_installation_identity_policy_and_prior_epoch(self):
        before = self.old_epoch()
        status = install.status(self.directory)
        self.assertTrue(status['renewal_required']); self.assertFalse(status['admission_enabled'])
        result = install.renew(self.directory); after = install.load(self.directory)
        self.assertEqual(result['record']['boot_id'], store.stamp().boot_id)
        self.assertEqual(after['previous_record'], before['record'])
        self.assertEqual(after['revision'], before['revision']+2)
        for key in ('service_id', 'domain_id', 'runtime_uid', 'runtime_gid', 'policy'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(install.renew(self.directory), result)
        self.assertEqual(install.load(self.directory), after)

    def test_same_boot_deleted_domain_is_not_silently_recreated(self):
        self.initialize(); self.drop_owned_domain(); before = install.load(self.directory)
        with self.assertRaises(FileNotFoundError): install.renew(self.directory)
        self.assertEqual(install.load(self.directory), before)
        self.assertFalse(Path(before['record']['root']).exists())

    def test_existing_path_in_new_boot_cannot_be_adopted_or_erased(self):
        before = self.old_epoch(); root = Path(before['record']['root']); root.mkdir(mode=0o755)
        identity = kernel.identity(root)
        with self.assertRaisesRegex(ValueError, 'absent old domain'): install.renew(self.directory)
        self.assertEqual(kernel.identity(root), identity)
        self.assertEqual(install.load(self.directory), before)

    def test_partial_new_kernel_epoch_resumes_only_recorded_boot_and_domain(self):
        before = self.old_epoch(); original = kernel.write
        def fail(fd, name, amount):
            original(fd, name, amount)
            if name == 'memory.high': raise OSError('injected renewal controller write')
        with mock.patch.object(kernel, 'write', side_effect=fail), self.assertRaises(OSError):
            install.renew(self.directory)
        pending = install.load(self.directory); root = Path(before['record']['root'])
        identity = kernel.identity(root)
        self.assertEqual(pending['record'], before['record']); self.assertEqual(pending['phase'], 'renewing')
        self.assertEqual(pending['renewal']['target_boot'], store.stamp().boot_id)
        with self.assertRaisesRegex(ValueError, 'not enabled'): install.read_public(self.directory)
        result = install.finish(self.directory)
        self.assertEqual(result['record']['root_identity'], identity)
        self.assertEqual(install.load(self.directory)['previous_record'], before['record'])

    def test_ready_receipt_failure_repairs_current_epoch_without_another_renewal(self):
        before = self.old_epoch(); original = install.publish
        def fail(directory, value):
            if value['phase'] == 'ready': raise OSError('injected renewed publication')
            return original(directory, value)
        with mock.patch.object(install, 'publish', side_effect=fail), self.assertRaises(OSError):
            install.renew(self.directory)
        current = install.load(self.directory)
        self.assertEqual(current['previous_record'], before['record'])
        with self.assertRaisesRegex(ValueError, 'not enabled'): install.read_public(self.directory)
        install.finish(self.directory)
        self.assertEqual(install.load(self.directory), current)
        self.assertEqual(install.read_public(self.directory), install.public_value(current))

    def test_another_boot_during_unpublished_renewal_continues_original_installation(self):
        before = self.old_epoch()
        with mock.patch.object(kernel, 'create', side_effect=OSError('injected before new epoch')):
            with self.assertRaises(OSError): install.renew(self.directory)
        pending = install.load(self.directory)
        pending['renewal']['target_boot'] = str(uuid.uuid4())
        install.commit(self.directory, pending)
        result = install.renew(self.directory)
        self.assertEqual(result['domain_id'], before['domain_id'])
        self.assertEqual(result['record']['boot_id'], store.stamp().boot_id)
        self.assertEqual(result['revision'], before['revision']+3)

    def test_old_boot_uninstall_keeps_tombstone_without_recreating_any_domain(self):
        before = self.old_epoch(); result = install.remove(self.directory)
        self.assertTrue(result['removed']); self.assertFalse(Path(before['record']['root']).exists())
        self.assertEqual(result['domain_id'], before['domain_id'])
        with self.assertRaisesRegex(ValueError, 'cannot renew'): install.renew(self.directory)
        self.assertEqual(install.remove(self.directory), result)

    def test_explicit_entry_resumes_old_epoch_then_drops_privileges(self):
        before = self.old_epoch(); result = self.run_entry('normal')
        self.assertEqual(result['outer_uid'], self.uid); self.assertTrue(result['child']['fd_closed'])
        after = install.load(self.directory)
        self.assertEqual(after['previous_record'], before['record'])
        self.assertEqual(after['record']['boot_id'], store.stamp().boot_id)


if __name__ == '__main__': unittest.main(module=None)
