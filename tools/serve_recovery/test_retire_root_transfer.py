"""Real transfer, registration and checkpoint bytes; no model process."""
import copy
from contextlib import contextmanager
import os
from unittest import mock
import unittest
import uuid

from tools.serve_recovery import deployment, retire_root_transfer as archive
from tools.serve_recovery import root_transfer, version_registration
from tools.serve_recovery import test_root_transfer
from tools.serve_service import package


class RetirementTests(unittest.TestCase):
    def setUp(self):
        fixture = test_root_transfer.TransferTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.switch(fixture.old)
        self.state = fixture.state
        self.source = self.state/root_transfer.INTENT
        self.raw = self.source.read_bytes()
        self.digest = package.sha(self.source)
        self.output = self.state.parent/'saved-transfer'
        self.desc = deployment.read_descriptor(self.state)
        self.package = self.desc['package']
        self.selected = copy.deepcopy(fixture.base.load())
        self.binding = fixture.binding
        self.entries = {key:(p['committed'], package.read(p['committed']))
                        for key, p in version_registration.intents(self.state).items()}
        self.entry_bytes = {p:p.read_bytes() for p, _ in self.entries.values()}
        self.desc['deployment_revision'] += 1
        self.selected['revision'] += 1
        self.selected['registry']['cycle_id'] = str(uuid.uuid4())

        @contextmanager
        def stable(*args, **kwargs):
            yield self.state, self.desc, {}, {}, self.selected, fixture.binding

        for patch in (mock.patch.object(archive.maintenance, 'authority'),
                      mock.patch.object(archive.version_archive, 'locked', stable),
                      mock.patch.object(archive.version_archive, 'entries', return_value=self.entries)):
            patch.start()
            self.addCleanup(patch.stop)

    def retire(self):
        return archive.retire(self.state, self.package, self.desc['manifest_sha256'],
                              self.output, self.digest)

    def test_rename_crash_receipt_and_idempotent_retry(self):
        original = os.rename
        def interrupted(src, dst):
            if src == self.source:
                raise OSError('simulated interruption before atomic move')
            return original(src, dst)
        with mock.patch.object(os, 'rename', interrupted), self.assertRaises(OSError):
            self.retire()
        self.assertEqual(self.source.read_bytes(), self.raw)
        inode = self.source.stat().st_ino
        result = self.retire()
        self.assertFalse(self.source.exists())
        saved = self.output/root_transfer.INTENT
        self.assertEqual(saved.read_bytes(), self.raw)
        self.assertEqual(saved.stat().st_ino, inode)
        self.assertEqual(result, self.retire())
        self.assertEqual(self.entry_bytes, {p:p.read_bytes() for p in self.entry_bytes})
        self.assertEqual(len(result['checkpoints']), 2)
        self.source.write_bytes(self.raw)
        self.source.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'also exists'):
            self.retire()
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_live_cycle_wrong_digest_and_partial_write_are_preserved(self):
        current = self.selected['registry']['cycle_id']
        self.selected['registry']['cycle_id'] = next(iter(self.entries))
        with self.assertRaisesRegex(ValueError, 'current deployment or version'):
            self.retire()
        self.selected['registry']['cycle_id'] = current
        self.desc['deployment_revision'] -= 1
        with self.assertRaisesRegex(ValueError, 'current deployment or version'):
            self.retire()
        self.desc['deployment_revision'] += 1
        self.digest = '0'*64
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            self.retire()
        self.digest = package.sha(self.source)
        self.source.with_suffix('.json.new').touch(mode=0o600)
        with self.assertRaisesRegex(ValueError, 'being written'):
            self.retire()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), self.raw)

    def test_missing_checkpoint_and_unrelated_output_are_preserved(self):
        with mock.patch.object(archive.root_checkpoints, 'read', return_value=None):
            with self.assertRaisesRegex(ValueError, 'checkpoint missing'):
                self.retire()
        self.output.mkdir(mode=0o700)
        (self.output/'unrelated').touch()
        with self.assertRaisesRegex(ValueError, 'unexpected root transfer archive'):
            self.retire()
        self.assertEqual(self.source.read_bytes(), self.raw)
