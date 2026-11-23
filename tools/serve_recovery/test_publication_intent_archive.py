"""Real CPU journals/locks/renames; synthetic packages, no model loading."""
import fcntl
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import publication_intent_archive as archive
from tools.serve_recovery import test_publication as fixture


class IntentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PublicationTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.publish()
        self.state, self.root = self.f.state, self.f.root
        self.retired = self.f.request
        # Populate real history beyond its 16 retained transactions.
        for _ in range(16):
            self.publish()
        self.desc = fixture.d.read_descriptor(self.state)
        self.current = fixture.p.ready(self.state, self.desc)
        self.source = fixture.p.intent_path(self.state, self.retired)
        self.output = self.f.fixture.directory/'intent-archive'

    def publish(self):
        current = fixture.p.status(self.state)
        return fixture.p.execute(self.state, request_id=str(uuid.uuid4()),
            root=self.root, package=self.f.target, digest=self.f.digest,
            release=self.f.release, expected=self.f.digest,
            revision=current['revision'])

    def prepare(self):
        return archive.prepare(self.state, self.output, [self.retired])

    def preserved(self):
        return {str(p.relative_to(self.state)): p.read_bytes()
            for p in self.state.rglob('*.json')
            if fixture.p.INTENTS not in p.relative_to(self.state).parts}

    def test_capacity_archive_replay_restore_and_new_publish_without_budget_reset(self):
        before, raw = self.preserved(), self.source.read_bytes()
        with mock.patch.object(fixture.p, 'MAX_INTENTS', 17), \
                mock.patch.object(fixture.p.files, 'snapshot') as snapshot, \
                self.assertRaisesRegex(ValueError, 'intent capacity reached'):
            self.publish()
        snapshot.assert_not_called()
        plan = self.prepare()
        with mock.patch.object(archive.archives.service, 'launch',
                               side_effect=AssertionError('no model')):
            result = archive.apply(plan['plan'], plan['sha256'])
            self.assertEqual(result['status'], 'RETIRED')
            self.assertEqual(archive.apply(plan['plan'], plan['sha256']), result)
            self.assertFalse(self.source.exists())
            self.assertEqual(before, self.preserved())
            with self.assertRaisesRegex(ValueError, 'intent unavailable'):
                fixture.p.execute(self.state, request_id=self.retired, resume=True)
            restored = archive.apply(plan['plan'], plan['sha256'], restore=True)
            self.assertEqual(restored['status'], 'RESTORED')
            self.assertEqual(archive.apply(plan['plan'], plan['sha256'], restore=True), restored)
        self.assertEqual(raw, self.source.read_bytes())
        self.assertEqual(before, self.preserved())
        with self.assertRaisesRegex(ValueError, 'publication/archive changed'):
            archive.apply(plan['plan'], plan['sha256'])
        # A new explicit plan reclaims the same obsolete intent; retain both plans.
        self.output = self.output.with_name('second-intent-archive')
        plan = self.prepare()
        archive.apply(plan['plan'], plan['sha256'])
        with mock.patch.object(fixture.p, 'MAX_INTENTS', 17):
            self.assertTrue(self.publish()['durable'])
        self.assertEqual(self.f.business(), self.f.before)
        with self.assertRaisesRegex(ValueError, 'predecessor changed'):
            self.f.publish()  # Original archived request retains its stale revision.

    def test_history_prejournal_and_partial_intents_are_protected(self):
        for row in self.current['history']:
            with self.assertRaisesRegex(ValueError, 'retained intent'):
                archive.prepare(self.state, self.output, [row['request_id']])
        request = str(uuid.uuid4())
        fixture.p.prepare_intent(self.state, self.desc, self.f.target, self.f.digest,
            self.f.release, self.root, self.f.digest, self.current['revision'], request)
        with self.assertRaisesRegex(ValueError, 'retained intent'):
            archive.prepare(self.state, self.output, [request])
        self.source.with_name(self.source.name+'.new').write_bytes(b'{')
        with self.assertRaisesRegex(ValueError, 'partial or unknown'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_changed_plan_bytes_and_collision_reject_before_move(self):
        plan = self.prepare()
        with self.assertRaisesRegex(ValueError, 'reviewed archive plan'):
            archive.apply(plan['plan'], 'f'*64)
        raw = self.source.read_bytes()
        self.source.chmod(0o600)
        self.source.write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError, 'bytes or identity'):
            archive.apply(plan['plan'], plan['sha256'])
        self.source.write_bytes(raw)
        shutil.copyfile(self.source, self.output/self.source.name)
        with self.assertRaisesRegex(ValueError, 'collision or loss'):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(self.source.exists())

    def test_interrupted_rename_resumes_exact_plan_and_syncs(self):
        plan, before = self.prepare(), self.preserved()
        original, fired = archive.files.sync, False
        def fail(path):
            nonlocal fired
            if Path(path) == self.source.parent and not fired:
                fired = True
                raise OSError('CPU interruption after rename')
            return original(path)
        with mock.patch.object(archive.files, 'sync', side_effect=fail), \
                self.assertRaises(OSError):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(fired)
        with mock.patch.object(archive.files, 'sync', wraps=original) as repeated:
            self.assertEqual(archive.apply(plan['plan'], plan['sha256'])['status'], 'RETIRED')
            self.assertIn(mock.call(self.source.parent), repeated.call_args_list)
            self.assertIn(mock.call(self.output), repeated.call_args_list)
        self.assertEqual(before, self.preserved())

    def test_lease_and_publication_writer_competition_preserve_files(self):
        plan = self.prepare()
        for lock in (self.f.lease, self.state/'publication.lock'):
            with lock.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises((BlockingIOError, RuntimeError, ValueError)):
                    archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(self.source.exists())
        self.assertFalse((self.output/self.source.name).exists())

    def test_active_reference_symlink_and_unsafe_destination_are_rejected(self):
        reference = self.state/'active-reference.json'
        fixture.engine.save(reference, dict(required=str(self.source)))
        with self.assertRaisesRegex(ValueError, 'active state/release dependency'):
            self.prepare()
        reference.unlink()
        with self.assertRaisesRegex(ValueError, 'new private archive directory'):
            archive.prepare(self.state, self.source.parent/'nested', [self.retired])
        self.output.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'new private archive directory'):
            self.prepare()
        self.assertTrue(self.source.exists())

    def test_new_publication_rejects_stale_plan_before_movement(self):
        plan = self.prepare()
        self.publish()
        before = self.preserved()
        with self.assertRaisesRegex(ValueError, 'publication/archive changed'):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertEqual(before, self.preserved())
        self.assertTrue(self.source.exists())

    def test_restore_capacity_is_checked_before_movement(self):
        plan = self.prepare()
        archive.apply(plan['plan'], plan['sha256'])
        with mock.patch.object(fixture.p, 'MAX_INTENTS', 16), \
                self.assertRaisesRegex(ValueError, 'restore exceeds active intent capacity'):
            archive.apply(plan['plan'], plan['sha256'], restore=True)
        self.assertFalse(self.source.exists())
        self.assertTrue((self.output/self.source.name).is_file())


if __name__ == '__main__':
    unittest.main()
