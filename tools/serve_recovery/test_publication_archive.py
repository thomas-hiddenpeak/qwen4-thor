"""Real CPU filesystem/lock transactions with explicitly synthetic packages."""
import fcntl
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid

from tools.serve_recovery import publication_archive as archive
from tools.serve_recovery import test_publication as fixture


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',
            fixture.test_native_bundle.ROOT/'.q4t-work/publication-archive-cpu'))
        self.env=mock.patch.dict(os.environ,Q4T_TEST_OUTPUT_DIR=str(root))
        self.env.start();self.addCleanup(self.env.stop)
        self.f=fixture.PublicationTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.publish()
        self.state,self.root=self.f.state,self.f.root
        self.desc=fixture.d.read_descriptor(self.state)
        self.current=fixture.p.ready(self.state,self.desc)
        self.package=self.f.fixture.directory/'retired-payload'
        shutil.copytree(self.f.package,self.package)
        self.release=fixture.accepted(self.package,self.f.digest,self.state,
            self.f.fixture.directory/'retired-release.json')
        self.selected=fixture.files.snapshot(self.root,self.state,self.desc,self.package,
            self.f.digest,self.release)
        self.hash=self.selected['generation_sha256']
        self.source=fixture.files.generation(self.root,self.selected)
        self.output=self.f.fixture.directory/'archive'

    def prepare(self):return archive.prepare(self.state,self.output,[self.hash])

    def test_archive_replay_restore_preserve_current_budgets_and_all_original_bytes(self):
        before=self.f.business();expected={n:(self.source/n).read_bytes() for n in fixture.files.FILES}
        receipt=self.prepare()
        with mock.patch.object(archive.service,'launch',side_effect=AssertionError('no model')):
            result=archive.apply(receipt['plan'],receipt['sha256'])
            self.assertEqual(result['status'],'RETIRED');self.assertFalse(self.source.exists())
            self.assertEqual(archive.apply(receipt['plan'],receipt['sha256']),result)
            self.assertEqual(fixture.p.ready(self.state,self.desc),self.current)
            self.assertEqual(self.f.business(),before)
            restored=archive.apply(receipt['plan'],receipt['sha256'],restore=True)
            self.assertEqual(restored['status'],'RESTORED')
            self.assertEqual(archive.apply(receipt['plan'],receipt['sha256'],restore=True),restored)
        self.assertEqual({n:(self.source/n).read_bytes() for n in fixture.files.FILES},expected)
        self.assertEqual(self.f.business(),before)
        with self.assertRaisesRegex(ValueError,'publication/archive changed'):
            archive.apply(receipt['plan'],receipt['sha256'])

    def test_current_history_and_not_yet_journaled_intent_are_protected(self):
        retained={self.current['current']['generation_sha256'],
                  self.current['history'][0]['previous']['generation_sha256']}
        for digest in retained:
            with self.assertRaisesRegex(ValueError,'retained generation'):
                archive.prepare(self.state,self.output,[digest])
        fixture.p.prepare_intent(self.state,self.desc,self.package,self.f.digest,self.release,
            self.root,self.f.digest,self.current['revision'],str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError,'retained generation'):self.prepare()
        self.assertFalse(self.output.exists());self.assertTrue(self.source.exists())

    def test_changed_plan_or_generation_rejected_before_movement(self):
        receipt=self.prepare()
        with self.assertRaisesRegex(ValueError,'reviewed archive plan changed'):
            archive.apply(receipt['plan'],'f'*64)
        path=self.source/'q4t';original=path.read_bytes();path.chmod(0o600)
        path.write_bytes(b'changed CPU fixture')
        with self.assertRaisesRegex(ValueError,'archive bytes changed'):
            archive.apply(receipt['plan'],receipt['sha256'])
        self.assertTrue(self.source.exists());self.assertFalse((self.output/self.hash).exists())
        path.write_bytes(original);path.chmod(0o500)
        (self.output/self.hash).mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError,'collision or loss'):
            archive.apply(receipt['plan'],receipt['sha256'])

    def test_interruption_after_atomic_rename_resumes_original_plan(self):
        receipt=self.prepare();sync=fixture.files.sync;fired=False
        def fail(path):
            nonlocal fired
            if Path(path)==self.source.parent and not fired:
                fired=True;raise OSError('CPU interruption after rename')
            return sync(path)
        with mock.patch.object(fixture.files,'sync',side_effect=fail),self.assertRaises(OSError):
            archive.apply(receipt['plan'],receipt['sha256'])
        self.assertTrue(fired);self.assertFalse(self.source.exists())
        self.assertFalse((self.output/'retired.json').exists())
        with mock.patch.object(fixture.files,'sync',wraps=sync) as repeated:
            self.assertEqual(archive.apply(receipt['plan'],receipt['sha256'])['status'],'RETIRED')
            self.assertIn(mock.call(self.source.parent),repeated.call_args_list)
            self.assertIn(mock.call(self.output),repeated.call_args_list)
        self.assertEqual(fixture.p.ready(self.state,self.desc),self.current)

    def test_actual_lease_competition_does_not_move_generation(self):
        receipt=self.prepare()
        with self.f.lease.open('r+') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises((BlockingIOError,RuntimeError,ValueError)):
                archive.apply(receipt['plan'],receipt['sha256'])
            with self.f.lease.open('r+') as other,self.assertRaises(BlockingIOError):
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.assertTrue(self.source.exists());self.assertFalse((self.output/self.hash).exists())

    def test_active_file_reference_and_unsafe_destination_are_not_retired(self):
        fixture.engine.save(self.state/'active-reference.json',dict(required=str(self.source/'q4t')))
        with self.assertRaisesRegex(ValueError,'active state/release dependency'):self.prepare()
        (self.state/'active-reference.json').unlink()
        with self.assertRaisesRegex(ValueError,'new private archive directory'):
            archive.prepare(self.state,self.source/'nested',[self.hash])
        self.output.symlink_to(self.source,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'new private archive directory'):self.prepare()
        self.assertTrue(self.source.exists())


if __name__=='__main__':unittest.main()
