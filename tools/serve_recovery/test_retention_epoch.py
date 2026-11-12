"""CPU ownership-boundary migration: real private flock, no GPU/model operations."""
import fcntl
import json
import os
from pathlib import Path
import time
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import ledger, retention as r, retention_epoch as epoch
from tools.serve_recovery import test_retention as fixtures


class EpochTests(unittest.TestCase):
    setUp = fixtures.RetentionTests.setUp
    write = fixtures.RetentionTests.write
    run_fixture = fixtures.RetentionTests.run_fixture
    archive = fixtures.RetentionTests.archive
    plan = fixtures.RetentionTests.plan

    def stopped(self):
        state = ledger.new_state(self.service_id,
            ledger.Stamp(r.native.stamp()['boot_id'], time.monotonic_ns()))
        self.write(self.state / 'ledger.json', dict(previous_sha256=None,
            sha256=ledger.digest(state), state=state))
        self.write(self.state / 'writer.lock', dict(version=1, service_id=self.service_id))
        self.write(self.state / 'guardian.lock', b'')
        self.lease = self.directory / 'cpu-only-ownership.lock'; self.write(self.lease, b'')
        patch = mock.patch.object(epoch.bundle, 'verify', return_value=dict(lock_path=str(self.lease)))
        patch.start(); self.addCleanup(patch.stop)

    def legacy(self):
        run_id, path = self.run_fixture()
        cfg = json.loads((path / 'controller.json').read_text())
        cfg['version'] = 2; cfg.pop('retention_context')
        self.write(path / 'controller.json', cfg)
        return run_id, path

    def test_legacy_run_migrates_then_archives_and_retires(self):
        self.stopped(); run_id, path = self.legacy()
        before = (self.state / 'ledger.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'epoch'):
            self.archive(run_id)
        result = epoch.migrate(self.state, run_id)
        self.assertTrue(result['migrated'])
        self.assertFalse(result['model_signalled']);self.assertFalse(result['gpu_probe_started'])
        self.archive(run_id)
        result = r.apply(self.state, self.plan(limits=r.policy(max_bytes=1)), pins=set())
        self.assertEqual(result['retired'][0]['state'], 'RETIRED')
        self.assertFalse(path.exists())
        self.assertEqual((self.state / 'ledger.json').read_bytes(), before)

    def test_live_lease_or_nonstopped_intent_refuses_migration(self):
        self.stopped(); run_id, path = self.legacy()
        fd = os.open(self.lease, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):epoch.migrate(self.state, run_id)
        finally:
            os.close(fd)
        self.assertFalse((self.state / 'retention' / (run_id + '.epoch.json')).exists())
        data = json.loads((self.state / 'ledger.json').read_text())
        data['state']['desired'] = 'RUNNING';data['sha256'] = ledger.digest(data['state'])
        self.write(self.state / 'ledger.json', data)
        with self.assertRaisesRegex(ValueError, 'STOPPED'):epoch.migrate(self.state, run_id)
        self.assertTrue(path.exists())

    def test_file_operations_never_hold_core_ownership_locks(self):
        self.stopped(); run_id, _ = self.legacy()
        original_json, original_inode = r.native.private_json, r.native.inode
        paths = (self.lease, self.state / 'guardian.lock', self.state / 'writer.lock')
        def check_free():
            for path in paths:
                fd = os.open(path, os.O_RDWR)
                try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:os.close(fd)
        def read(*args, **kwargs):
            check_free();return original_json(*args, **kwargs)
        def inode(*args, **kwargs):
            check_free();return original_inode(*args, **kwargs)
        with mock.patch.object(r.native, 'private_json', side_effect=read), \
                mock.patch.object(r.native, 'inode', side_effect=inode):
            self.assertTrue(epoch.migrate(self.state, run_id)['migrated'])
        check_free()

    def test_revision_race_and_changed_source_do_not_receive_eligibility(self):
        self.stopped(); run_id, path = self.legacy()
        original = epoch.stopped_snapshot
        calls = 0
        def changed(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                data = json.loads((self.state / 'ledger.json').read_text())
                data['state'], _ = ledger.transition(data['state'], dict(
                    id=str(uuid.uuid4()), expected_revision=data['state']['revision'],
                    operation='desired', args=dict(state='STOPPED')),
                    ledger.Stamp(r.native.stamp()['boot_id'], time.monotonic_ns()))
                data['sha256'] = ledger.digest(data['state'])
                self.write(self.state / 'ledger.json', data)
            return original(*args)
        with mock.patch.object(epoch, 'stopped_snapshot', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed'):epoch.migrate(self.state, run_id)
        self.assertFalse((self.state / 'retention' / (run_id + '.epoch.json')).exists())
        epoch.migrate(self.state, run_id)
        self.write(path / 'new-fault', b'changed after proof')
        with self.assertRaisesRegex(ValueError, 'epoch'):self.archive(run_id)
        self.assertEqual(self.plan()['protected'][0]['reason'], 'owner_epoch_unverified')

    def test_interrupted_legacy_retirement_rebinds_after_old_boot_proof(self):
        self.stopped(); run_id, _ = self.legacy()
        epoch.migrate(self.state, run_id);receipt = self.archive(run_id)
        unlink = os.unlink
        calls = 0
        def interrupt(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:raise OSError('CPU interruption')
            return unlink(*args, **kwargs)
        with mock.patch.object(r.os, 'unlink', side_effect=interrupt):
            with self.assertRaises(OSError):
                r.apply(self.state, self.plan(limits=r.policy(max_bytes=1)), pins=set())
        proof_path = self.state / 'retention' / (run_id + '.epoch.json')
        record = json.loads(proof_path.read_text());record['proof']['boot_id'] = str(uuid.uuid4())
        self.write(proof_path, record)
        with self.assertRaisesRegex(ValueError, 'epoch'):r.resume(self.state, run_id, pins=set())
        epoch.migrate(self.state, run_id)
        self.assertEqual(r.resume(self.state, run_id, pins=set())['state'], 'RETIRED')
        self.assertTrue((Path(receipt['archive']) / 'files/model-1/model.log').exists())


if __name__ == '__main__':
    unittest.main()
