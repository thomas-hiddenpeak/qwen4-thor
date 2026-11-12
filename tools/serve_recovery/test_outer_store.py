"""Real atomic journal faults keep spent outer startup attempts unavailable."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import access_store, ledger, outer_policy as p, outer_store as s


class StoreTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2]/'.q4t-work/serve-outer-recovery-20261112/cpu-fixtures'
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name); self.sid = str(uuid.uuid4())
        self.binding = access_store.binding(self.path, self.sid)
        self.now = ledger.Stamp(str(uuid.uuid4()), ledger.SECOND)
        self.initial = p.initial(self.sid, self.now)
        s.initialize_bound(self.binding, self.initial)
        self.commit('configure', dict(enabled=True))
        self.core = ledger.new_state(self.sid, self.now); self.core['desired'] = 'RUNNING'

    def commit(self, op, args):
        return s.commit_once(self.binding, op, args, clock=lambda:self.now)

    def request(self):
        return self.commit('request', dict(core=self.core, ticket=str(uuid.uuid4()),
            manifest_sha256='b'*64, cleanup_verified=True, identity_valid=True,
            custody=self.custody()))

    def custody(self):
        return dict(supervisor=dict(pid=100,start_ticks=200,boot_id=self.now.boot_id),
                    instance_token=str(uuid.uuid4()),tree_scope=str(uuid.uuid4()))

    def load(self):
        with s.open_bound(self.binding) as writer: return writer.head['state']['policy']

    def test_pending_rename_spends_attempt_and_never_redispatches(self):
        with mock.patch.object(os, 'replace', side_effect=OSError('before rename')):
            with self.assertRaises(OSError): self.request()
        self.assertTrue((self.path/'outer-recovery/policy.pending').exists())
        recovered = self.load(); self.assertEqual(recovered['total'], 1)
        next_try = self.request()['state']
        self.assertEqual(next_try['last']['result']['reason'], 'unreconciled_attempt')
        self.assertEqual(next_try['pending'], recovered['pending'])
        self.assertEqual(next_try['total'], 1)

    def test_postrename_fsync_poison_preserves_original_pending_attempt(self):
        with s.open_bound(self.binding) as writer:
            previous = writer.head['state']['policy']
            candidate = p.change(previous, 'request', dict(core=self.core,ticket=str(uuid.uuid4()),
                manifest_sha256='c'*64,cleanup_verified=True,identity_valid=True,
                custody=self.custody()),self.now)
            real = os.fsync
            def fail(fd):
                if fd == writer.dir_fd: raise OSError('postrename')
                return real(fd)
            with mock.patch.object(os, 'fsync', side_effect=fail):
                with self.assertRaises(OSError): writer.transact(str(uuid.uuid4()),previous['revision'],candidate)
            self.assertTrue(writer.poisoned)
            with self.assertRaises(ValueError): writer.transact(str(uuid.uuid4()),previous['revision'],candidate)
        self.assertEqual(self.load(), candidate)
        self.assertFalse(self.request()['state']['last']['result']['allowed'])

    def test_missing_or_corrupt_state_cannot_recreate_budget(self):
        spent = self.request()['state']; path = self.path/'outer-recovery/policy.json'
        original = path.read_bytes(); path.write_bytes(b'{')
        with self.assertRaises(ValueError): self.load()
        path.write_bytes(original); self.assertEqual(self.load(), spent)
        shutil.rmtree(path.parent)
        with self.assertRaises((OSError,ValueError)): s.initialize_bound(self.binding,self.initial)
        self.assertFalse(path.parent.exists()); self.assertTrue((self.path/'outer-recovery.json').exists())

    def test_writer_conflict_and_foreign_service_never_change_journal(self):
        before = self.load()
        with s.open_bound(self.binding):
            with self.assertRaises(BlockingIOError): self.request()
        self.assertEqual(self.load(), before)
        self.core['service_id'] = str(uuid.uuid4())
        with self.assertRaises(ValueError): self.request()
        self.assertEqual(self.load(), before)


if __name__ == '__main__': unittest.main()
