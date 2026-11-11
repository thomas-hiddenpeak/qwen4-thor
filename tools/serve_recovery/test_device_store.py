"""Actual private root journal faults retain charges; no GPU or host fence."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import access_store, device_budget as p
from tools.serve_recovery import device_store as s, ledger


class StoreTests(unittest.TestCase):
    def setUp(self):
        s.privileged()  # Explicit sudo CPU group; never silently skip it.
        base = Path(__file__).resolve().parents[2]/'.q4t-work/serve-device-deployment-20261112/store-tests'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.service_id, self.domain_id, self.boot = (str(uuid.uuid4()) for _ in range(3))
        self.binding = access_store.binding(self.path, self.service_id)
        self.now = ledger.Stamp(self.boot, ledger.SECOND)
        self.initial = p.initial(self.service_id, self.domain_id, 2002, self.now)
        s.initialize_bound(self.binding, self.initial)
        s.commit_qualification(self.binding, 'a'*64, clock=lambda: self.now)

    def charge(self):
        return s.charge_once(self.binding, 'b'*64, 2049, 321, clock=lambda: self.now)

    def load(self):
        with s.open_bound(self.binding) as writer:
            return writer.head['state']['policy']

    def test_pending_rename_spends_charge_before_next_entry_and_cannot_reissue(self):
        with mock.patch.object(os, 'replace', side_effect=OSError('pre-rename')):
            with self.assertRaises(OSError): self.charge()
        self.assertTrue((self.path/'device-domain/policy.pending').exists())
        recovered = self.load()
        self.assertEqual(recovered['total'], 1)
        with self.assertRaisesRegex(ValueError, 'cooldown'): self.charge()
        self.now = ledger.Stamp(self.boot, 31*ledger.SECOND)
        second = self.charge()
        self.assertEqual(second['state']['total'], 2)
        self.assertNotEqual(second['state']['last_ticket']['id'], recovered['last_ticket']['id'])
        self.assertTrue(second['receipt']['durable'])
        self.assertFalse(second['receipt']['replayed'])
        self.assertEqual(second['receipt_sha256'], ledger.digest(second['receipt']))

    def test_postrename_fsync_lost_reply_preserves_charge_and_poisoned_writer(self):
        with s.open_bound(self.binding) as writer:
            old = writer.head['state']['policy']
            candidate = p.charge(old, 'b'*64, 2049, 321, str(uuid.uuid4()), self.now)
            real = os.fsync
            def fail(fd):
                if fd == writer.dir_fd: raise OSError('post-rename unknown')
                return real(fd)
            with mock.patch.object(os, 'fsync', side_effect=fail):
                with self.assertRaises(OSError):
                    writer.transact(candidate['last_ticket']['id'], old['revision'], candidate)
            self.assertTrue(writer.poisoned)
            with self.assertRaisesRegex(ValueError, 'requires recovery'):
                writer.transact(str(uuid.uuid4()), old['revision'], candidate)
        self.assertEqual(self.load(), candidate)
        with self.assertRaisesRegex(ValueError, 'cooldown'): self.charge()

    def test_missing_or_corrupt_journal_cannot_reinitialize_a_fresh_budget(self):
        spent = self.charge()['state']
        path = self.path/'device-domain/policy.json'
        original = path.read_bytes(); path.write_bytes(b'{')
        with self.assertRaises(ValueError): self.load()
        path.write_bytes(original)
        self.assertEqual(self.load(), spent)
        shutil.rmtree(path.parent)
        with self.assertRaises(OSError): s.initialize_bound(self.binding, self.initial)
        self.assertTrue((self.path/'device-domain.json').exists())
        self.assertFalse(path.parent.exists())

    def test_concurrent_writer_and_unprivileged_entry_cannot_issue(self):
        previous = self.load()
        with s.open_bound(self.binding):
            with self.assertRaises(BlockingIOError): self.charge()
        with mock.patch.object(os, 'geteuid', return_value=2002):
            with self.assertRaisesRegex(ValueError, 'requires root'): self.charge()
        self.assertEqual(self.load(), previous)

    def test_requalification_and_disable_preserve_original_ticket_and_budget(self):
        spent = self.charge()['state']
        self.now = ledger.Stamp(str(uuid.uuid4()), 1000000*ledger.SECOND)
        s.commit_qualification(self.binding, 'c'*64, clock=lambda: self.now)
        resumed = self.load()
        self.assertEqual(resumed['last_ticket'], spent['last_ticket'])
        self.assertEqual(resumed['attempts'], spent['attempts'])
        with self.assertRaisesRegex(ValueError, 'cooldown'): self.charge()
        s.disable(self.binding, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, 'lacks device qualification'): self.charge()
        self.assertEqual(self.load()['total'], 1)


if __name__ == '__main__': unittest.main()
