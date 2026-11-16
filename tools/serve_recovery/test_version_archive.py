"""Real CPU journals/locks/renames; explicitly synthetic model qualification."""
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import deployment, publication, publication_files
from tools.serve_recovery import test_publication, test_version_registration
from tools.serve_recovery import version_archive as archive
from tools.serve_recovery import version_registration, version_store


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.base = test_version_registration.RegistrationTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.install()
        self.retired = self.base.registration['registry']['cycle_id']
        self.spent = self.base.consume()
        self.fixture = self.base.base.fixture
        metadata = self.fixture.directory/'cpu-engine-release.json'
        test_publication.engine.save(metadata, dict(cpu_fixture=True))
        self.fixture.base['files']['q4t.release.json'] = test_publication.engine.sha(metadata)
        newer = self.base.repair()
        self.base.install(newer, self.retired)
        self.state = self.base.state
        self.desc = deployment.read_descriptor(self.state)
        self.root = self.fixture.directory/'default'
        self.root.mkdir(mode=0o700)
        package = Path(self.desc['package'])
        # Publication needs actual sealed engine files even in CPU fixtures.
        shutil.copyfile(self.fixture.binary, package/'engine/q4t')
        shutil.copyfile(metadata, package/'engine/q4t.release.json')
        release = test_publication.accepted(package, self.desc['manifest_sha256'],
            self.state, self.fixture.directory/'publication-release.json')
        publication.execute(self.state, request_id=str(uuid.uuid4()), root=self.root,
            package=package, digest=self.desc['manifest_sha256'], release=release,
            expected=self.desc['manifest_sha256'], revision=0)
        self.desc = deployment.read_descriptor(self.state)
        self.current = publication.ready(self.state, self.desc)
        self.source = self.state/version_registration.DIRECTORY/(self.retired+'.json')
        self.output = self.fixture.directory/'version-archive'

    def preserved(self):
        return {str(p.relative_to(self.state)): p.read_bytes()
                for p in self.state.rglob('*.json')
                if version_registration.DIRECTORY not in p.relative_to(self.state).parts}

    def prepare(self):
        return archive.prepare(self.state, self.output, [self.retired])

    def test_archive_replay_restore_keeps_consumed_predecessor_and_current_state(self):
        before, raw = self.preserved(), self.source.read_bytes()
        value = json.loads(raw)
        self.assertEqual(value['registration']['registry']['cycle_id'], self.retired)
        active = version_registration.intents(self.state)[
            self.base.load()['registry']['cycle_id']]['committed']
        self.assertEqual(json.loads(active.read_bytes())['previous'], self.spent)
        plan = self.prepare()
        with mock.patch.object(archive.publication.service, 'launch',
                               side_effect=AssertionError('no model')):
            result = archive.apply(plan['plan'], plan['sha256'])
            self.assertEqual(result['status'], 'RETIRED')
            self.assertEqual(archive.apply(plan['plan'], plan['sha256']), result)
            self.assertFalse(self.source.exists())
            self.assertEqual(self.preserved(), before)
            selected = version_registration.load(self.state, self.desc)
            self.assertEqual(selected['state'], self.base.load())
            restored = archive.apply(plan['plan'], plan['sha256'], restore=True)
            self.assertEqual(restored['status'], 'RESTORED')
            self.assertEqual(archive.apply(plan['plan'], plan['sha256'], restore=True), restored)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.preserved(), before)
        with self.assertRaisesRegex(ValueError, 'archive changed'):
            archive.apply(plan['plan'], plan['sha256'])

    def test_current_cycle_and_retained_publication_roots_are_protected(self):
        selected = self.base.load()
        with self.assertRaisesRegex(ValueError, 'retained root'):
            archive.prepare(self.state, self.output, [selected['registry']['cycle_id']])
        original = archive.protected
        def with_retained(state, desc, current, selected, records):
            current = copy.deepcopy(current)
            current['current']['manifest_sha256'] = self.base.root['manifest_sha256']
            return original(state, desc, current, selected, records)
        with mock.patch.object(archive, 'protected', side_effect=with_retained), \
                self.assertRaisesRegex(ValueError, 'retained root'):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.assertTrue(self.source.exists())

    def test_pending_registration_or_active_reference_prevents_movement(self):
        pending = self.source.with_name(self.source.name+'.new')
        pending.write_bytes(self.source.read_bytes()); pending.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'needs resume'):
            self.prepare()
        pending.unlink()
        reference = self.state/'active-reference.json'
        test_publication.engine.save(reference, dict(required=str(self.source)))
        with self.assertRaisesRegex(ValueError, 'active state/release dependency'):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_changed_plan_bytes_collision_or_unsafe_destination_are_rejected(self):
        unsafe = self.source.parent/'nested'
        with self.assertRaisesRegex(ValueError, 'new private archive directory'):
            archive.prepare(self.state, unsafe, [self.retired])
        plan = self.prepare()
        with self.assertRaisesRegex(ValueError, 'reviewed archive plan'):
            archive.apply(plan['plan'], 'f'*64)
        raw = self.source.read_bytes()
        self.source.write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError, 'bytes or identity'):
            archive.apply(plan['plan'], plan['sha256'])
        self.source.write_bytes(raw)
        shutil.copyfile(self.source, self.output/self.source.name)
        with self.assertRaisesRegex(ValueError, 'collision or loss'):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(self.source.exists())

    def test_interrupted_rename_resumes_and_fsyncs_without_reset(self):
        plan, before = self.prepare(), self.preserved()
        sync = publication_files.sync
        fired = False
        def fail(path):
            nonlocal fired
            if Path(path) == self.source.parent and not fired:
                fired = True
                raise OSError('CPU interruption after atomic rename')
            return sync(path)
        with mock.patch.object(publication_files, 'sync', side_effect=fail), \
                self.assertRaises(OSError):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(fired); self.assertFalse(self.source.exists())
        with mock.patch.object(publication_files, 'sync', wraps=sync) as repeated:
            self.assertEqual(archive.apply(plan['plan'], plan['sha256'])['status'], 'RETIRED')
            self.assertIn(mock.call(self.source.parent), repeated.call_args_list)
            self.assertIn(mock.call(self.output), repeated.call_args_list)
        self.assertEqual(before, self.preserved())

    def test_lease_and_version_writer_competition_do_not_move_files(self):
        plan = self.prepare()
        lease = archive.publication.publication.bundle.verify(
            self.desc['package'], self.desc['manifest_sha256'])['lock_path']
        with open(lease, 'a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises((BlockingIOError, ValueError, RuntimeError)):
                archive.apply(plan['plan'], plan['sha256'])
        with version_store.open_bound(self.base.binding), self.assertRaises(BlockingIOError):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertTrue(self.source.exists())
        self.assertFalse((self.output/self.source.name).exists())

    def test_changed_publication_or_policy_rejects_stale_plan(self):
        plan = self.prepare()
        self.base.consume()
        before = self.preserved()
        with self.assertRaisesRegex(ValueError, 'publication/version/archive changed'):
            archive.apply(plan['plan'], plan['sha256'])
        self.assertEqual(before, self.preserved())
        self.assertTrue(self.source.exists())

    def test_restore_respects_active_capacity_and_does_not_delete_evidence(self):
        plan = self.prepare()
        archive.apply(plan['plan'], plan['sha256'])
        with mock.patch.object(version_registration, 'MAX_CYCLES', 1), \
                self.assertRaisesRegex(ValueError, 'restore exceeds'):
            archive.apply(plan['plan'], plan['sha256'], restore=True)
        self.assertTrue((self.output/self.source.name).is_file())
        self.assertFalse(self.source.exists())


if __name__ == '__main__': unittest.main()
