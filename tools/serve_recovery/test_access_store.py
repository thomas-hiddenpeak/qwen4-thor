"""Private CPU journal and C++ fingerprint interoperability; no live service."""
import copy
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_policy as p, access_store as s, ledger

ROOT = Path(__file__).resolve().parents[2]
ALICE, BOB, NEW = 'a' * 40, 'b' * 40, 'c' * 40


class AccessStoreTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.q4t-work/serve-access-20261109/store-fixtures'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'access'
        info = self.root.stat()
        self.identity = [info.st_dev, info.st_ino]
        self.service_id = str(uuid.uuid4())
        self.initial = p.from_keys([dict(id='alice', api_keys=[ALICE]),
                                    dict(id='bob', api_keys=[BOB])])

    def open(self, **options):
        return s.Store(self.directory, self.service_id, self.identity, **options)

    def next(self, before=None, *, key=NEW):
        return p.from_keys([dict(id='alice', api_keys=[key] if key else []),
                            dict(id='bob', api_keys=[BOB])],
                           self.initial if before is None else before)

    def write(self, name, value):
        raw = value if type(value) is bytes else ledger.canonical(value)
        path = self.directory / name
        path.write_bytes(raw); path.chmod(0o600)

    def check_native(self, value, *, success=True):
        binary = Path(os.environ.get('Q4T_ACCESS_CHECKER',
            ROOT / '.q4t-work/serve-access-20261109/host/q4t_ingress_access_tests'))
        result = subprocess.run([str(binary), '--check-policy'],
            input=ledger.canonical(value), capture_output=True, timeout=5)
        self.assertEqual(result.returncode == 0, success)
        if success:
            self.assertEqual(json.loads(result.stdout), p.describe(value))
        else:
            self.assertEqual(result.stdout, b'')

    def test_cpp_python_contracts_and_large_history_match(self):
        self.check_native(self.initial)
        self.check_native(self.next(key=None))
        large = copy.deepcopy(self.initial)
        for i in range(p.MAX_HISTORY - len(large['key_owners'])):
            large['key_owners'][hashlib.sha256(f'retired-{i}'.encode()).hexdigest()] = 'alice'
        self.assertGreater(len(ledger.canonical(large)), 128 * 1024)
        self.check_native(large)
        for value in (dict(self.initial, revision=True),
                      dict(self.initial, revision=1.5),
                      dict(self.initial, unexpected=True)):
            with self.assertRaises(ValueError):p.validate(value)
            self.check_native(value, success=False)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            p.from_keys([dict(id='alice', api_keys=[]),
                         dict(id='bob', api_keys=[ALICE])], self.initial)

    def test_durable_idempotent_update_never_claims_live_application(self):
        request = str(uuid.uuid4());candidate = self.next()
        with self.open(initialize=self.initial) as journal:
            reply = journal.transact(request, 0, candidate)
            self.assertTrue(reply['durable']);self.assertFalse(reply['live_applied'])
            self.assertFalse(reply['replayed'])
            self.assertTrue(journal.transact(request, 0, candidate)['replayed'])
            with self.assertRaisesRegex(ValueError, 'idempotency'):
                journal.transact(request, 0, self.next(key=None))
        with self.open() as journal:
            self.assertEqual(journal.head['state']['policy'], candidate)
            self.assertTrue(journal.transact(request, 0, candidate)['replayed'])
        for file in self.directory.iterdir():
            self.assertEqual(file.stat().st_mode & 0o077, 0)
            raw = file.read_bytes()
            self.assertNotIn(ALICE.encode(), raw);self.assertNotIn(NEW.encode(), raw)
        self.assertEqual({f.name for f in self.directory.iterdir()}, {'writer.lock', 'policy.json'})

    def test_complete_pending_before_rename_recovers_once(self):
        request = str(uuid.uuid4());candidate = self.next(key=None)
        with self.open(initialize=self.initial) as journal:
            before = (self.directory / 'policy.json').read_bytes()
            with mock.patch.object(s.os, 'replace', side_effect=OSError('CPU pre-rename failure')):
                with self.assertRaises(OSError):journal.transact(request, 0, candidate)
            self.assertTrue(journal.poisoned)
            self.assertEqual((self.directory / 'policy.json').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'recovery'):
                journal.transact(request, 0, candidate)
        self.assertTrue((self.directory / 'policy.pending').exists())
        with self.open() as journal:
            self.assertEqual(journal.head['state']['policy'], candidate)
            self.assertTrue(journal.transact(request, 0, candidate)['replayed'])
        self.assertFalse((self.directory / 'policy.pending').exists())

    def test_postrename_fsync_unknown_commit_is_queryable_without_replay(self):
        request = str(uuid.uuid4());candidate = self.next()
        with self.open(initialize=self.initial) as journal:
            sync = s.os.fsync
            def fail_directory(fd):
                if fd == journal.dir_fd:raise OSError('CPU directory fsync failure')
                return sync(fd)
            with mock.patch.object(s.os, 'fsync', side_effect=fail_directory):
                with self.assertRaises(OSError):journal.transact(request, 0, candidate)
            self.assertTrue(journal.poisoned)
        self.assertFalse((self.directory / 'policy.pending').exists())
        with self.open() as journal:
            self.assertEqual(journal.head['state']['policy']['revision'], 1)
            reply = journal.transact(request, 0, candidate)
            self.assertTrue(reply['replayed']);self.assertFalse(reply['live_applied'])
            self.assertEqual(len(journal.head['state']['receipts']), 1)

    def test_recovery_requires_fresh_file_and_directory_durability(self):
        request = str(uuid.uuid4());candidate = self.next(key=None)
        with self.open(initialize=self.initial) as journal:
            with mock.patch.object(s.os, 'replace', side_effect=OSError('CPU interrupt')):
                with self.assertRaises(OSError):journal.transact(request, 0, candidate)
        with mock.patch.object(s.os, 'fsync', side_effect=OSError('still no durable storage')):
            with self.assertRaises(OSError):self.open()
        self.assertTrue((self.directory / 'policy.pending').exists())
        with self.open() as journal:
            self.assertEqual(journal.head['state']['policy'], candidate)
        sync = s.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('directory still unavailable')
            return sync(fd)
        with mock.patch.object(s.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):self.open()
        with self.open() as journal:
            self.assertTrue(journal.transact(request, 0, candidate)['replayed'])

    def test_corruption_or_missing_policy_never_rebootstrap_package_keys(self):
        with self.open(initialize=self.initial):pass
        head = (self.directory / 'policy.json').read_bytes()
        self.write('policy.pending', b'{"partial":')
        with self.assertRaises(ValueError):self.open()
        self.assertEqual((self.directory / 'policy.json').read_bytes(), head)
        self.assertEqual((self.directory / 'policy.pending').read_bytes(), b'{"partial":')
        (self.directory / 'policy.pending').unlink()
        value = json.loads(head);value['sha256'] = '0' * 64;self.write('policy.json', value)
        with self.assertRaises(ValueError):self.open()
        (self.directory / 'policy.json').unlink()
        with self.assertRaises(FileNotFoundError):self.open()
        with self.assertRaises(FileExistsError):self.open(initialize=self.initial)
        self.assertFalse((self.directory / 'policy.json').exists())

    def test_foreign_identity_links_and_competing_writer_are_refused(self):
        with self.open(initialize=self.initial):
            with self.assertRaises(BlockingIOError):self.open()
        with self.assertRaisesRegex(ValueError, 'identity'):
            s.Store(self.directory, str(uuid.uuid4()), self.identity)
        with self.assertRaisesRegex(ValueError, 'identity'):
            s.Store(self.directory, self.service_id, [self.identity[0], self.identity[1] + 1])
        path = self.directory / 'policy.json';outside = self.root / 'outside'
        path.rename(outside);saved = outside.read_bytes()
        path.symlink_to(outside)
        with self.assertRaises(OSError):self.open()
        path.unlink();os.link(outside, path)
        with self.assertRaises(ValueError):self.open()
        self.assertEqual(outside.read_bytes(), saved)

    def test_receipt_window_is_bounded_and_old_requests_cannot_reset_revision(self):
        requests = []
        with self.open(initialize=self.initial) as journal:
            before = self.initial
            for i in range(s.MAX_RECEIPTS + 2):
                candidate = self.next(before, key=NEW if i % 2 == 0 else ALICE)
                request = str(uuid.uuid4());requests.append((request, i, candidate))
                journal.transact(request, i, candidate);before = candidate
            self.assertEqual(len(journal.head['state']['receipts']), s.MAX_RECEIPTS)
            self.assertEqual(len(journal.head['state']['policy']['key_owners']), 3)
        with self.open() as journal:
            with self.assertRaisesRegex(ValueError, 'revision'):
                journal.transact(*requests[0])
            self.assertTrue(journal.transact(*requests[-1])['replayed'])
        self.assertLess((self.directory / 'policy.json').stat().st_size, p.MAX_BYTES)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_changed_head_and_wrong_pending_predecessor_fail_before_overwrite(self):
        request = str(uuid.uuid4());candidate = self.next()
        with self.open(initialize=self.initial) as journal:
            value = copy.deepcopy(journal.head)
            value['state']['policy']['clients'][0]['key_sha256'].clear()
            value['sha256'] = ledger.digest(value['state']);self.write('policy.json', value)
            changed = (self.directory / 'policy.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'committed access'):
                journal.transact(request, 0, candidate)
            self.assertEqual((self.directory / 'policy.json').read_bytes(), changed)
        receipt = dict(id=request, request_sha256=ledger.digest(dict(expected_revision=0, policy=candidate)),
            revision=1, policy_sha256=p.digest(candidate))
        value['state'] = dict(policy=candidate, receipts=[receipt])
        value['sha256'] = ledger.digest(value['state']);value['previous_sha256'] = 'f' * 64
        self.write('policy.pending', value)
        with self.assertRaisesRegex(ValueError, 'predecessor'):self.open()
        self.assertEqual((self.directory / 'policy.json').read_bytes(), changed)
        self.assertTrue((self.directory / 'policy.pending').exists())


if __name__ == '__main__':
    unittest.main()
