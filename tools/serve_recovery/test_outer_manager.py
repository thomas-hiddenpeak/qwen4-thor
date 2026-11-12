"""Explicit outer enablement and resumable private journal CPU checks."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import access_store, deployment, journal, ledger, lifecycle
from tools.serve_recovery import outer_manager as manager, outer_policy, outer_store, service, store
from tools.serve_recovery import test_lifecycle


class ManagerTests(unittest.TestCase):
    setUp = test_lifecycle.LifecycleTests.setUp
    command = test_lifecycle.LifecycleTests.command
    files = test_lifecycle.LifecycleTests.files

    def initialize(self): return manager.initialize(self.state, self.package, self.digest)
    def status(self): return manager.status(self.state, self.package, self.digest)
    def configure(self, enabled):
        return manager.configure(self.state, self.package, self.digest, enabled=enabled)
    def core(self):
        with store.Store(self.state, self.desc['service_id']) as current: return copy.deepcopy(current.state)
    def change_core(self, operation, **args):
        with store.Store(self.state, self.desc['service_id']) as current:
            return current.transact(dict(id=str(uuid.uuid4()), expected_revision=current.state['revision'],
                                         operation=operation, args=args))
    def policy(self): return lifecycle.read_family(self.state, self.desc, outer_store.Store)

    def test_explicit_cli_initialization_never_changes_core_or_starts_business(self):
        before = self.files()
        with mock.patch.object(service, 'acquire_lease', side_effect=AssertionError('GPU forbidden')), \
             mock.patch.object(service, 'launch', side_effect=AssertionError('launch forbidden')):
            for operation in ('outer-status', 'outer-enable'):
                rc, _ = self.command(operation); self.assertEqual(rc, 1)
            self.assertFalse((self.state/manager.INTENT).exists())
            rc, result = self.command('outer-init'); self.assertEqual(rc, 0)
            self.assertTrue(result['initialization_complete']); self.assertFalse(result['enabled'])
            self.assertFalse(result['automatic_start']); self.assertFalse(result['model_actions'])
            for operation in ('outer-enable', 'outer-status', 'outer-disable', 'outer-init'):
                rc, result = self.command(operation); self.assertEqual(rc, 0, result)
                self.assertFalse(result['automatic_start'])
        for path, value in before.items(): self.assertEqual((self.state/path).read_bytes(), value)
        self.assertEqual(list((self.state/'runs').iterdir()), [])

    def test_partial_first_intent_has_no_policy_authority_and_can_resume(self):
        original = store.Store.write_all; fired = False
        def write(fd, raw):
            nonlocal fired
            if not fired and manager.FORMAT.encode() in raw:
                fired = True; os.write(fd, raw[:19]); raise OSError('injected first intent loss')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=write), self.assertRaises(OSError):
            self.initialize()
        self.assertTrue(fired); self.assertFalse((self.state/outer_store.Store.directory_name).exists())
        with self.assertRaises(OSError): self.configure(True)
        self.assertTrue(self.initialize()['initialization_complete'])
        self.assertEqual(self.policy()['total'], 0)

    def test_partial_policy_bootstrap_reuses_original_seed(self):
        original = store.Store.write_all; fired = False
        before = (self.state/store.STATE).read_bytes()
        def write(fd, raw):
            nonlocal fired
            if not fired and os.readlink(f'/proc/self/fd/{fd}') == str(self.state/'outer-recovery/policy.pending'):
                fired = True; os.write(fd, raw[:17]); raise OSError('injected bootstrap partial write')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=write), self.assertRaises(OSError):
            self.initialize()
        seed = json.loads((self.state/manager.INTENT).read_bytes())
        self.assertTrue(fired); self.assertEqual(seed['phase'], 'prepared')
        self.assertTrue(self.initialize()['initialization_complete'])
        self.assertEqual(self.policy(), seed['initial'])
        self.assertEqual((self.state/store.STATE).read_bytes(), before)

    def test_lost_completion_receipt_stays_inactive_until_explicit_resume(self):
        original = manager.write_intent
        def write(state, value):
            if value['phase'] == 'complete': raise OSError('injected completion receipt loss')
            return original(state, value)
        with mock.patch.object(manager, 'write_intent', side_effect=write), self.assertRaises(OSError):
            self.initialize()
        before = (self.state/'outer-recovery/policy.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'explicit resume'): self.configure(True)
        self.assertTrue(self.initialize()['initialization_complete'])
        self.assertEqual((self.state/'outer-recovery/policy.json').read_bytes(), before)
        self.assertFalse(self.initialize()['initialized'])

    def test_missing_published_history_cannot_be_reseeded(self):
        self.initialize(); self.configure(True); self.configure(False)
        before = (self.state/store.STATE).read_bytes()
        shutil.rmtree(self.state/'outer-recovery'); (self.state/'outer-recovery.json').unlink()
        with self.assertRaises(OSError): self.initialize()
        self.assertFalse((self.state/'outer-recovery').exists())
        self.assertEqual((self.state/store.STATE).read_bytes(), before)

    def charged(self, *, protect=False):
        now = store.stamp(); begin = ledger.Stamp(now.boot_id, now.monotonic_ns-70*ledger.SECOND)
        with mock.patch.object(store, 'stamp', return_value=begin):
            self.initialize(); self.configure(True)
        self.change_core('desired', state='RUNNING')
        binding = access_store.binding(self.state, self.desc['service_id'])
        for index in range(4 if protect else 1):
            clock = lambda: ledger.Stamp(now.boot_id, begin.monotonic_ns+min(index, 2)*30*ledger.SECOND)
            receipt = outer_store.commit_once(binding, 'request', dict(core=self.core(),
                ticket=str(uuid.uuid4()), manifest_sha256=self.digest,
                cleanup_verified=True, identity_valid=True, custody=dict(supervisor=dict(
                    pid=os.getpid(), start_ticks=journal.process_start(os.getpid()), boot_id=now.boot_id),
                    instance_token=str(uuid.uuid4()), tree_scope=str(uuid.uuid4()))), clock=clock)
            if receipt['state']['last']['result']['allowed']:
                outer_store.commit_once(binding, 'retired', dict(
                    ticket=receipt['state']['pending']['ticket'], verified=True), clock=clock)
        return self.policy()

    def test_enable_disable_with_live_core_locks_keeps_intent_faults_and_budget(self):
        prior = self.charged(); self.change_core('quarantine', reason='device_failure')
        before = (self.state/store.STATE).read_bytes()
        with deployment.claim(self.state, name='guardian.lock'), \
             deployment.claim(self.state, name=store.LOCK, create=False):
            self.configure(False); self.configure(True)
            value = self.policy()
            self.assertEqual(value['total'], prior['total']); self.assertEqual(value['starts'], prior['starts'])
            self.assertTrue(self.status()['enabled'])
        self.assertEqual((self.state/store.STATE).read_bytes(), before)
        revision = value['revision']; self.configure(True)
        self.assertEqual(self.policy()['revision'], revision)

    def test_acknowledge_requires_stopped_disabled_empty_and_preserves_all_charges(self):
        prior = self.charged(protect=True); self.assertTrue(prior['protected'])
        with self.assertRaises(ValueError): manager.acknowledge(self.state, self.package, self.digest)
        self.configure(False)
        with self.assertRaises(ValueError): manager.acknowledge(self.state, self.package, self.digest)
        self.change_core('desired', state='STOPPED')
        self.change_core('quarantine', reason='device_failure'); before = self.core()
        value = manager.acknowledge(self.state, self.package, self.digest)
        self.assertTrue(value['acknowledged']); self.assertFalse(value['protected'])
        self.assertEqual(self.policy()['starts'], prior['starts']); self.assertEqual(value['charged_total'], 3)
        self.assertEqual(self.core(), before)

    def test_source_change_and_active_instance_cannot_reinitialize(self):
        self.initialize(); before = self.files()
        with self.assertRaises(ValueError): manager.configure(self.state, self.package, 'a'*64, enabled=True)
        with deployment.claim(self.state, name='guardian.lock'), self.assertRaises(BlockingIOError):
            self.initialize()
        with deployment.claim(self.state, name=store.LOCK, create=False), self.assertRaises(BlockingIOError):
            self.initialize()
        self.assertEqual(self.files(), before)


if __name__ == '__main__': unittest.main(module=None)
