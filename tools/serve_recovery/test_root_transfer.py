"""Real STOPPED journals and injected write failures; CPU model receipts only."""
import copy
import os
from pathlib import Path
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import access_store, deployment, ledger, outer_manager
from tools.serve_recovery import profile_manager, profile_store, root_checkpoints
from tools.serve_recovery import root_transfer, store, test_version_registration
from tools.serve_recovery import version_launch, version_policy, version_registration, version_store


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.base = test_version_registration.RegistrationTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.install(); self.spent = self.base.consume()
        self.old = copy.deepcopy(self.base.root)
        newer = self.base.repair()
        self.base.install(newer, self.spent['registry']['cycle_id'])
        self.new = copy.deepcopy(newer['root'])
        self.fresh = self.base.load()
        self.state, self.binding = self.base.state, self.base.binding
        outer_manager.initialize(self.state, self.new['package'], self.new['manifest_sha256'])
        self.protected = {name:(self.state/name).read_bytes() for name in
            ['ledger.json', 'outer-initialization.json', 'outer-recovery/policy.json',
             *(w.directory_name+'/policy.json' for w in deployment.FAMILIES.values())]}

    def switch(self, target, request=None):
        desc = deployment.read_descriptor(self.state)
        return deployment.switch(self.state, target['package'], target['manifest_sha256'],
            expected=desc['manifest_sha256'], request_id=request or str(uuid.uuid4()), rollback=True)

    def assert_protected(self):
        for name, raw in self.protected.items():
            self.assertEqual((self.state/name).read_bytes(), raw, name)

    def assert_old(self, revision):
        selected = version_registration.load(self.state, deployment.read_descriptor(self.state))
        self.assertEqual(selected['state'], dict(self.spent, revision=revision))
        self.assertEqual(version_policy.launch_payload(selected['state']), 'fallback')
        with profile_store.open_bound(self.binding) as writer:
            old_manifest = root_transfer.version_contract.payload(
                selected['registration']['payloads']['fallback']['descriptor'])['manifest']
            profile_manager.matches(writer.head['state']['policy'], old_manifest)
        self.assert_protected()

    def test_real_roundtrip_restores_consumed_cycle_and_actual_fallback_profile(self):
        self.switch(self.old); self.assert_old(3)
        self.switch(self.new)
        self.assertEqual(self.base.load(), dict(self.fresh, revision=4))
        self.switch(self.old); self.assert_old(5)
        self.assertEqual(len(list((self.state/root_checkpoints.DIRECTORY).iterdir())), 4)

    def test_reserve_cycle_rolls_back_to_consumed_base_and_forward(self):
        cycle = self.fresh['registry']['cycle_id']
        reserve = self.state/version_registration.RESERVE_DIRECTORY
        reserve.mkdir(mode=0o700)
        base = self.state/version_registration.DIRECTORY
        (base/(cycle+'.json')).rename(reserve/(cycle+'.json'))
        self.switch(self.old)
        self.assert_old(3)
        old = self.spent['registry']['cycle_id']
        self.assertEqual({p.name for p in base.iterdir()}, {old+'.json'})
        self.assertEqual({p.name for p in (self.state/root_checkpoints.DIRECTORY).iterdir()},
                         {old+'.bound', old+'.json'})
        self.switch(self.new)
        self.assertEqual(self.base.load(), dict(self.fresh, revision=4))
        self.assertEqual({p.name for p in (self.state/root_checkpoints.RESERVE_DIRECTORY).iterdir()},
                         {cycle+'.bound', cycle+'.json'})
        self.assert_protected()

    def test_terminal_cycle_survives_roundtrip_and_actual_launch_gate(self):
        self.switch(self.old)
        value = self.base.load()
        payload = value['registry']['payloads']['fallback']['manifest_sha256']
        event = dict(role='model', generation=2, boot_id=value['boot_id'], payload_sha256=payload,
                     kind='native_process_signal', signal=11, fingerprint='e'*64)
        proof = dict(desired='RUNNING', boot_id=value['boot_id'], payload_sha256=payload,
            generations={r:2 for r in version_policy.ROLES}, termination_requested=False,
            **{k:True for k in version_policy.GUARDS})
        terminal = version_policy.observe(value, event, proof, str(uuid.uuid4()))
        version_store.execute(self.binding, dict(sequence=1, op='commit',
            request_id=str(uuid.uuid4()), expected_revision=value['revision'], candidate=terminal))
        self.switch(self.new); self.switch(self.old)
        self.assertEqual(self.base.load(), dict(terminal, revision=6))
        with self.assertRaisesRegex(ValueError, 'version recovery exhausted'):
            version_launch.configuration(self.state, deployment.read_descriptor(self.state),
                                         self.state/'MUST_NOT_CREATE')
        self.assertFalse((self.state/'MUST_NOT_CREATE').exists()); self.assert_protected()

    def test_partial_cross_cycle_journal_reconciles_only_original_transfer(self):
        request, original = str(uuid.uuid4()), os.replace
        def fail(src, dst, **kwargs):
            fd = kwargs.get('dst_dir_fd')
            if str(src) == 'policy.pending' and fd is not None and \
                    Path(os.readlink('/proc/self/fd/'+str(fd))).name == 'version-selection':
                raise OSError('CPU root transfer before rename')
            return original(src, dst, **kwargs)
        with mock.patch.object(os, 'replace', side_effect=fail), self.assertRaises(OSError):
            self.switch(self.old, request)
        with self.assertRaises(ValueError): self.base.load()
        self.switch(self.old, request); self.assert_old(3)
        result = deployment.switch(self.state, self.old['package'], self.old['manifest_sha256'],
            expected=self.new['manifest_sha256'], request_id=request, rollback=True)
        self.assertTrue(result['replayed'])

    def test_partial_checkpoint_resumes_original_bytes_before_any_state_change(self):
        request, original, fired = str(uuid.uuid4()), store.Store.write_all, False
        def fail(fd, raw):
            nonlocal fired
            name = Path(os.readlink('/proc/self/fd/'+str(fd)))
            if not fired and name.parent.name == root_checkpoints.DIRECTORY and name.name.endswith('.json.new'):
                fired = True; os.write(fd, raw[:91]); raise OSError('CPU checkpoint partial')
            return original(fd, raw)
        with mock.patch.object(store.Store, 'write_all', side_effect=fail), self.assertRaises(OSError):
            self.switch(self.old, request)
        self.assertTrue(fired); self.assertEqual(self.base.load(), self.fresh)
        self.switch(self.old, request); self.assert_old(3)

    def test_descriptor_failure_after_version_commit_replays_without_refund(self):
        request = str(uuid.uuid4())
        with mock.patch.object(deployment, 'write_descriptor', side_effect=OSError('CPU descriptor')), \
                self.assertRaises(OSError):
            self.switch(self.old, request)
        self.assertEqual(self.base.load(), dict(self.spent, revision=3))
        self.switch(self.old, request); self.assert_old(3)

    def test_missing_checkpoint_cannot_fall_back_to_retained_registration_seed(self):
        self.switch(self.old); self.switch(self.new)
        cycle = self.spent['registry']['cycle_id']
        checkpoint = self.state/root_checkpoints.DIRECTORY/(cycle+'.json')
        checkpoint.rename(self.state/'missing-checkpoint.saved')
        before = self.base.load()
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.switch(self.old)
        self.assertEqual(self.base.load(), before); self.assert_protected()

    def test_original_writer_competition_and_outer_contract_are_preserved(self):
        for name in ('deployment.lock', 'guardian.lock'):
            with deployment.claim(self.state, name=name), self.assertRaises(BlockingIOError):
                self.switch(self.old)
        with version_store.open_bound(self.binding), self.assertRaises(BlockingIOError):
            self.switch(self.old)
        with self.assertRaisesRegex(ValueError, 'outer supervision'):
            self.switch(self.base.fallback)
        self.assertEqual(self.base.load(), self.fresh); self.assert_protected()

    def test_another_request_cannot_recover_or_replace_prepared_root_transfer(self):
        request = str(uuid.uuid4())
        with mock.patch.object(profile_manager, 'deploy', side_effect=OSError('CPU migration')), \
                self.assertRaises(OSError):
            self.switch(self.old, request)
        intent = (self.state/root_transfer.INTENT).read_bytes()
        with self.assertRaisesRegex(ValueError, 'resume identity'):
            self.switch(self.old)
        self.assertEqual((self.state/root_transfer.INTENT).read_bytes(), intent)
        self.switch(self.old, request); self.assert_old(3)


if __name__ == '__main__': unittest.main()
