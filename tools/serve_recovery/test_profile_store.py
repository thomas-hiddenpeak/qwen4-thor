"""Real private journal faults cannot reset or bypass profile switch budgets."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

from tools.serve_recovery import access_store, ledger, profile_policy as p
from tools.serve_recovery import profile_store as s, test_profile_policy as fixtures

ROOT = Path(__file__).resolve().parents[2]


class StoreTests(unittest.TestCase):
    def setUp(self):
        base = ROOT/'.q4t-work/serve-adaptive-profiles-20261111/store-fixtures'
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.binding = access_store.binding(self.path, str(uuid.uuid4()))
        self.stamp = ledger.Stamp(str(uuid.uuid4()), 1)
        self.initial = p.initial(fixtures.registry(), self.stamp)
        s.initialize_bound(self.binding, self.initial)

    def load(self):
        return s.execute(self.binding, dict(sequence=1, op='load'))

    def intent(self):
        previous = self.load()
        candidate = p.prepare(previous, 'light', 'cold_memory_capacity',
                              str(uuid.uuid4()), 0, self.stamp)
        return dict(sequence=1, op='commit', request_id=str(uuid.uuid4()),
                    expected_revision=previous['revision'], candidate=candidate)

    def test_lost_receipt_replays_same_intent_without_spending_budget_again(self):
        request = self.intent()
        with s.open_bound(self.binding) as writer:
            original = os.fsync
            def fail(fd):
                if fd == writer.dir_fd: raise OSError('post-rename sync unknown')
                return original(fd)
            with mock.patch.object(access_store.os, 'fsync', side_effect=fail):
                with self.assertRaises(OSError):
                    writer.transact(request['request_id'], request['expected_revision'],
                                    request['candidate'])
        actual = s.execute(self.binding, request)
        self.assertEqual(actual, request['candidate'])
        self.assertEqual(s.execute(self.binding, request), actual)
        self.assertEqual(len(actual['history']), 1)
        self.assertFalse(p.describe(actual)['model_actions'])

    def test_pending_rename_recovers_intent_before_read_or_launch(self):
        request = self.intent()
        with mock.patch.object(access_store.os, 'replace', side_effect=OSError('before rename')):
            with self.assertRaises(OSError): s.execute(self.binding, request)
        self.assertTrue((self.path/'profile-selection/policy.pending').exists())
        actual = self.load()
        self.assertEqual(actual, request['candidate'])
        self.assertEqual(p.selected_for_launch(actual), 'light')
        self.assertFalse((self.path/'profile-selection/policy.pending').exists())

    def test_missing_or_corrupt_journal_never_rebootstraps_old_selection(self):
        actual = s.execute(self.binding, self.intent())
        path = self.path/'profile-selection/policy.json'
        original = path.read_bytes(); path.write_bytes(b'{')
        with self.assertRaises(ValueError): self.load()
        path.write_bytes(original)
        self.assertEqual(self.load(), actual)
        shutil.rmtree(path.parent)
        with self.assertRaises((OSError, ValueError)):
            s.initialize_bound(self.binding, self.initial)
        self.assertFalse(path.exists())
        self.assertTrue((self.path/'profile-selection.json').exists())

    def test_writer_conflict_and_stale_revision_do_not_mutate_original_state(self):
        request = self.intent()
        with s.open_bound(self.binding):
            with self.assertRaises(BlockingIOError): s.execute(self.binding, request)
        actual = s.execute(self.binding, request)
        conflict = dict(request, request_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            s.execute(self.binding, conflict)
        self.assertEqual(self.load(), actual)
        self.assertEqual({f.name for f in self.path.iterdir()},
                         {'profile-selection', 'profile-selection.json'})


if __name__ == '__main__': unittest.main()
